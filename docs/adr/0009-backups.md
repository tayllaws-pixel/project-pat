# ADR-0009: Backups — ZFS snapshots locally, restic to the desktop weekly, verified restores

Status: accepted (2026-10-25)

- Local recovery: `zfs-auto-snapshot`-style timer (`infra/proxmox/60-zfs-snapshots.sh`): hourly×48, daily×14,
  weekly×8 for `rpool/data`. Covers accidental deletion and bad deploys; does not cover disk death.
- Off-box weekly: `restic` repo on the desktop over SSH (`sftp:` backend). Contents: vzdump of CT 100 (DB),
  `pg_dump` of each database, `/etc/project-pat`, git repos, approval/audit exports, Proxmox `/etc/pve`.
  Encrypted with a restic key stored in Sentinel's secrets and printed once for the admin to store offline.
- Availability detection: systemd timer every 2 h checks the desktop (ping + ssh); runs pending jobs; a job
  is marked successful only after `restic check --read-data-subset=10%` passes and a `pg_restore --list`
  of the dump succeeds. Status is reported to Pat; "last verified backup" is shown in the dashboard.
- Monthly restore drill: restore latest dump into a scratch database and run the functional test suite.
- Future: Proxmox Backup Server on dedicated hardware replaces the restic job.
