#!/usr/bin/env bash
# Apply numbered SQL migrations to each Project PAT database (PAT-022), splitting each file by its
# `-- @db <name>` markers so a section runs only against the database it names (plus 'common' and 'all').
# Mirrors db/migrate.py's splitting logic; used inside the infra-core container where psycopg may be absent.
# Idempotent: a version already recorded in that db's schema_migrations is skipped.
set -euo pipefail

MIG_DIR="${1:-/opt/project-pat/db/migrations}"
DBS=(pat wade sentinel)
PSQL=(sudo -u postgres psql -v ON_ERROR_STOP=1 -X -q)

section_for_db() {
  # $1 = db name, reads SQL file on stdin, prints the common preamble + 'all' + that db's section
  awk -v db="$1" '
    /^--[ \t]*@db[ \t]+[A-Za-z0-9_]+[ \t]*$/ { sec=$3; next }
    { if (sec=="" || sec=="all" || sec==db) print }
  '
}

for db in "${DBS[@]}"; do
  for f in "$MIG_DIR"/[0-9]*.sql; do
    [ -e "$f" ] || continue
    ver="$(basename "$f" | cut -d_ -f1)"
    already="$("${PSQL[@]}" -tA -d "$db" -c \
      "SELECT 1 FROM schema_migrations WHERE version='${ver}'" 2>/dev/null || true)"
    if [ "$already" = "1" ]; then
      echo "[$db] migration $ver already applied"
      continue
    fi
    echo "[$db] applying migration $ver"
    section_for_db "$db" < "$f" | "${PSQL[@]}" -d "$db"
    # schema_migrations is created by the migration's own '@db all' section; record the version now
    "${PSQL[@]}" -d "$db" -c \
      "INSERT INTO schema_migrations(version, checksum) VALUES ('${ver}','sh-applied') ON CONFLICT (version) DO NOTHING"
  done
done
echo "migrations complete"
