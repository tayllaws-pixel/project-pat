"""Fake Runner that answers from canned command output / files. Shared by preflight and installer tests."""
from __future__ import annotations


class FakeRunner:
    def __init__(self, cmds: dict[str, tuple[int, str]] | None = None, files: dict[str, str] | None = None):
        self.cmds = cmds or {}
        self.files = files or {}
        self.calls: list[list[str]] = []

    def run(self, cmd, timeout=20):
        self.calls.append(list(cmd))
        key = " ".join(cmd)
        for k, v in self.cmds.items():
            if key == k or key.startswith(k + " ") or (k.endswith("*") and key.startswith(k[:-1])):
                return v
        return 127, ""

    def read(self, path):
        return self.files.get(path)

    def exists(self, path):
        return path in self.files or path in getattr(self, "dirs", set())


def healthy_host() -> FakeRunner:
    """A fresh PVE 9 ZFS install on the spec hardware, after step 10, before step 20."""
    return FakeRunner(
        cmds={
            "dmesg": (0, "[0.1] DMAR: IOMMU enabled\n[0.2] DMAR: Intel(R) Virtualization Technology for Directed I/O\n"),
            "lspci -nn": (0, "00:02.0 VGA compatible controller [0300]: Intel Corporation Arrow Lake-S [Intel Graphics] [8086:7d67]\n"
                             "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation AD103 [GeForce RTX 4070 Ti SUPER] [10de:2705]\n"),
            "nvidia-smi": (127, ""),
            "pveversion": (0, "pve-manager/9.1.1/abcdef (running kernel: 6.14.11-2-pve)\n"),
            "uname -r": (0, "6.14.11-2-pve\n"),
            "apt-mark showhold": (0, ""),
            "zpool status -x": (0, "all pools are healthy\n"),
            "zpool list": (0, "rpool\t1.81T\t12.3G\t1.80T\tONLINE\n"),
            "lsblk": (0, '{"blockdevices":[{"name":"nvme0n1","size":"1.8T","type":"disk","model":"Samsung SSD 990 PRO 2TB","mountpoint":null,"fstype":null}]}'),
            "smartctl --scan": (0, "/dev/nvme0 -d nvme\n"),
            "ip -j -4 addr": (0, '[{"ifname":"lo","addr_info":[{"local":"127.0.0.1","prefixlen":8}]},'
                                 '{"ifname":"vmbr0","addr_info":[{"local":"192.168.1.50","prefixlen":24}]}]'),
            "ethtool vmbr0": (1, ""),
            "sshd -T": (0, "passwordauthentication no\npermitrootlogin prohibit-password\n"),
            "pct list": (0, "VMID Status Lock Name\n"),
            "qm list": (0, "VMID NAME STATUS\n"),
            "pvesm status": (0, "Name Type Status\nlocal dir active\nlocal-zfs zfspool active\n"),
            "zfs list": (0, "rpool\t12.3G\nrpool/ROOT\t4.1G\nrpool/data\t96K\n"),
        },
        files={
            "/proc/cpuinfo": "processor\t: 0\nmodel name\t: Intel(R) Core(TM) Ultra 7 265K\nflags\t: fpu vmx sse\n" + "".join(f"processor\t: {i}\n" for i in range(1, 20)),
            "/proc/meminfo": "MemTotal:       65432100 kB\n",
            "/proc/cmdline": "BOOT_IMAGE=/vmlinuz root=ZFS=rpool/ROOT/pve-1 quiet\n",
            "/proc/modules": "zfs 1 0 - Live\n",
            "/sys/module/zfs/parameters/zfs_arc_max": "8589934592\n",
            "/root/.ssh/authorized_keys": "ssh-ed25519 AAAA taylor\n",
            "/etc/pve/nodes": "",
        },
    )
