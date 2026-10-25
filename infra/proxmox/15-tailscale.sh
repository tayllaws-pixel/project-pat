#!/usr/bin/env bash
# Step 15 — Tailscale on the host (PAT-006). Prints an auth URL; approve it from your phone.
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
STEP=15
check() { command -v tailscale >/dev/null && tailscale status --json 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get("BackendState")=="Running" else 1)'; }
apply() {
  require_root; ensure_dirs
  if ! command -v tailscale >/dev/null; then
    local codename; codename="$(. /etc/os-release; echo "$VERSION_CODENAME")"
    curl -fsSL "https://pkgs.tailscale.com/stable/debian/${codename}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL "https://pkgs.tailscale.com/stable/debian/${codename}.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
    apt-get update -qq && apt_install tailscale
  fi
  systemctl enable --now tailscaled >/dev/null
  # No --ssh (OpenSSH stays the SSH path), no --advertise-routes yet; accept-dns off so AdGuard can own DNS later.
  tailscale up --accept-dns=false --hostname=pve || true
  log "If an auth URL was printed above, open it on your phone, then re-run this step (or 'patctl install')."
  check && mark_done $STEP
}
if [[ "$MODE" == check ]]; then check && exit 0 || exit 1; fi
apply
