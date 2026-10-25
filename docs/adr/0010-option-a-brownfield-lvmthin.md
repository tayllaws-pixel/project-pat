# ADR-0010: Option A — brownfield build on the existing Proxmox install (LVM-thin, driver reuse)

Status: accepted (2026-10-25). Supersedes the ZFS assumption in ADR-0002 and the driver-install plan in ADR-0001, for this deployment.

## Context
On first login the server was already running Proxmox VE **9.2.2** (kernel 7.0.2-6-pve) from an earlier
ChatGPT-built stack, on a single 1.8 TB NVMe partitioned as **LVM-thin** (`local` dir + `local-lvm` thinpool),
**not** ZFS. The NVIDIA driver (**615.71.09**) was already installed on the host and working, with the GPU
kernel modules autoloaded via `/etc/modules-load.d/nvidia.conf`. Taylor chose to keep this install and clear
only the old AI stack (Option A), rather than reinstall Proxmox to get ZFS (Option B).

The old stack was inventoried, its ~19 GB of Ollama models and code archived, and then CT 100/101, VM 102 and
all `wade-*` host services were destroyed (see docs/STATUS.md teardown log). The host is now clean.

## Decisions
1. **Storage: LVM-thin.** Guest root/data disks go on `local-lvm` (`DISK_STORAGE = "local-lvm"`); templates and
   the models backup live on `local` (`/var/lib/vz`). ZFS-specific steps (ARC cap, `zfs` snapshot timers) are
   **dropped** for this host.
2. **Local recovery snapshots via LVM-thin.** Instead of ZFS snapshots (ADR-0009), use `lvcreate -s` thin
   snapshots of the guest volumes on a timer, plus Proxmox `vzdump` of the core guest (CT holding Postgres).
   LVM-thin snapshots are writable/space-efficient and cover the accidental-deletion / bad-deploy case; they do
   not give ZFS's checksums, so off-box `restic` backups (ADR-0009) remain mandatory. A second SSD or a move to
   ZFS/PBS is still the recommended future upgrade for data integrity.
3. **Reuse the existing NVIDIA driver (615.71.09).** Do **not** run the `.run` DKMS install (old PAT-007). The
   host driver and module autoload already work. Installer step `20-nvidia-host` is treated as satisfied; what
   remains is: (a) record `615.71.09` in `/etc/project-pat/nvidia-version`, (b) ensure the kernel is held so an
   upgrade can't desync the driver, (c) for GPU-sharing into the inference LXC, install the **matching** userspace
   driver (615.71.09, `--no-kernel-modules`) inside the container. The `.run` for 615.71.09 is fetched once to
   `/var/lib/project-pat/` for the container-side userspace install.
4. **Restore models instead of re-downloading.** The new inference container imports the 19 GB of qwen3 blobs
   from `/var/lib/vz/ollama-backup` (saved during teardown) into its Ollama models dir, rather than pulling them
   again. These are the same qwen3 family the router already targets (ADR-0006), so they are directly reusable.
5. **Guest IDs 100–106, 110** are free again after teardown and are used as planned (ADR-0003).

## Consequences
- `infra/proxmox/20-nvidia-host.sh` and `60-zfs-snapshots.sh` are not run on this host; a new
  `61-lvm-snapshots.sh` provides the local-recovery timers, and the inference build does the container-side
  userspace driver install keyed to the recorded version.
- `patctl` Phase 1 on this host is effectively just `10-post-install` (hardening, firewall, unattended security
  updates) + `15-tailscale` (already present — Tailscale is up) + `40-network` (LAB bridge for forge). The heavy
  driver/kernel work is skipped.
- Everything downstream (guests, Postgres, Sentinel, inference, Pat, Wade) is unchanged by this ADR.
