# ADR-0002: Proxmox install choices — ZFS root, Secure Boot off, 9.x no-subscription repo

Status: accepted (2026-10-25)

## Decisions
- **Filesystem: ZFS (single-disk `rpool`)** instead of the default ext4/LVM-thin. Gains: cheap snapshots
  for the hourly local-recovery mechanism, checksums (bit-rot detection on a consumer NVMe), lz4
  compression, `zfs send` for backups. Cost: ARC memory — capped at 8 GB via
  `options zfs zfs_arc_max=8589934592`. A single disk gives *detection* of corruption, not
  *repair*; off-box backups remain mandatory.
- **Secure Boot: disabled** in BIOS. Proxmox can boot with Secure Boot, but out-of-tree NVIDIA
  modules would then require enrolling a MOK and re-signing after every DKMS build — avoidable
  operational burden on a home server administered from a phone.
- **Repository:** `pve-no-subscription`. Enterprise repo removed; the nag dialog is left alone
  (patching the UI JS is fragile across upgrades).
- **Kernel:** whatever 9.x ships; after the NVIDIA driver is built, `proxmox-kernel-*` is `apt-mark hold`
  until the kernel-upgrade runbook (`docs/RUNBOOK-kernel-upgrade.md`) is run.
- **Swap:** ZFS install creates none. A 8 GB zvol swap is *not* created (swap on ZFS is a known
  deadlock risk). Memory is instead governed by guest caps (ADR-0003).
- **Hostname/domain:** `pve.home.arpa` (RFC 8375). Static IP via DHCP reservation on the RS180,
  not a static config on the host, so the router stays the single source of truth for addressing.
