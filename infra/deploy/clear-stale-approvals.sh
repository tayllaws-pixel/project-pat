#!/usr/bin/env bash
# One-off maintenance: clear the stale pending approvals and matching Wade job records
# left over from capability-ladder testing (calcbot, hello-svc, demo_notes, rung-1 edits, …).
#   - Sentinel: each open approval is REVOKED (its single-use token can no longer be consumed,
#     so nothing can ship), and the revoke is written to the hash-chained audit.
#   - Wade: the orphaned awaiting_approval job records move to 'cancelled' (awaiting_approval is
#     terminal in Wade and never reconciles on its own), clearing the Supervisors tab list.
# Safe to re-run: it only touches currently-open approvals and awaiting_approval jobs.
# Run on the Proxmox HOST:  bash infra/deploy/clear-stale-approvals.sh
set -euo pipefail
PAT_CT=102; INFRA_CT=100
log(){ printf '[clear-approvals] %s\n' "$*"; }

log "1/2 revoke open Sentinel approvals (voids their tokens, audited)"
pct exec "$PAT_CT" -- bash -c '
set -a; . /etc/pat.env; set +a
T="$SENTINEL_DECIDER_TOKEN"; U="$SENTINEL_URL"
ids=$(curl -s -H "Authorization: Bearer $T" "$U/approvals?status=open" \
      | python3 -c "import sys,json;print(\" \".join(a[\"id\"] for a in json.load(sys.stdin).get(\"approvals\",[])))")
if [ -z "${ids// }" ]; then echo "  no open approvals"; else
  for id in $ids; do
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
      -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
      -d "{\"by\":\"taylor\",\"note\":\"cleanup: stale capability-ladder test approvals\"}" \
      "$U/approvals/$id/revoke")
    echo "  revoke $id -> $code"
  done
fi
echo "  remaining open: $(curl -s -H "Authorization: Bearer $T" "$U/approvals?status=open" \
      | python3 -c "import sys,json;print(len(json.load(sys.stdin).get(\"approvals\",[])))")"
'

log "2/2 cancel Wade awaiting_approval job records"
pct exec "$INFRA_CT" -- sudo -u postgres psql -d wade -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO job_log (job_id, from_state, to_state, note)
  SELECT id, 'awaiting_approval', 'cancelled', 'cleanup: stale test approval'
  FROM jobs WHERE state='awaiting_approval';
UPDATE jobs SET state='cancelled', finished_at=now(), updated_at=now()
  WHERE state='awaiting_approval';
SELECT 'wade awaiting_approval remaining: '||count(*) AS result FROM jobs WHERE state='awaiting_approval';
SQL

log "done"
