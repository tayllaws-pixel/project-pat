#!/usr/bin/env bash
# Runs on the Proxmox HOST. Creates the infra-core LXC (CT 100) on LVM-thin, generates secrets if needed,
# pushes the provisioning files + schema into the container, and provisions PostgreSQL 17 + pgvector.
# Idempotent: re-running reconciles rather than recreating. Part of Option A (ADR-0010).
set -euo pipefail

CTID=100
NAME=infra-core
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
TEMPL_STORAGE="${TEMPL_STORAGE:-local}"
MEM=8192; CORES=4; DISK=200; BRIDGE=vmbr0
ETC=/etc/project-pat
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

log() { printf '[create-infra-core] %s\n' "$*"; }

log "1/6 secrets"
mkdir -p "$ETC"; chmod 700 "$ETC"
if [ ! -f "$ETC/secrets.env" ]; then
  umask 077
  {
    echo "# project-pat secrets — generated $(date -Is). Never commit."
    for k in POSTGRES_SUPERUSER_PASSWORD PAT_DB_PASSWORD WADE_DB_PASSWORD SENTINEL_DB_PASSWORD \
             PAT_READER_PASSWORD PAT_SESSION_SECRET INFERENCE_SHARED_TOKEN; do
      echo "${k}=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 32)"
    done
  } > "$ETC/secrets.env"
  chmod 600 "$ETC/secrets.env"
  log "    generated $ETC/secrets.env"
else
  log "    $ETC/secrets.env already present — keeping"
fi

log "2/6 template"
TEMPL="$(pveam list "$TEMPL_STORAGE" 2>/dev/null | awk '/debian-13-standard/{print $1}' | head -1 || true)"
if [ -z "${TEMPL:-}" ]; then
  pveam update
  AVAIL="$(pveam available --section system | awk '/debian-13-standard/{print $2}' | head -1)"
  [ -n "$AVAIL" ] || { echo "no debian-13-standard template available"; exit 1; }
  pveam download "$TEMPL_STORAGE" "$AVAIL"
  TEMPL="$(pveam list "$TEMPL_STORAGE" | awk '/debian-13-standard/{print $1}' | head -1)"
fi
log "    template: $TEMPL"

log "3/6 create CT $CTID"
if ! pct config "$CTID" >/dev/null 2>&1; then
  pct create "$CTID" "$TEMPL" \
    --hostname "$NAME" --cores "$CORES" --cpuunits 1024 --memory "$MEM" --swap 0 \
    --rootfs "${DISK_STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
    --unprivileged 1 --features nesting=1 --onboot 1 --start 0 \
    --tags "pat;data" --description "infra-core: PostgreSQL 17 + pgvector (Project PAT)"
  log "    created"
else
  log "    CT $CTID already exists — keeping"
fi
pct status "$CTID" | grep -q running || pct start "$CTID"

log "4/6 wait for container network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

log "5/6 push provisioning files"
pct exec "$CTID" -- mkdir -p /opt/project-pat/db/migrations /etc/project-pat
pct push "$CTID" "$ETC/secrets.env" /etc/project-pat/secrets.env --perms 0600
pct push "$CTID" "$REPO/services/infra-core/provision-db.sh" /opt/project-pat/provision-db.sh --perms 0755
pct push "$CTID" "$REPO/db/apply-migrations.sh" /opt/project-pat/db/apply-migrations.sh --perms 0755
for f in "$REPO"/db/migrations/*.sql; do
  pct push "$CTID" "$f" "/opt/project-pat/db/migrations/$(basename "$f")"
done

log "6/6 provision PostgreSQL"
pct exec "$CTID" -- bash /opt/project-pat/provision-db.sh

IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
log "DONE — infra-core CT $CTID running at ${IP} (PostgreSQL 17 + pgvector)"
log "record this IP for the other services; reserve it on the router for stability"
