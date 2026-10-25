#!/usr/bin/env bash
# Step 40 — LAB bridge vmbr1 (10.77.0.0/24, NAT to WAN, no LAN access) for the forge VM (PAT-008, ADR-0003).
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
STEP=40
LAB_CIDR="10.77.0.1/24"; LAB_NET="10.77.0.0/24"
check() { grep -q '^auto vmbr1' /etc/network/interfaces && ip link show vmbr1 >/dev/null 2>&1; }
apply() {
  require_root; require_pve; ensure_dirs
  local wan_if; wan_if="$(ip -o -4 route show default | awk '{print $5}' | head -1)"
  if ! grep -q '^auto vmbr1' /etc/network/interfaces; then
    backup_file /etc/network/interfaces
    cat >> /etc/network/interfaces <<EOT

# project-pat: isolated LAB bridge for Wade's forge VM — NAT only, LAN blocked by pve-firewall
auto vmbr1
iface vmbr1 inet static
        address ${LAB_CIDR}
        bridge-ports none
        bridge-stp off
        bridge-fd 0
        post-up   echo 1 > /proc/sys/net/ipv4/ip_forward
        post-up   iptables -t nat -A POSTROUTING -s '${LAB_NET}' -o ${wan_if} -j MASQUERADE
        post-down iptables -t nat -D POSTROUTING -s '${LAB_NET}' -o ${wan_if} -j MASQUERADE
EOT
  fi
  ifreload -a
  # LAN isolation for guests on vmbr1 is enforced per-guest (legacy iptables firewall filters bridged traffic at the
  # guest level): the installer writes /etc/pve/firewall/<vmid>.fw with OUT DROP to +lan and +tailnet for every guest
  # defined with network = "lab" (see infra/guests/*.toml and installer step 'guest-firewall'). Verified by acceptance test.
  mark_done $STEP; log "step $STEP applied (vmbr1 ${LAB_CIDR}, NAT via ${wan_if})"
}
if [[ "$MODE" == check ]]; then check && exit 0 || exit 1; fi
apply
