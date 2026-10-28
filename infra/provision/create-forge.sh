#!/usr/bin/env bash
# Host script: create the Forge LXC (CT 110) — the isolated build/test executor. Secret-free: it gets only
# its own FORGE_TOKEN, no DB/Sentinel/other creds. Runs as an unprivileged 'forge' user in throwaway dirs.
# Idempotent. NOTE (ADR-0003): target architecture is a kernel-isolated VM on vmbr1; this LXC v1 runs only
# our own repo's checks until that upgrade lands.
set -euo pipefail

CTID=110
NAME=forge
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
TEMPL_STORAGE="${TEMPL_STORAGE:-local}"
MEM=6144; CORES=4; DISK=25; BRIDGE=vmbr0; PORT=8050
ETC=/etc/project-pat
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
log(){ printf '[create-forge] %s\n' "$*"; }

# shellcheck disable=SC1091
source "$ETC/secrets.env"
grep -q '^FORGE_TOKEN=' "$ETC/secrets.env" || echo "FORGE_TOKEN=$(openssl rand -hex 24)" >> "$ETC/secrets.env"
source "$ETC/secrets.env"

log "1/6 create CT ${CTID}"
TEMPL="$(pveam list "$TEMPL_STORAGE" 2>/dev/null | awk '/debian-13-standard/{print $1}' | head -1)"
[ -n "${TEMPL:-}" ] || { echo "debian-13 template missing"; exit 1; }
if ! pct config "$CTID" >/dev/null 2>&1; then
  pct create "$CTID" "$TEMPL" \
    --hostname "$NAME" --cores "$CORES" --cpuunits 512 --memory "$MEM" --swap 1024 \
    --rootfs "${DISK_STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
    --nameserver "1.1.1.1 8.8.8.8" --searchdomain home.arpa \
    --unprivileged 1 --features nesting=1 --onboot 1 --start 0 \
    --tags "pat;forge" --description "Forge — isolated build/test executor (Project PAT)"
else
  log "    CT ${CTID} exists — keeping"
fi
pct status "$CTID" | grep -q running || pct start "$CTID"

log "2/6 prep + wait for network"
pct push "$CTID" "$REPO/infra/provision/prep-container.sh" /opt/prep-container.sh --perms 0755
pct exec "$CTID" -- bash /opt/prep-container.sh
for _ in $(seq 1 30); do pct exec "$CTID" -- getent hosts pypi.org >/dev/null 2>&1 && break; sleep 2; done

log "3/6 install python + git + deps"
pct exec "$CTID" -- bash -c '
  set -e; export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv git ca-certificates curl >/dev/null
  id forge >/dev/null 2>&1 || useradd -r -m -d /home/forge -s /usr/sbin/nologin forge
  mkdir -p /var/forge/work && chown -R forge:forge /var/forge
  test -d /opt/forge-venv || python3 -m venv /opt/forge-venv
  /opt/forge-venv/bin/pip install -q --upgrade pip
  /opt/forge-venv/bin/pip install -q fastapi "uvicorn[standard]"'

log "4/6 deploy service"
pct exec "$CTID" -- mkdir -p /opt/forge/forge
pct push "$CTID" "$REPO/services/forge/forge/__init__.py" /opt/forge/forge/__init__.py
pct push "$CTID" "$REPO/services/forge/forge/app.py"      /opt/forge/forge/app.py
pct exec "$CTID" -- chown -R forge:forge /opt/forge

log "5/6 config + systemd (runs as unprivileged 'forge')"
pct exec "$CTID" -- bash -c "cat > /etc/forge.env <<EOF
FORGE_TOKEN=${FORGE_TOKEN}
FORGE_WORK=/var/forge/work
EOF
chmod 640 /etc/forge.env; chgrp forge /etc/forge.env"
pct exec "$CTID" -- bash -c 'cat > /etc/systemd/system/forge.service <<EOF
[Unit]
Description=Forge build/test executor
After=network-online.target

[Service]
User=forge
Group=forge
EnvironmentFile=/etc/forge.env
WorkingDirectory=/opt/forge
ExecStart=/opt/forge-venv/bin/uvicorn forge.app:app --host 0.0.0.0 --port 8050
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now forge'

log "6/6 verify"
sleep 4
IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo "----- healthz -----"
pct exec "$CTID" -- bash -c "curl -s http://127.0.0.1:${PORT}/healthz" || true
echo
echo "========================================================"
echo "  Forge live at:  http://${IP}:${PORT}   (runs as unprivileged 'forge', no secrets)"
echo "  Token in ${ETC}/secrets.env (FORGE_TOKEN)"
echo "========================================================"
