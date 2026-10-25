"""Concrete installer steps. Phase 1 wraps the numbered host scripts; Phase 2+ provisions guests from TOML."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .engine import Step
from .host import Host

ETC = "/etc/project-pat"
TEMPLATE_STORAGE = "local"
DISK_STORAGE = "local-zfs"


# ---------------------------------------------------------------- guests
def load_guests(repo_root: str) -> list[dict]:
    out = []
    for f in sorted(Path(repo_root, "infra/guests").glob("*.toml")):
        g = tomllib.loads(f.read_text())
        g.setdefault("tags", [])
        g.setdefault("features", [])
        g.setdefault("phase", 2)
        out.append(g)
    ids = [g["id"] for g in out]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate guest ids in infra/guests: {ids}")
    return out


def bridge_for(network: str) -> str:
    return {"lan": "vmbr0", "lab": "vmbr1"}[network]


def pct_create_args(g: dict, template_path: str) -> list[str]:
    net = f"name=eth0,bridge={bridge_for(g['network'])},ip=dhcp,firewall=1"
    args = ["pct", "create", str(g["id"]), template_path,
            "--hostname", g["name"], "--cores", str(g["cores"]), "--memory", str(g["memory"]),
            "--swap", str(g.get("swap", 0)), "--rootfs", f"{DISK_STORAGE}:{g['disk_gb']}",
            "--net0", net, "--unprivileged", "1" if g.get("unprivileged", True) else "0",
            "--onboot", "1", "--start", "0", "--tags", ";".join(g["tags"]),
            "--description", g.get("description", "")]
    if g["features"]:
        args += ["--features", ",".join(g["features"])]
    if g.get("cpuunits"):
        args += ["--cpuunits", str(g["cpuunits"])]
    if g.get("startup"):
        args += ["--startup", g["startup"]]
    if g.get("pool"):
        args += ["--pool", g["pool"]]
    return args


def qm_create_args(g: dict) -> list[str]:
    """Cloud-image VM. Disk import happens in a second command (qm importdisk) handled by the step."""
    net = f"virtio,bridge={bridge_for(g['network'])},firewall=1"
    args = ["qm", "create", str(g["id"]), "--name", g["name"], "--cores", str(g["cores"]), "--memory", str(g["memory"]),
            "--cpu", "host", "--machine", "q35", "--bios", "ovmf", "--efidisk0", f"{DISK_STORAGE}:0,efitype=4m",
            "--scsihw", "virtio-scsi-single", "--net0", net, "--agent", "enabled=1", "--onboot", "1",
            "--serial0", "socket", "--vga", "serial0", "--tags", ";".join(g["tags"]),
            "--description", g.get("description", "")]
    if g.get("balloon") is not None:
        args += ["--balloon", str(g["balloon"])]
    if g.get("cpulimit"):
        args += ["--cpulimit", str(g["cpulimit"])]
    if g.get("cpuunits"):
        args += ["--cpuunits", str(g["cpuunits"])]
    if g.get("pool"):
        args += ["--pool", g["pool"]]
    return args


def guest_firewall(g: dict, lan_cidr_ipset: str = "+lan") -> str:
    """Per-guest firewall. LAB guests may not talk to the LAN or tailnet; everyone drops inbound except from LAN/tailnet."""
    rules = ["[OPTIONS]", "enable: 1", "policy_in: DROP", "policy_out: ACCEPT", "", "[RULES]"]
    if g["network"] == "lab":
        rules += [f"OUT DROP -dest {lan_cidr_ipset} -log warning",
                  "OUT DROP -dest +tailnet -log warning",
                  "OUT DROP -dest 10.77.0.1 -p tcp -dport 8006 -log warning",   # host UI via the bridge IP
                  "OUT DROP -dest 10.77.0.1 -p tcp -dport 22 -log warning"]
    rules += ["IN ACCEPT -source +lan -log nolog", "IN ACCEPT -source +tailnet -log nolog", "IN ACCEPT -p icmp -log nolog"]
    if g["network"] == "lab":
        rules += ["IN ACCEPT -source +lan -log nolog"]  # Wade/Sentinel reach forge over SSH from the LAN side
    return "\n".join(rules) + "\n"


# ---------------------------------------------------------------- step factory
def build_steps(host: Host, repo_root: str, nvidia_version: str | None = None) -> list[Step]:
    steps: list[Step] = []

    # ---- Phase 1 (host) ----
    steps.append(Step("10-post-install", 1, "Post-install hardening (repos, updates, sshd key-only, fail2ban, firewall, ARC cap)",
                      check=lambda: host.script_check("10-post-install.sh"),
                      apply=lambda: host.script("10-post-install.sh")))
    steps.append(Step("15-tailscale", 1, "Tailscale on host (auth URL printed; approve on phone)",
                      check=lambda: host.script_check("15-tailscale.sh"),
                      apply=lambda: host.script("15-tailscale.sh"), depends=["10-post-install"]))
    steps.append(Step("20-nvidia-host", 1, f"NVIDIA host driver via DKMS (version {nvidia_version or 'UNSET — pass --nvidia-version'}) + kernel hold",
                      check=lambda: host.script_check("20-nvidia-host.sh"),
                      apply=lambda: _apply_nvidia(host, nvidia_version),
                      rollback=lambda: _rollback_nvidia(host), depends=["10-post-install"], reboot_after=True))
    steps.append(Step("40-network", 1, "LAB bridge vmbr1 (10.77.0.0/24) with NAT; LAN isolation via per-guest firewall",
                      check=lambda: host.script_check("40-network.sh"),
                      apply=lambda: host.script("40-network.sh"),
                      rollback=lambda: _rollback_network(host), depends=["10-post-install"]))
    steps.append(Step("60-zfs-snapshots", 1, "Local ZFS snapshot timers (hourly×48, daily×14, weekly×8)",
                      check=lambda: host.script_check("60-zfs-snapshots.sh"),
                      apply=lambda: host.script("60-zfs-snapshots.sh"), depends=["10-post-install"]))

    # ---- Phase 2+: secrets, pool, templates, guests ----
    steps.append(Step("secrets", 2, "Generate per-service secrets under /etc/project-pat (0600); never stored in repo",
                      check=lambda: host.exists(f"{ETC}/secrets.env"),
                      apply=lambda: _gen_secrets(host)))
    steps.append(Step("pool-autonomous", 2, "Resource pool 'autonomous' (Wade + forge) — the only pool Sentinel's token may manage",
                      check=lambda: "autonomous" in host.probe(["pvesh", "get", "/pools", "--output-format", "json"])[1],
                      apply=lambda: host.run(["pvesh", "create", "/pools", "--poolid", "autonomous",
                                              "--comment", "Autonomous AI workloads; Sentinel may stop/start these"])))
    steps.append(Step("ct-template", 2, "Download Debian 13 LXC template",
                      check=lambda: _template_path(host) is not None,
                      apply=lambda: _download_template(host)))

    guests = load_guests(repo_root)
    for g in guests:
        gid = str(g["id"])
        if g["kind"] == "lxc":
            steps.append(Step(f"guest-{gid}", g["phase"], f"CT {gid} {g['name']}: {g['description'][:60]}",
                              check=lambda gid=gid: host.probe(["pct", "config", gid])[0] == 0,
                              apply=lambda g=g: _create_ct(host, g),
                              depends=["ct-template", "secrets"] + (["pool-autonomous"] if g.get("pool") else []) +
                                      (["40-network"] if g["network"] == "lab" else []),
                              rollback=lambda gid=gid: _destroy_guest(host, "pct", gid), destructive=False))
            if g.get("gpu"):
                steps.append(Step(f"gpu-{gid}", g["phase"], f"Share NVIDIA GPU with CT {gid} (matching userspace driver inside)",
                                  check=lambda gid=gid: host.script_check("30-lxc-gpu.sh", gid),
                                  apply=lambda gid=gid: host.script("30-lxc-gpu.sh", gid),
                                  depends=[f"guest-{gid}", "20-nvidia-host"]))
        else:
            steps.append(Step(f"guest-{gid}", g["phase"], f"VM {gid} {g['name']}: {g['description'][:60]}",
                              check=lambda gid=gid: host.probe(["qm", "config", gid])[0] == 0,
                              apply=lambda g=g: _create_vm(host, g),
                              depends=["secrets"] + (["pool-autonomous"] if g.get("pool") else []) +
                                      (["40-network"] if g["network"] == "lab" else []),
                              rollback=lambda gid=gid: _destroy_guest(host, "qm", gid)))
        steps.append(Step(f"fw-{gid}", g["phase"], f"Per-guest firewall for {gid} ({'LAB-isolated' if g['network'] == 'lab' else 'LAN'})",
                          check=lambda g=g: host.read(f"/etc/pve/firewall/{g['id']}.fw") == guest_firewall(g),
                          apply=lambda g=g: host.write(f"/etc/pve/firewall/{g['id']}.fw", guest_firewall(g)),
                          depends=[f"guest-{gid}"]))

    # Explicit destructive step: wipe + recreate a guest. Only reachable by id with --i-understand.
    for g in guests:
        gid = str(g["id"])
        tool = "pct" if g["kind"] == "lxc" else "qm"
        steps.append(Step(f"recreate-{gid}", 99, f"DESTROY and recreate guest {gid} ({g['name']}) — all its data is lost",
                          check=lambda: False, destructive=True,
                          apply=lambda g=g, tool=tool: (_destroy_guest(host, tool, str(g["id"])),
                                                        _create_ct(host, g) if tool == "pct" else _create_vm(host, g))))
    return steps


# ---------------------------------------------------------------- helpers
def _template_path(host: Host) -> str | None:
    rc, out = host.probe(["pveam", "list", TEMPLATE_STORAGE])
    m = re.search(r"(local:vztmpl/debian-13-standard_[^\s]+)", out) if rc == 0 else None
    return m.group(1) if m else None


def _download_template(host: Host):
    host.run(["pveam", "update"])
    rc, out = host.run(["pveam", "available", "--section", "system"])
    m = re.search(r"(debian-13-standard_[^\s]+)", out)
    if not m:
        raise RuntimeError("no debian-13-standard template in pveam available")
    host.run(["pveam", "download", TEMPLATE_STORAGE, m.group(1)])


def _create_ct(host: Host, g: dict):
    tpl = _template_path(host) or f"{TEMPLATE_STORAGE}:vztmpl/debian-13-standard.tar.zst"
    host.run(pct_create_args(g, tpl))


def _create_vm(host: Host, g: dict):
    host.run(qm_create_args(g))
    img = f"/var/lib/vz/template/iso/{g['image']}.qcow2"
    if not host.exists(img):
        host.run(["curl", "-fL", f"https://cloud.debian.org/images/cloud/trixie/latest/{g['image']}.qcow2", "-o", img])
    host.run(["qm", "importdisk", str(g["id"]), img, DISK_STORAGE])
    host.run(["qm", "set", str(g["id"]), "--scsi0", f"{DISK_STORAGE}:vm-{g['id']}-disk-1,discard=on,ssd=1",
              "--boot", "order=scsi0", "--ide2", f"{DISK_STORAGE}:cloudinit", "--ipconfig0", "ip=dhcp",
              "--ciuser", "forge", "--sshkeys", f"{ETC}/forge_authorized_keys"])
    host.run(["qm", "resize", str(g["id"]), "scsi0", f"{g['disk_gb']}G"])


def _destroy_guest(host: Host, tool: str, gid: str):
    host.run([tool, "stop", gid], check=False)
    host.run([tool, "destroy", gid, "--purge", "1"] if tool == "pct" else [tool, "destroy", gid, "--purge", "1", "--destroy-unreferenced-disks", "1"])


def _gen_secrets(host: Host):
    keys = ["POSTGRES_SUPERUSER_PASSWORD", "PAT_DB_PASSWORD", "WADE_DB_PASSWORD", "SENTINEL_DB_PASSWORD",
            "PAT_SESSION_SECRET", "INFERENCE_SHARED_TOKEN", "NTFY_TOKEN", "RESTIC_PASSWORD"]
    body = "# generated by patctl — do not commit. Rotate with `patctl repair --step secrets` after deleting this file.\n"
    body += "".join(f"{k}={host.new_secret()}\n" for k in keys)
    host.write(f"{ETC}/secrets.env", body, mode=0o600)


def _apply_nvidia(host: Host, version: str | None):
    if not version:
        raise RuntimeError("NVIDIA_VERSION not set: pass --nvidia-version <production branch> (see infra/proxmox/20-nvidia-host.sh header)")
    host.script("20-nvidia-host.sh", env={"NVIDIA_VERSION": version})


def _rollback_nvidia(host: Host):
    host.run(["bash", "-c", "nvidia-uninstall --silent || true"])
    host.run(["bash", "-c", "apt-mark unhold $(apt-mark showhold | grep -E 'proxmox-(default-)?kernel') || true"])
    host.run(["rm", "-f", "/etc/modules-load.d/nvidia.conf", "/etc/udev/rules.d/70-nvidia.rules", "/etc/modprobe.d/blacklist-nouveau.conf"])
    host.run(["update-initramfs", "-u", "-k", "all"])


def _rollback_network(host: Host):
    host.run(["bash", "-c", "ifdown vmbr1 || true"])
    host.run(["bash", "-c", "python3 - <<'PY'\nimport re,pathlib\np=pathlib.Path('/etc/network/interfaces');s=p.read_text()\n"
                            "s=re.sub(r'\\n# project-pat: isolated LAB bridge.*?(?=\\n\\S|\\Z)', '', s, flags=re.S)\np.write_text(s)\nPY"])
    host.run(["ifreload", "-a"])
