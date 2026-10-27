#!/usr/bin/env bash
# Host script: create the Pat LXC (CT 102), install the Pat backend + web UI, wire it to the Postgres
# (infra-core) and Ollama (inference) containers, and start it. Idempotent. Prints the admin login.
set -euo pipefail

CTID=102
NAME=pat
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
TEMPL_STORAGE="${TEMPL_STORAGE:-local}"
MEM=6144; CORES=4; DISK=40; BRIDGE=vmbr0
ETC=/etc/project-pat
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
log(){ printf '[create-pat] %s\n' "$*"; }

# shellcheck disable=SC1091
source "$ETC/secrets.env"

log "1/7 admin password"
if ! grep -q '^PAT_ADMIN_PASSWORD=' "$ETC/secrets.env"; then
  PW="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)"
  echo "PAT_ADMIN_PASSWORD=${PW}" >> "$ETC/secrets.env"
else
  PW="$(grep '^PAT_ADMIN_PASSWORD=' "$ETC/secrets.env" | cut -d= -f2-)"
fi

log "2/7 template + create CT ${CTID}"
TEMPL="$(pveam list "$TEMPL_STORAGE" 2>/dev/null | awk '/debian-13-standard/{print $1}' | head -1)"
if ! pct config "$CTID" >/dev/null 2>&1; then
  pct create "$CTID" "$TEMPL" \
    --hostname "$NAME" --cores "$CORES" --cpuunits 2048 --memory "$MEM" --swap 0 \
    --rootfs "${DISK_STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
    --nameserver "1.1.1.1 8.8.8.8" --searchdomain home.arpa \
    --unprivileged 1 --features nesting=1 --onboot 1 --start 0 \
    --tags "pat;app" --description "Pat assistant (Project PAT)"
fi
pct status "$CTID" | grep -q running || pct start "$CTID"

log "3/7 prep + wait for network"
pct push "$CTID" "$REPO/infra/provision/prep-container.sh" /opt/prep-container.sh --perms 0755
pct exec "$CTID" -- bash /opt/prep-container.sh
for _ in $(seq 1 30); do pct exec "$CTID" -- getent hosts pypi.org >/dev/null 2>&1 && break; sleep 2; done

log "4/7 resolve peer IPs"
INFRA_IP="$(pct exec 100 -- hostname -I | awk '{print $1}')"
INFER_IP="$(pct exec 101 -- hostname -I | awk '{print $1}')"
[ -n "$INFRA_IP" ] && [ -n "$INFER_IP" ] || { echo "infra-core (100) / inference (101) must be running"; exit 1; }
log "    infra-core=${INFRA_IP}  inference=${INFER_IP}"

log "5/7 install python + deps"
pct exec "$CTID" -- bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv ca-certificates >/dev/null
  test -d /opt/pat-venv || python3 -m venv /opt/pat-venv
  /opt/pat-venv/bin/pip install -q --upgrade pip
  /opt/pat-venv/bin/pip install -q fastapi "uvicorn[standard]" "psycopg[binary]" psycopg_pool httpx'

log "6/7 deploy app + config + service"
pct exec "$CTID" -- mkdir -p /opt/pat/pat /opt/pat/web
pct push "$CTID" "$REPO/services/pat/pat/__init__.py" /opt/pat/pat/__init__.py
pct push "$CTID" "$REPO/services/pat/pat/app.py"      /opt/pat/pat/app.py
pct push "$CTID" "$REPO/services/pat/web/index.html"  /opt/pat/web/index.html
pct exec "$CTID" -- bash -c "cat > /etc/pat.env <<EOF
PAT_DB_DSN=postgresql://pat_app:${PAT_DB_PASSWORD}@${INFRA_IP}:5432/pat
OLLAMA_URL=http://${INFER_IP}:11434
PAT_SESSION_SECRET=${PAT_SESSION_SECRET}
PAT_ADMIN_USER=taylor
PAT_ADMIN_PASSWORD=${PW}
PAT_DEFAULT_MODEL=qwen3:8b
EOF
chmod 600 /etc/pat.env"
pct exec "$CTID" -- bash -c 'cat > /etc/systemd/system/pat.service <<EOF
[Unit]
Description=Pat assistant
After=network-online.target

[Service]
EnvironmentFile=/etc/pat.env
WorkingDirectory=/opt/pat
ExecStart=/opt/pat-venv/bin/uvicorn pat.app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now pat'

log "7/7 verify"
sleep 5
PAT_IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo "----- healthz -----"
pct exec "$CTID" -- bash -c "apt-get install -y -qq curl >/dev/null 2>&1 || true; curl -s http://127.0.0.1:8080/healthz" || true
echo
echo "========================================================"
echo "  Pat is live at:   http://${PAT_IP}:8080"
echo "  Login:  taylor  /  ${PW}"
echo "  (password stored in ${ETC}/secrets.env; change later)"
echo "========================================================"
