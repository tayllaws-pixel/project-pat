#!/usr/bin/env bash
# Host script: create the Sentinel LXC (CT 104) — the independent safety plane — install the service,
# wire it to the sentinel DB, and verify. Sentinel generates its own Ed25519 approval key on first start
# (stays in the container, 0600). Decider/requester bearer tokens are generated here and stored in
# secrets.env. Idempotent. ADR-0003 (CT 104 must be stoppable independently of Wade), ADR-0005.
set -euo pipefail

CTID=104
NAME=sentinel
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
TEMPL_STORAGE="${TEMPL_STORAGE:-local}"
MEM=2048; CORES=2; DISK=10; BRIDGE=vmbr0; PORT=8070
ETC=/etc/project-pat
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
log(){ printf '[create-sentinel] %s\n' "$*"; }

# shellcheck disable=SC1091
source "$ETC/secrets.env"
[ -n "${SENTINEL_DB_PASSWORD:-}" ] || { echo "SENTINEL_DB_PASSWORD missing from $ETC/secrets.env (run provision-db.sh)"; exit 1; }

log "1/7 tokens"
gen(){ openssl rand -hex 24; }
grep -q '^SENTINEL_DECIDER_TOKEN='   "$ETC/secrets.env" || echo "SENTINEL_DECIDER_TOKEN=$(gen)"   >> "$ETC/secrets.env"
grep -q '^SENTINEL_REQUESTER_TOKEN=' "$ETC/secrets.env" || echo "SENTINEL_REQUESTER_TOKEN=$(gen)" >> "$ETC/secrets.env"
source "$ETC/secrets.env"

log "2/7 template + create CT ${CTID}"
TEMPL="$(pveam list "$TEMPL_STORAGE" 2>/dev/null | awk '/debian-13-standard/{print $1}' | head -1)"
[ -n "${TEMPL:-}" ] || { echo "debian-13 template missing"; exit 1; }
if ! pct config "$CTID" >/dev/null 2>&1; then
  pct create "$CTID" "$TEMPL" \
    --hostname "$NAME" --cores "$CORES" --cpuunits 1024 --memory "$MEM" --swap 512 \
    --rootfs "${DISK_STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
    --nameserver "1.1.1.1 8.8.8.8" --searchdomain home.arpa \
    --unprivileged 1 --features nesting=1 --onboot 1 --start 0 \
    --tags "pat;safety" --description "Sentinel — independent safety plane (Project PAT)"
else
  log "    CT ${CTID} exists — keeping"
fi
pct status "$CTID" | grep -q running || pct start "$CTID"

log "3/7 prep + wait for network"
pct push "$CTID" "$REPO/infra/provision/prep-container.sh" /opt/prep-container.sh --perms 0755
pct exec "$CTID" -- bash /opt/prep-container.sh
for _ in $(seq 1 30); do pct exec "$CTID" -- getent hosts pypi.org >/dev/null 2>&1 && break; sleep 2; done

log "4/7 resolve DB IP"
INFRA_IP="$(pct exec 100 -- hostname -I | awk '{print $1}')"
[ -n "$INFRA_IP" ] || { echo "infra-core (100) must be running"; exit 1; }
log "    infra-core=${INFRA_IP}"

log "5/7 install python + deps"
pct exec "$CTID" -- bash -c '
  set -e; export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv ca-certificates curl >/dev/null
  test -d /opt/sentinel-venv || python3 -m venv /opt/sentinel-venv
  /opt/sentinel-venv/bin/pip install -q --upgrade pip
  /opt/sentinel-venv/bin/pip install -q fastapi "uvicorn[standard]" "psycopg[binary]" psycopg_pool cryptography pydantic'

log "6/7 deploy service (sentinel pkg + shared patshared) + config + systemd"
pct exec "$CTID" -- mkdir -p /opt/sentinel/sentinel /opt/sentinel/patshared /etc/project-pat/sentinel
for f in __init__.py core.py pgstore.py budget.py app.py; do
  pct push "$CTID" "$REPO/services/sentinel/sentinel/$f" "/opt/sentinel/sentinel/$f"
done
for f in __init__.py schemas.py router.py; do
  pct push "$CTID" "$REPO/services/shared/patshared/$f" "/opt/sentinel/patshared/$f"
done
pct exec "$CTID" -- bash -c "cat > /etc/sentinel.env <<EOF
SENTINEL_DB_DSN=postgresql://sentinel_app:${SENTINEL_DB_PASSWORD}@${INFRA_IP}:5432/sentinel
SENTINEL_KEY_DIR=/etc/project-pat/sentinel
SENTINEL_ESTOP_FLAG=/etc/project-pat/sentinel/ESTOP
SENTINEL_DECIDER_TOKEN=${SENTINEL_DECIDER_TOKEN}
SENTINEL_REQUESTER_TOKEN=${SENTINEL_REQUESTER_TOKEN}
EOF
chmod 600 /etc/sentinel.env"
pct exec "$CTID" -- bash -c 'cat > /etc/systemd/system/sentinel.service <<EOF
[Unit]
Description=Sentinel safety plane
After=network-online.target

[Service]
EnvironmentFile=/etc/sentinel.env
WorkingDirectory=/opt/sentinel
ExecStart=/opt/sentinel-venv/bin/uvicorn sentinel.app:app --host 0.0.0.0 --port 8070
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable sentinel; systemctl restart sentinel'

log "7/7 verify"
sleep 5
IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo "----- healthz -----"
pct exec "$CTID" -- bash -c "curl -s http://127.0.0.1:${PORT}/healthz" || true
echo; echo "----- pubkey (first line) -----"
pct exec "$CTID" -- bash -c "curl -s http://127.0.0.1:${PORT}/pubkey | head -1" || true
echo
echo "========================================================"
echo "  Sentinel live at:  http://${IP}:${PORT}"
echo "  Endpoints: /status /gate/{actor} /estop /approvals /verify /audit/verify"
echo "  Decider token (Pat) + Requester token (Wade) in ${ETC}/secrets.env"
echo "========================================================"
