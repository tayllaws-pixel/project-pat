#!/usr/bin/env bash
# Host script: create the Wade LXC (CT 103) — the autonomous-dev orchestration brain — install the worker
# + control API, wire it to the wade DB and to Sentinel (requester token only), and verify. Wade holds NO
# Proxmox token and NO Sentinel signing key (ADR-0003). Idempotent.
set -euo pipefail

CTID=103
NAME=wade
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
TEMPL_STORAGE="${TEMPL_STORAGE:-local}"
MEM=4096; CORES=4; DISK=20; BRIDGE=vmbr0; PORT=8060
ETC=/etc/project-pat
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
log(){ printf '[create-wade] %s\n' "$*"; }

# shellcheck disable=SC1091
source "$ETC/secrets.env"
[ -n "${WADE_DB_PASSWORD:-}" ] || { echo "WADE_DB_PASSWORD missing from $ETC/secrets.env (run provision-db.sh)"; exit 1; }
[ -n "${SENTINEL_REQUESTER_TOKEN:-}" ] || { echo "SENTINEL_REQUESTER_TOKEN missing (run create-sentinel.sh first)"; exit 1; }

log "1/7 control token"
grep -q '^WADE_CONTROL_TOKEN=' "$ETC/secrets.env" || echo "WADE_CONTROL_TOKEN=$(openssl rand -hex 24)" >> "$ETC/secrets.env"
source "$ETC/secrets.env"

log "2/7 create CT ${CTID}"
TEMPL="$(pveam list "$TEMPL_STORAGE" 2>/dev/null | awk '/debian-13-standard/{print $1}' | head -1)"
[ -n "${TEMPL:-}" ] || { echo "debian-13 template missing"; exit 1; }
if ! pct config "$CTID" >/dev/null 2>&1; then
  pct create "$CTID" "$TEMPL" \
    --hostname "$NAME" --cores "$CORES" --cpuunits 1024 --memory "$MEM" --swap 1024 \
    --rootfs "${DISK_STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
    --nameserver "1.1.1.1 8.8.8.8" --searchdomain home.arpa \
    --unprivileged 1 --features nesting=1 --onboot 1 --start 0 \
    --tags "pat;wade" --description "Wade — AI systems supervisor (Project PAT)"
else
  log "    CT ${CTID} exists — keeping"
fi
pct status "$CTID" | grep -q running || pct start "$CTID"

log "3/7 prep + wait for network"
pct push "$CTID" "$REPO/infra/provision/prep-container.sh" /opt/prep-container.sh --perms 0755
pct exec "$CTID" -- bash /opt/prep-container.sh
for _ in $(seq 1 30); do pct exec "$CTID" -- getent hosts pypi.org >/dev/null 2>&1 && break; sleep 2; done

log "4/7 resolve peers"
INFRA_IP="$(pct exec 100 -- hostname -I | awk '{print $1}')"
SENT_IP="$(pct exec 104 -- hostname -I | awk '{print $1}')"
INFER_IP="$(pct exec 101 -- hostname -I | awk '{print $1}')"
FORGE_IP="$(pct exec 110 -- hostname -I 2>/dev/null | awk '{print $1}' || true)"
[ -n "$INFRA_IP" ] && [ -n "$SENT_IP" ] || { echo "infra-core (100) + sentinel (104) must be running"; exit 1; }
INFERENCE_URL=""; [ -n "$INFER_IP" ] && INFERENCE_URL="http://${INFER_IP}:11434"
FORGE_URL="";     [ -n "$FORGE_IP" ] && FORGE_URL="http://${FORGE_IP}:8050"
REPO_URL="${WADE_REPO_URL:-https://github.com/tayllaws-pixel/project-pat.git}"
log "    infra-core=${INFRA_IP}  sentinel=${SENT_IP}  inference=${INFER_IP:-<none>}  forge=${FORGE_IP:-<none>}"

log "5/7 install python + git + deps"
pct exec "$CTID" -- bash -c '
  set -e; export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv git ca-certificates curl >/dev/null
  mkdir -p /var/wade/work
  test -d /opt/wade-venv || python3 -m venv /opt/wade-venv
  /opt/wade-venv/bin/pip install -q --upgrade pip
  /opt/wade-venv/bin/pip install -q fastapi "uvicorn[standard]" "psycopg[binary]" psycopg_pool httpx'

log "6/7 deploy worker + config + systemd"
pct exec "$CTID" -- mkdir -p /opt/wade/wade
for f in __init__.py db.py handlers.py worker.py app.py; do
  pct push "$CTID" "$REPO/services/wade/wade/$f" "/opt/wade/wade/$f"
done
pct exec "$CTID" -- bash -c "cat > /etc/wade.env <<EOF
WADE_DB_DSN=postgresql://wade_app:${WADE_DB_PASSWORD}@${INFRA_IP}:5432/wade
SENTINEL_URL=http://${SENT_IP}:8070
SENTINEL_REQUESTER_TOKEN=${SENTINEL_REQUESTER_TOKEN}
WADE_CONTROL_TOKEN=${WADE_CONTROL_TOKEN}
WADE_ACTOR_ID=wade
INFERENCE_URL=${INFERENCE_URL}
FORGE_URL=${FORGE_URL}
FORGE_TOKEN=${FORGE_TOKEN:-}
WADE_REPO_URL=${REPO_URL}
WADE_CODE_MODEL=${WADE_CODE_MODEL:-qwen3:8b}
WADE_WORK_ROOT=/var/wade/work
EOF
chmod 600 /etc/wade.env"
pct exec "$CTID" -- bash -c 'cat > /etc/systemd/system/wade.service <<EOF
[Unit]
Description=Wade supervisor
After=network-online.target

[Service]
EnvironmentFile=/etc/wade.env
WorkingDirectory=/opt/wade
ExecStart=/opt/wade-venv/bin/uvicorn wade.app:app --host 0.0.0.0 --port 8060
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now wade'

log "7/7 verify"
sleep 5
IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo "----- healthz -----"
pct exec "$CTID" -- bash -c "curl -s http://127.0.0.1:${PORT}/healthz" || true
echo
echo "========================================================"
echo "  Wade live at:  http://${IP}:${PORT}"
echo "  Control token (Pat/human) in ${ETC}/secrets.env (WADE_CONTROL_TOKEN)"
echo "  Enqueue: POST /jobs   Status: GET /status   Jobs: GET /jobs"
echo "========================================================"
