#!/usr/bin/env bash
# Step 30 — Share the host GPU with an unprivileged LXC container (ADR-0001). Usage: 30-lxc-gpu.sh --apply CTID
# Installs the matching userspace driver inside the CT (--no-kernel-modules).
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
STEP=30
CTID=""
for a in "$@"; do [[ "$a" =~ ^[0-9]+$ ]] && CTID="$a"; done
[[ -n "$CTID" ]] || die "usage: $0 --check|--apply CTID"
CONF="/etc/pve/lxc/${CTID}.conf"
NV_VER="$(cat "$PAT_ETC/nvidia-version" 2>/dev/null || true)"

check() { grep -q '^dev0: /dev/nvidia0' "$CONF" 2>/dev/null && pct exec "$CTID" -- nvidia-smi -L >/dev/null 2>&1; }
apply() {
  require_root; require_pve; [[ -f "$CONF" ]] || die "no such CT $CTID"; [[ -n "$NV_VER" ]] || die "run step 20 first"
  [[ -e /dev/nvidia-uvm ]] || nvidia-modprobe -c0 -u
  backup_file "$CONF"
  local i=0
  for dev in /dev/nvidia0 /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia-caps/nvidia-cap1 /dev/nvidia-caps/nvidia-cap2; do
    [[ -e "$dev" ]] || continue
    grep -q "^dev[0-9]*: ${dev}" "$CONF" || pct set "$CTID" --dev"$i" "${dev},gid=0,mode=0666"   # gid mapping: root-group inside unprivileged CT
    i=$((i+1))
  done
  pct status "$CTID" | grep -q running || pct start "$CTID"
  local run="/var/lib/project-pat/NVIDIA-Linux-x86_64-${NV_VER}.run"
  pct push "$CTID" "$run" "/root/nvidia.run" --perms 0755
  pct exec "$CTID" -- bash -c "command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=driver_version --format=csv,noheader | grep -q '^${NV_VER}\$' || /root/nvidia.run --silent --no-kernel-modules --no-x-check --no-nouveau-check --no-opengl-files --no-install-compat32-libs --no-questions"
  pct exec "$CTID" -- nvidia-smi -L || die "GPU not visible inside CT $CTID"
  log "GPU shared with CT $CTID (driver $NV_VER)"
}
if [[ "$MODE" == check ]]; then check && exit 0 || exit 1; fi
apply
