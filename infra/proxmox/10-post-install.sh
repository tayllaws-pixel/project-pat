#!/usr/bin/env bash
# Step 10 — Post-install hardening for Proxmox VE 9.x (PAT-005). Idempotent. Never drops the current SSH session.
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
STEP=10
LAN_CIDR="${LAN_CIDR:-}"           # e.g. 192.168.1.0/24 — auto-detected from vmbr0 if empty
ARC_MAX_BYTES="${ARC_MAX_BYTES:-8589934592}"

detect_lan() { ip -4 -o addr show dev vmbr0 2>/dev/null | awk '{print $4}' | head -1 | python3 -c 'import sys,ipaddress;print(ipaddress.ip_interface(sys.stdin.read().strip()).network)'; }

check() {
  local ok=0
  [[ -f /etc/apt/sources.list.d/pve-no-subscription.sources || -f /etc/apt/sources.list.d/pve-no-subscription.list ]] || ok=1
  ls /etc/apt/sources.list.d/ 2>/dev/null | grep -qi enterprise && { grep -rqE '^\s*Enabled:\s*false' /etc/apt/sources.list.d/*enterprise* 2>/dev/null || ok=1; }
  sshd -T 2>/dev/null | grep -q '^passwordauthentication no' || ok=1
  pkg_installed fail2ban && pkg_installed unattended-upgrades && pkg_installed smartmontools && pkg_installed lm-sensors || ok=1
  grep -qs "zfs_arc_max=${ARC_MAX_BYTES}" /etc/modprobe.d/zfs.conf || ok=1
  [[ -f /etc/pve/firewall/cluster.fw ]] && grep -q '^enable: 1' /etc/pve/firewall/cluster.fw || ok=1
  return $ok
}

apply() {
  require_root; require_pve; ensure_dirs
  local pve_major; pve_major="$(pveversion | sed -E 's|pve-manager/([0-9]+).*|\1|')"
  [[ "$pve_major" -ge 9 ]] || die "expected Proxmox VE 9.x, found $(pveversion)"
  local codename; codename="$(. /etc/os-release; echo "$VERSION_CODENAME")"

  log "repositories (deb822 format, PVE 9)"
  for f in /etc/apt/sources.list.d/pve-enterprise.sources /etc/apt/sources.list.d/ceph.sources; do
    [[ -f "$f" ]] && ! grep -qE '^\s*Enabled:\s*false' "$f" && { backup_file "$f"; printf '\nEnabled: false\n' >> "$f"; }
  done
  if [[ ! -f /etc/apt/sources.list.d/pve-no-subscription.sources ]]; then
    cat > /etc/apt/sources.list.d/pve-no-subscription.sources <<EOT
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: ${codename}
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOT
  fi
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y -qq
  apt_install fail2ban unattended-upgrades apt-listchanges smartmontools lm-sensors chrony sudo curl gnupg python3 git rsync

  log "unattended security upgrades (Debian security only; PVE packages are upgraded by the runbook)"
  cat > /etc/apt/apt.conf.d/52project-pat-unattended <<'EOT'
Unattended-Upgrade::Origins-Pattern { "origin=Debian,codename=${distro_codename}-security,label=Debian-Security"; };
Unattended-Upgrade::Package-Blacklist { "proxmox-kernel-.*"; "pve-.*"; "nvidia-.*"; };
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOT

  log "sshd: key-only (guarded)"
  if [[ -s /root/.ssh/authorized_keys ]]; then
    mkdir -p /etc/ssh/sshd_config.d
    cat > /etc/ssh/sshd_config.d/10-project-pat.conf <<'EOT'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 4
ClientAliveInterval 60
ClientAliveCountMax 5
EOT
    sshd -t && systemctl reload ssh   # reload keeps existing sessions alive
  else
    warn "/root/.ssh/authorized_keys is empty — NOT disabling password auth. Add your key and re-run."
  fi

  log "fail2ban for sshd + proxmox web"
  cat > /etc/fail2ban/jail.d/project-pat.local <<'EOT'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
[proxmox]
enabled = true
port = https,http,8006
filter = proxmox
backend = systemd
maxretry = 4
bantime = 1h
EOT
  cat > /etc/fail2ban/filter.d/proxmox.conf <<'EOT'
[Definition]
failregex = pvedaemon\[.*authentication failure; rhost=<HOST> user=.* msg=.*
ignoreregex =
journalmatch = _SYSTEMD_UNIT=pvedaemon.service
EOT
  systemctl enable --now fail2ban >/dev/null; systemctl restart fail2ban

  log "ZFS ARC cap ${ARC_MAX_BYTES} bytes"
  backup_file /etc/modprobe.d/zfs.conf
  printf 'options zfs zfs_arc_max=%s\n' "$ARC_MAX_BYTES" > /etc/modprobe.d/zfs.conf
  echo "$ARC_MAX_BYTES" > /sys/module/zfs/parameters/zfs_arc_max 2>/dev/null || true
  update-initramfs -u -k all >/dev/null 2>&1 || true

  log "Proxmox firewall: datacenter default DROP inbound, allow LAN + tailnet to 22/8006 (SSH session preserved: rules allow established)"
  [[ -n "$LAN_CIDR" ]] || LAN_CIDR="$(detect_lan)"
  [[ -n "$LAN_CIDR" ]] || die "could not detect LAN CIDR from vmbr0; set LAN_CIDR=… and re-run"
  mkdir -p /etc/pve/firewall
  if [[ ! -f /etc/pve/firewall/cluster.fw ]] || ! grep -q 'project-pat' /etc/pve/firewall/cluster.fw; then
    backup_file /etc/pve/firewall/cluster.fw
    cat > /etc/pve/firewall/cluster.fw <<EOT
# managed by project-pat step 10
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[IPSET lan]
${LAN_CIDR}

[IPSET tailnet]
100.64.0.0/10

[IPSET lab]
10.77.0.0/24

[RULES]
IN ACCEPT -source +lan -p tcp -dport 22 -log nolog
IN ACCEPT -source +lan -p tcp -dport 8006 -log nolog
IN ACCEPT -source +tailnet -p tcp -dport 22 -log nolog
IN ACCEPT -source +tailnet -p tcp -dport 8006 -log nolog
IN ACCEPT -p icmp -log nolog
IN ACCEPT -i tailscale0 -log nolog
EOT
  fi
  cat > /etc/pve/nodes/"$(hostname)"/host.fw <<'EOT'
[OPTIONS]
enable: 1
EOT
  pve-firewall restart
  pve-firewall status | grep -q running || die "pve-firewall not running"

  log "sensors + smart"
  sensors-detect --auto >/dev/null 2>&1 || true
  systemctl enable --now smartd >/dev/null 2>&1 || true
  mark_done $STEP; log "step $STEP applied"
}

if [[ "$MODE" == check ]]; then check && { log "step $STEP: satisfied"; exit 0; } || { log "step $STEP: pending"; exit 1; }; fi
apply
