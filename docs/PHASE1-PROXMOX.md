# Phase 1 — Fresh Proxmox VE install and secure initial configuration

Hardware: Intel Core Ultra 7 265K · ASUS Pro WS Z890-ACE SE · 64 GB DDR5 · RTX 4070 Ti SUPER · 2 TB NVMe.

> **Destructive-operation gate (spec §3).** §2 below erases the 2 TB NVMe. Before doing it:
> 1. If the old Proxmox is still bootable, run `python3 -m infra.preflight --inventory-loss` on it. It prints every
>    VM/CT, storage pool, and `/etc/pve` config that will be destroyed and writes the report to a USB stick or
>    your desktop.
> 2. Decide what, if anything, to keep (old conversation DBs? media?) and copy it off the box.
> 3. Reply in chat with the exact words **"erase the 2 TB NVMe"** — I will not treat "fresh start" as that consent.

## 1. BIOS / UEFI checklist (ASUS Pro WS Z890-ACE SE)

Press **Del** at POST → **Advanced Mode (F7)**. Menu names are from ASUS's Z890 firmware family; if a label
differs on your BIOS version, the *setting* is what matters.

| Setting | Path (Advanced Mode) | Value | Why |
|---|---|---|---|
| Intel (VMX) Virtualization Technology | Advanced → CPU Configuration | **Enabled** | KVM |
| Intel VT-d | Advanced → System Agent (SA) Configuration | **Enabled** | IOMMU — required for any future PCI passthrough and for proper device isolation |
| Above 4G Decoding | Advanced → PCI Subsystem Settings | **Enabled** | Large-BAR GPUs |
| Re-Size BAR Support | Advanced → PCI Subsystem Settings | **Auto/Enabled** | GPU performance |
| Secure Boot | Boot → Secure Boot → OS Type | **Other OS** (= Secure Boot off) | NVIDIA DKMS modules (ADR-0002) |
| CSM | Boot → CSM | **Disabled** | Pure UEFI, needed for ZFS boot via systemd-boot/proxmox-boot-tool |
| Fast Boot | Boot | **Disabled** | Lets you reach the boot menu / USB installer reliably |
| Primary Display | Advanced → System Agent → Graphics Configuration | **CPU Graphics** (iGPU) | Keeps the NVIDIA card free of console duty; host console comes out of the motherboard HDMI/DP |
| iGPU Multi-Monitor | same | **Enabled** | Keeps the iGPU initialized for VA-API/QSV in Jellyfin/Frigate later |
| XMP / Intel XMP profile | Ai Tweaker → Ai Overclock Tuner | **XMP I** (DDR5-6000 or the kit's rating) | Only if stable; if the host ever reboots unexpectedly, set back to Auto first |
| Restore AC Power Loss | Advanced → APM Configuration | **Power On** | 24/7 server comes back after an outage |
| ErP Ready | Advanced → APM | **Disabled** | Keeps Wake-on-LAN possible |
| Wake on LAN (PCIe/onboard) | Advanced → APM / Onboard Devices | **Enabled** | Remote wake |
| SATA/NVMe mode | Advanced → Storage / VMD | **AHCI**, **VMD disabled** | Intel VMD hides NVMe from Linux installers |
| Fan profile | Monitor → Q-Fan | **Standard** (or a custom curve starting ~30% at 40 °C) | Quiet 24/7; the Phantom Spirit 120 SE handles the 265K at stock |
| Boot order | Boot | USB installer first, then the NVMe | — |

Save (F10) and reboot into the installer.

### Why not "Intel Default Settings"/performance presets?
Leave the CPU at stock power limits (PL1/PL2 defaults). A 24/7 server with a 120 mm air cooler should not run
boosted presets; the 265K is fast enough at stock and runs cooler.

## 2. Proxmox VE 9.x installation (console, ~10 minutes)

1. Download the current **Proxmox VE 9.x ISO** from proxmox.com/downloads and write it to a USB stick
   (Balena Etcher / `dd`). Verify the SHA-256 shown on the download page.
2. Boot the stick → **Install Proxmox VE (Graphical)**.
3. **Target disk:** click *Options* → Filesystem **zfs (RAID0)** (single disk) · ashift 12 · compress **lz4** ·
   checksum on · copies 1 · hdsize: leave default (whole disk). ARC max: set **8 GB** if the installer offers it
   (9.x does); otherwise the post-install script sets it.
4. Country/timezone **America/New_York**, keyboard **en-us**.
5. Root password: long, unique; you will switch to key-only SSH in §3, but this password is still the PVE web
   login until PAT-028 — put it in your password manager.
6. Email: your real address (ZFS/SMART alerts go here until the observe stack exists).
7. **Network:** interface = the onboard 1 GbE (the Realtek/Intel port — pick the one that shows a link),
   hostname **`pve.home.arpa`**, IP: pick a free LAN IP *and reserve it on the RS180 for this MAC*
   (Router → Attached Devices → Reserve). Gateway/DNS: the router's IP.
8. Install → reboot → remove the stick. Log in at `https://<ip>:8006` (accept the self-signed cert) to
   confirm it is up, then do everything else from SSH.

## 3. First SSH session — bootstrap (copy-paste, one block)

From your phone/desktop SSH client, as root with the password you set:

```bash
apt-get update -qq && apt-get install -y -qq git python3 >/dev/null
git clone https://github.com/<you>/project-pat.git /opt/project-pat   # or scp the archive and tar -xzf
cd /opt/project-pat
python3 -m infra.preflight
```

Read the report (`/var/lib/project-pat/preflight.md`). It must show: PVE 9.x, ZFS rpool ONLINE, 64 GB RAM,
VT-d active (`iommu: enabled`), NVIDIA device present, link up on vmbr0. If any line is ✗, stop and send me
the report.

Then put your SSH public key in place **before** the hardening step disables password login:

```bash
mkdir -p /root/.ssh && chmod 700 /root/.ssh
cat >> /root/.ssh/authorized_keys <<'KEY'
ssh-ed25519 AAAA...your-public-key... taylor-iphone
KEY
chmod 600 /root/.ssh/authorized_keys
```

Open a **second** SSH session and confirm key login works (`ssh -o PasswordAuthentication=no root@pve`) before
continuing. `10-post-install.sh` refuses to disable password auth if `authorized_keys` is empty.

## 4. Run Phase 1 through the installer

```bash
cd /opt/project-pat
python3 -m infra.installer plan --phase 1          # lists steps 10..60 and current state
python3 -m infra.installer install --phase 1       # non-destructive steps run; it will stop before 20-nvidia (reboot needed)
```

Steps and what each does (all in `infra/proxmox/`, each safe to re-run):

| Step | Script | Effect | Reboot? |
|---|---|---|---|
| 10 | `10-post-install.sh` | no-subscription repo, full update, unattended security upgrades, sshd key-only (keeps the live session), fail2ban, PVE firewall: inbound default DROP except LAN + tailnet for 22/8006, ZFS ARC cap, email alerts, `smartmontools`, `lm-sensors` | no |
| 15 | `15-tailscale.sh` | installs Tailscale and prints the auth URL; you approve it on your phone | no |
| 20 | `20-nvidia-host.sh` | blacklists nouveau, installs headers + build tools, NVIDIA `.run` driver with `--dkms`, module autoload, `apt-mark hold proxmox-kernel-*`, udev rule for `/dev/nvidia-uvm` | **yes** |
| 40 | `40-network.sh` | adds `vmbr1` (10.77.0.1/24) with NAT masquerade and firewall rules that block vmbr1→LAN | no (ifreload) |
| 60 | `60-zfs-snapshots.sh` | creates `rpool/data` snapshot timers (hourly/daily/weekly) | no |

After step 20 the installer prints `reboot required`. Run `reboot`, reconnect, and run
`python3 -m infra.installer install --phase 1` again — it resumes at the first unsatisfied step.

## 5. Phase 1 acceptance (PAT-013)

```bash
python3 -m infra.installer verify --phase 1
```
Checks performed (each prints measured values, never assumptions): `nvidia-smi` sees the GPU; `zpool status`
ONLINE; ARC max ≤ 8 GB; `sshd -T` shows `passwordauthentication no`; `pve-firewall status` running; port 8006
not reachable from vmbr1; Tailscale connected; snapshot timer active; `sensors` and `smartctl -H` return data
(or are reported as unavailable, not invented).

## 6. Rollback / recovery for Phase 1
- Every host script writes a timestamped backup of each file it edits to `/var/lib/project-pat/backups/`.
- `python3 -m infra.installer rollback --step 20` removes the NVIDIA driver and un-holds the kernel.
- If SSH is lost: console login at the box still works (password auth is disabled for SSH only).
- If networking is broken by step 40: `ifdown vmbr1; cp /var/lib/project-pat/backups/interfaces.* /etc/network/interfaces; ifreload -a`.

## 7. Kernel upgrades after the NVIDIA driver is installed
See `docs/RUNBOOK-kernel-upgrade.md` (created with step 20). Short version: `apt-mark unhold`, upgrade kernel,
reboot, confirm `dkms status` rebuilt the module, re-hold.
