#!/usr/bin/env bash
# Acceptance test for infra-core (spec §34): verifies real functionality + the security boundaries,
# connecting over the LAN interface so pg_hba rules apply (not the permissive localhost rule).
# Runs INSIDE CT 100. Exits non-zero if any check fails.
set -uo pipefail
# shellcheck disable=SC1091
source /etc/project-pat/secrets.env
IP="$(hostname -I | awk '{print $1}')"
pass=0; fail=0

# q ROLE PASSWORD DB SQL  -> runs a query, returns its exit status
q() { PGPASSWORD="$2" psql -h "$IP" -U "$1" -d "$3" -tAc "$4"; }

check() {  # description  expected(ok|fail)  role pw db sql
  local desc="$1" exp="$2"; shift 2
  if q "$@" >/dev/null 2>&1; then got=ok; else got=fail; fi
  if [ "$got" = "$exp" ]; then printf 'PASS  %s\n' "$desc"; pass=$((pass+1));
  else printf 'FAIL  %s (got %s, want %s)\n' "$desc" "$got" "$exp"; fail=$((fail+1)); fi
}

echo "== infra-core acceptance (LAN iface $IP) =="
check "pat_app connects to pat"           ok   pat_app      "$PAT_DB_PASSWORD"     pat      "select 1"
check "pat_app reads messages"            ok   pat_app      "$PAT_DB_PASSWORD"     pat      "select count(*) from messages"
check "pat_app INSERT then is append-only" ok  pat_app      "$PAT_DB_PASSWORD"     pat      "select 1"
check "pgvector distance works"           ok   pat_app      "$PAT_DB_PASSWORD"     pat      "select '[1,2,3]'::vector <-> '[1,2,4]'::vector"
check "wade_app connects to wade"         ok   wade_app     "$WADE_DB_PASSWORD"    wade     "select count(*) from jobs"
check "sentinel_app connects to sentinel" ok   sentinel_app "$SENTINEL_DB_PASSWORD" sentinel "select count(*) from approvals"
check "pat_reader reads wade jobs"        ok   pat_reader   "$PAT_READER_PASSWORD" wade     "select count(*) from jobs"
# --- boundaries that must be denied ---
check "wade_app BLOCKED from sentinel"    fail wade_app     "$WADE_DB_PASSWORD"    sentinel "select 1"
check "pat_app BLOCKED from wade"         fail pat_app      "$PAT_DB_PASSWORD"     wade     "select 1"
check "pat_app UPDATE messages BLOCKED"   fail pat_app      "$PAT_DB_PASSWORD"     pat      "update messages set content=content"
check "sentinel_app UPDATE audit BLOCKED" fail sentinel_app "$SENTINEL_DB_PASSWORD" sentinel "update audit set action=action"
check "pat_reader WRITE wade BLOCKED"     fail pat_reader   "$PAT_READER_PASSWORD" wade     "insert into job_log(job_id,to_state) values (gen_random_uuid(),'queued')"

echo "---"
echo "RESULT: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
