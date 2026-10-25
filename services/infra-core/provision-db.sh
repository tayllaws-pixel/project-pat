#!/usr/bin/env bash
# Runs INSIDE the infra-core LXC (CT 100). Installs PostgreSQL 17 + pgvector, configures it to serve the
# LAN, creates the per-domain roles and databases (ADR-0004), applies the schema, and locks down grants
# (append-only messages/audit, pat_reader read-only on wade reporting). Idempotent — safe to re-run.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/project-pat/secrets.env
export DEBIAN_FRONTEND=noninteractive

LAN_CIDR="${LAN_CIDR:-192.168.1.0/24}"
PG_MAJOR=17

echo "== installing PostgreSQL ${PG_MAJOR} + pgvector =="
apt-get update -qq
if ! apt-get install -y -qq "postgresql-${PG_MAJOR}" "postgresql-${PG_MAJOR}-pgvector" 2>/dev/null; then
  echo "-- falling back to PGDG apt repo --"
  apt-get install -y -qq curl ca-certificates gnupg lsb-release
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  apt-get install -y -qq "postgresql-${PG_MAJOR}" "postgresql-${PG_MAJOR}-pgvector"
fi
apt-get install -y -qq sudo gawk >/dev/null 2>&1 || true

systemctl enable --now postgresql >/dev/null 2>&1 || true

CONF_DIR="/etc/postgresql/${PG_MAJOR}/main"
PGCONF="${CONF_DIR}/postgresql.conf"
HBA="${CONF_DIR}/pg_hba.conf"

echo "== network config =="
# Listen on all interfaces (the container only has lo + eth0 on the LAN bridge); access is gated by pg_hba + firewall.
if ! grep -qE "^listen_addresses = '\*'" "$PGCONF"; then
  sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" "$PGCONF"
fi
if ! grep -q 'project-pat' "$HBA"; then
  cat >> "$HBA" <<EOF

# project-pat: each app role reaches only its own database from the LAN, scram-authenticated
host  pat       pat_app        ${LAN_CIDR}  scram-sha-256
host  wade      wade_app       ${LAN_CIDR}  scram-sha-256
host  sentinel  sentinel_app   ${LAN_CIDR}  scram-sha-256
host  wade      pat_reader     ${LAN_CIDR}  scram-sha-256
EOF
fi
systemctl restart postgresql

PSQL="sudo -u postgres psql -v ON_ERROR_STOP=1 -X -q"

echo "== roles =="
create_role() {  # name  password
  $PSQL -d postgres -c "DO \$\$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='$1') THEN ALTER ROLE $1 LOGIN PASSWORD '$2';
    ELSE CREATE ROLE $1 LOGIN PASSWORD '$2'; END IF; END \$\$;"
}
create_role pat_app      "${PAT_DB_PASSWORD}"
create_role wade_app     "${WADE_DB_PASSWORD}"
create_role sentinel_app "${SENTINEL_DB_PASSWORD}"
create_role pat_reader   "${PAT_READER_PASSWORD}"

echo "== databases =="
create_db() {  # dbname  owner
  $PSQL -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$1'" | grep -q 1 \
    || $PSQL -d postgres -c "CREATE DATABASE $1 OWNER $2"
}
create_db pat      pat_app
create_db wade     wade_app
create_db sentinel sentinel_app

echo "== schema =="
bash /opt/project-pat/db/apply-migrations.sh /opt/project-pat/db/migrations

echo "== grants / least privilege =="
# Append-only: app roles may INSERT/SELECT but not UPDATE/DELETE the history tables (spec §6, ADR-0005).
$PSQL -d pat -c "REVOKE UPDATE, DELETE ON messages FROM pat_app;" || true
$PSQL -d sentinel -c "REVOKE UPDATE, DELETE ON audit FROM sentinel_app;" || true
# pat_reader: read-only on wade reporting tables for the infra map / 'what did Wade finish' queries.
$PSQL -d wade -c "GRANT CONNECT ON DATABASE wade TO pat_reader;"
$PSQL -d wade -c "GRANT USAGE ON SCHEMA public TO pat_reader;"
$PSQL -d wade -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO pat_reader;"
$PSQL -d wade -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO pat_reader;"

echo "== verify =="
$PSQL -d pat      -c "SELECT extname FROM pg_extension WHERE extname='vector';"
for db in pat wade sentinel; do
  n=$($PSQL -d "$db" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
  echo "  $db: $n tables"
done
echo "infra-core provisioning complete"
