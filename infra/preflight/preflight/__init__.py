"""Read-only preflight inventory for Project PAT (PAT-011, PAT-004).

Everything here is observation. No file outside the report path is written, no service is touched.
All host access goes through a `Runner` so the logic is testable without a Proxmox host.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional


class Runner:
    """Executes commands and reads files. Replaced by a fake in tests."""

    def run(self, cmd: list[str], timeout: int = 20) -> tuple[int, str]:
        if shutil.which(cmd[0]) is None:
            return 127, ""
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout + p.stderr
        except (subprocess.TimeoutExpired, OSError) as e:  # pragma: no cover
            return 1, str(e)

    def read(self, path: str) -> Optional[str]:
        try:
            return Path(path).read_text()
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        return Path(path).exists()


@dataclass
class Check:
    name: str
    ok: Optional[bool]          # None = could not determine (reported as unavailable, never invented)
    value: str
    expected: str = ""
    severity: str = "error"     # error | warn | info


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    loss_inventory: dict = field(default_factory=dict)

    def add(self, name, ok, value, expected="", severity="error"):
        self.checks.append(Check(name, ok, str(value), expected, severity))

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False and c.severity == "error"]

    def to_json(self) -> str:
        return json.dumps({"checks": [asdict(c) for c in self.checks], "facts": self.facts,
                           "loss_inventory": self.loss_inventory}, indent=2)

    def to_markdown(self) -> str:
        sym = {True: "✓", False: "✗", None: "?"}
        lines = ["# Preflight report", "", "| | Check | Value | Expected |", "|---|---|---|---|"]
        for c in self.checks:
            lines.append(f"| {sym[c.ok]} | {c.name} | {c.value} | {c.expected} |")
        if self.loss_inventory:
            lines += ["", "## What a reinstall would destroy", ""]
            for k, v in self.loss_inventory.items():
                lines.append(f"- **{k}**: {v}")
        b = self.blocking
        lines += ["", f"**Blocking problems: {len(b)}**" if b else "**No blocking problems.**"]
        return "\n".join(lines) + "\n"


# ---------- individual collectors (pure functions of Runner) ----------

def collect_cpu(r: Runner, rep: Report):
    txt = r.read("/proc/cpuinfo") or ""
    model = next((l.split(":", 1)[1].strip() for l in txt.splitlines() if l.startswith("model name")), "")
    threads = sum(1 for l in txt.splitlines() if l.startswith("processor"))
    flags = next((l.split(":", 1)[1] for l in txt.splitlines() if l.startswith("flags")), "")
    rep.facts["cpu_model"] = model
    rep.facts["cpu_threads"] = threads
    rep.add("CPU model", "Ultra 7 265K" in model or None, model or "unavailable", "Intel Core Ultra 7 265K", "warn")
    rep.add("VT-x (vmx flag)", "vmx" in flags.split() if flags else None, "present" if "vmx" in flags else "absent", "present")
    rep.add("CPU threads", threads >= 20 if threads else None, threads, ">= 20", "warn")


def collect_memory(r: Runner, rep: Report):
    txt = r.read("/proc/meminfo") or ""
    m = re.search(r"MemTotal:\s+(\d+) kB", txt)
    gib = round(int(m.group(1)) / 1024 / 1024, 1) if m else None
    rep.facts["mem_gib"] = gib
    rep.add("RAM total", (gib is not None and gib >= 60) if gib else None, f"{gib} GiB" if gib else "unavailable", "~64 GiB")


def collect_iommu(r: Runner, rep: Report):
    rc, out = r.run(["dmesg"])
    cmdline = r.read("/proc/cmdline") or ""
    if rc != 0:
        rep.add("IOMMU (VT-d)", None, "dmesg unavailable", "DMAR: IOMMU enabled", "warn")
        return
    enabled = bool(re.search(r"DMAR: IOMMU enabled|DMAR: Intel\(R\) Virtualization Technology for Directed I/O", out))
    rep.facts["iommu_enabled"] = enabled
    rep.add("IOMMU (VT-d)", enabled, "enabled" if enabled else "not detected",
            "enabled in BIOS (System Agent → VT-d)", "warn")
    rep.facts["intel_iommu_cmdline"] = "intel_iommu=on" in cmdline


def collect_gpu(r: Runner, rep: Report):
    rc, out = r.run(["lspci", "-nn"])
    nv = [l for l in out.splitlines() if "NVIDIA" in l and ("VGA" in l or "3D" in l)]
    igpu = [l for l in out.splitlines() if "Intel" in l and "VGA" in l]
    rep.facts["nvidia_pci"] = nv
    rep.facts["intel_igpu_pci"] = igpu
    rep.add("NVIDIA GPU on PCI bus", bool(nv) if rc == 0 else None, nv[0].strip() if nv else "none", "RTX 4070 Ti SUPER")
    rep.add("Intel iGPU present (for Jellyfin/Frigate later)", bool(igpu) if rc == 0 else None,
            igpu[0].strip() if igpu else "none (iGPU disabled in BIOS?)", "present", "warn")
    rc, out = r.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"])
    if rc == 0 and out.strip():
        rep.facts["nvidia_smi"] = out.strip()
        rep.add("NVIDIA driver loaded", True, out.strip(), "", "info")
    else:
        rep.add("NVIDIA driver loaded", None, "not installed yet (expected before step 20)", "", "info")
    rep.add("nouveau loaded", not bool(re.search(r"^nouveau\s", r.read("/proc/modules") or "", re.M)),
            "loaded" if re.search(r"^nouveau\s", r.read("/proc/modules") or "", re.M) else "not loaded",
            "not loaded", "warn")


def collect_pve(r: Runner, rep: Report):
    rc, out = r.run(["pveversion"])
    if rc != 0:
        rep.add("Proxmox VE", False, "pveversion not found", "pve-manager/9.x")
        return
    m = re.search(r"pve-manager/(\d+)\.(\d+)", out)
    ver = f"{m.group(1)}.{m.group(2)}" if m else out.strip()
    rep.facts["pve_version"] = ver
    rep.add("Proxmox VE", bool(m) and int(m.group(1)) >= 9, ver, "9.x")
    rc, out = r.run(["uname", "-r"])
    rep.facts["kernel"] = out.strip()
    rep.add("Kernel", True if rc == 0 else None, out.strip(), "", "info")
    rc, out = r.run(["apt-mark", "showhold"])
    rep.facts["held_packages"] = out.split() if rc == 0 else []


def collect_storage(r: Runner, rep: Report):
    rc, out = r.run(["zpool", "status", "-x"])
    if rc == 0:
        healthy = "all pools are healthy" in out
        rep.add("ZFS pools", healthy, out.strip().splitlines()[0] if out.strip() else "", "all pools are healthy")
        rc2, lst = r.run(["zpool", "list", "-H", "-o", "name,size,alloc,free,health"])
        rep.facts["zpools"] = [l.split("\t") for l in lst.strip().splitlines()] if rc2 == 0 else []
    else:
        rep.add("ZFS pools", None, "zpool unavailable (ext4/LVM install?)", "ZFS rpool (ADR-0002)", "warn")
    rc, out = r.run(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL,MOUNTPOINT,FSTYPE"])
    if rc == 0:
        try:
            rep.facts["block_devices"] = json.loads(out)["blockdevices"]
        except (ValueError, KeyError):
            rep.facts["block_devices"] = []
    nvme = [d for d in rep.facts.get("block_devices", []) if d.get("type") == "disk" and d["name"].startswith("nvme")]
    rep.add("NVMe disk present", bool(nvme) if rc == 0 else None,
            ", ".join(f"{d['name']} {d.get('size')} {d.get('model') or ''}".strip() for d in nvme) or "none", "1× ~2 TB")
    arc = r.read("/sys/module/zfs/parameters/zfs_arc_max")
    if arc is not None:
        val = int(arc.strip() or 0)
        rep.facts["zfs_arc_max"] = val
        rep.add("ZFS ARC cap", 0 < val <= 8 * 1024**3, f"{val // 1024**3} GiB" if val else "unset (default = 50% RAM)", "<= 8 GiB", "warn")
    rc, out = r.run(["smartctl", "--scan"])
    rep.facts["smart_available"] = rc == 0
    rep.add("SMART tooling", True if rc == 0 else None, "available" if rc == 0 else "smartctl not installed (step 10)", "", "info")


def collect_network(r: Runner, rep: Report):
    rc, out = r.run(["ip", "-j", "-4", "addr"])
    ifaces = {}
    if rc == 0:
        try:
            for i in json.loads(out):
                ifaces[i["ifname"]] = [a["local"] + "/" + str(a["prefixlen"]) for a in i.get("addr_info", [])]
        except ValueError:
            pass
    rep.facts["interfaces"] = ifaces
    rep.add("vmbr0 has IPv4", bool(ifaces.get("vmbr0")) if rc == 0 else None, ", ".join(ifaces.get("vmbr0", [])) or "none", "LAN address")
    rep.add("vmbr1 (LAB) exists", "vmbr1" in ifaces if rc == 0 else None, "yes" if "vmbr1" in ifaces else "no (created in step 40)", "", "info")
    rep.add("tailscale0 exists", "tailscale0" in ifaces if rc == 0 else None, "yes" if "tailscale0" in ifaces else "no (step 15)", "", "info")
    rc, out = r.run(["ethtool", "vmbr0"])
    rep.add("Link", None, "bridge (ethtool n/a)", "", "info")
    sshd = r.run(["sshd", "-T"])[1]
    pw = re.search(r"^passwordauthentication (\w+)", sshd, re.M)
    rep.facts["sshd_password_auth"] = pw.group(1) if pw else None
    keys = r.read("/root/.ssh/authorized_keys") or ""
    rep.facts["root_authorized_keys"] = sum(1 for l in keys.splitlines() if l.strip() and not l.startswith("#"))
    rep.add("root authorized_keys present", rep.facts["root_authorized_keys"] > 0,
            f"{rep.facts['root_authorized_keys']} key(s)", ">= 1 before step 10 disables password auth", "warn")
    rep.add("sshd PasswordAuthentication", (pw.group(1) == "no") if pw else None, pw.group(1) if pw else "unavailable", "no (after step 10)", "info")


def collect_loss_inventory(r: Runner, rep: Report):
    """Everything a reinstall of this host would destroy (spec §3 gate)."""
    inv = {}
    rc, out = r.run(["pct", "list"])
    inv["lxc_containers"] = [l.strip() for l in out.strip().splitlines()[1:]] if rc == 0 else "pct unavailable"
    rc, out = r.run(["qm", "list"])
    inv["virtual_machines"] = [l.strip() for l in out.strip().splitlines()[1:]] if rc == 0 else "qm unavailable"
    rc, out = r.run(["pvesm", "status"])
    inv["storage_pools"] = [l.strip() for l in out.strip().splitlines()[1:]] if rc == 0 else "pvesm unavailable"
    rc, out = r.run(["zfs", "list", "-H", "-o", "name,used"])
    inv["zfs_datasets"] = [l.strip() for l in out.strip().splitlines()] if rc == 0 else "zfs unavailable"
    inv["etc_pve_present"] = r.exists("/etc/pve/nodes")
    inv["backups_in_var_lib_vz_dump"] = r.exists("/var/lib/vz/dump")
    rep.loss_inventory = inv


COLLECTORS: list[Callable[[Runner, Report], None]] = [
    collect_cpu, collect_memory, collect_iommu, collect_gpu, collect_pve, collect_storage, collect_network,
]


def run_preflight(r: Runner | None = None, loss_inventory: bool = True) -> Report:
    r = r or Runner()
    rep = Report()
    for c in COLLECTORS:
        try:
            c(r, rep)
        except Exception as e:  # a collector failing must not hide the others; report it, never invent
            rep.add(c.__name__, None, f"collector error: {e}", "", "warn")
    if loss_inventory:
        collect_loss_inventory(r, rep)
    return rep


def write_report(rep: Report, out_dir: str = "/var/lib/project-pat") -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    j = os.path.join(out_dir, "preflight.json")
    m = os.path.join(out_dir, "preflight.md")
    Path(j).write_text(rep.to_json())
    Path(m).write_text(rep.to_markdown())
    return j, m
