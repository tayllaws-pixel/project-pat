#!/usr/bin/env bash
# Host script: install the deploy controller (deployd) as a systemd service ON THE PROXMOX HOST — it needs
# `pct` to apply changes into containers. Stdlib Python only (no host pip). It acts solely on verified
# single-use Sentinel tokens. Idempotent. ADR-0003/0005 (authorized deployment controller; approvals
# enforced by a trusted backend service).
set -euo pipefail

ETC=/etc/project-pat
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
log(){ printf '[create-deployd] %s\n' "$*"; }

# shellcheck disable=SC1091
source "$ETC/secrets.env"
[ -n "${SENTINEL_REQUESTER_TOKEN:-}" ] || { echo "SENTINEL_REQUESTER_TOKEN missing (run create-sentinel.sh)"; exit 1; }

grep -q '^DEPLOY_CONTROLLER_TOKEN=' "$ETC/secrets.env" || echo "DEPLOY_CONTROLLER_TOKEN=$(openssl rand -hex 24)" >> "$ETC/secrets.env"
source "$ETC/secrets.env"

SENT_IP="$(pct exec 104 -- hostname -I | awk '{print $1}')"
[ -n "$SENT_IP" ] || { echo "sentinel (104) must be running"; exit 1; }
log "sentinel=${SENT_IP}"

cat > "$ETC/deployd.env" <<EOF
SENTINEL_URL=http://${SENT_IP}:8070
SENTINEL_REQUESTER_TOKEN=${SENTINEL_REQUESTER_TOKEN}
DEPLOY_CONTROLLER_TOKEN=${DEPLOY_CONTROLLER_TOKEN}
DEPLOY_BIND=0.0.0.0
DEPLOY_PORT=8040
EOF
chmod 600 "$ETC/deployd.env"

cat > /etc/systemd/system/deployd.service <<EOF
[Unit]
Description=Project PAT deploy controller
After=network-online.target pve-guests.service

[Service]
EnvironmentFile=${ETC}/deployd.env
ExecStart=/usr/bin/python3 ${REPO}/services/deployd/deployd.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable deployd
systemctl restart deployd

sleep 2
HOSTIP="$(hostname -I | awk '{print $1}')"
echo "----- healthz -----"
curl -s http://127.0.0.1:8040/healthz || true
echo
echo "========================================================"
echo "  deployd at http://${HOSTIP}:8040  (host service, root; pct access; token-gated)"
echo "  Control token in ${ETC}/secrets.env (DEPLOY_CONTROLLER_TOKEN)"
echo "========================================================"
