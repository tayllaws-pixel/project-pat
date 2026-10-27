#!/usr/bin/env bash
# Acceptance test for the live Sentinel service (CT 104). Exercises the safety guarantees end-to-end over
# HTTP with the real decider/requester tokens. Run on the Proxmox host. Leaves the e-stop released.
set -uo pipefail
ETC=/etc/project-pat
# shellcheck disable=SC1091
source "$ETC/secrets.env"
IP="$(pct exec 104 -- hostname -I | awk '{print $1}')"
SENT="http://${IP}:8070"
D="Authorization: Bearer ${SENTINEL_DECIDER_TOKEN}"
R="Authorization: Bearer ${SENTINEL_REQUESTER_TOKEN}"
PASS=0; FAIL=0
ck(){ if [ "$1" = "$2" ]; then echo "PASS $3"; PASS=$((PASS+1)); else echo "FAIL $3 (got '$1' want '$2')"; FAIL=$((FAIL+1)); fi; }
jget(){ sed -n "s/.*\"$1\":\"\\([^\"]*\\)\".*/\\1/p"; }   # first string field
jbool(){ sed -n "s/.*\"$1\":\\(true\\|false\\).*/\\1/p"; }

echo "Sentinel @ ${SENT}"
ck "$(curl -s -o /dev/null -w '%{http_code}' $SENT/healthz)" "200" "healthz"

# unauthenticated is rejected
ck "$(curl -s -o /dev/null -w '%{http_code}' $SENT/status)" "401" "no-token-rejected"

# requester (Wade) creates an approval request
AID=$(curl -s -H "$R" -H 'Content-Type: application/json' -X POST $SENT/approvals \
  -d '{"requester":{"kind":"supervisor","id":"wade"},"action":"deploy.production","reason":"ship UI tweak","affected":["pat"],"payload":{"service":"pat","sha":"abc"}}' | jget id)
[ -n "$AID" ] && { echo "PASS requester-create ($AID)"; PASS=$((PASS+1)); } || { echo "FAIL requester-create"; FAIL=$((FAIL+1)); }

# requester must NOT be able to decide its own request
ck "$(curl -s -o /dev/null -w '%{http_code}' -H "$R" -X POST $SENT/approvals/$AID/decide -d '{"decision":"approved"}')" "403" "requester-cannot-decide"
# requester must NOT be able to engage the e-stop
ck "$(curl -s -o /dev/null -w '%{http_code}' -H "$R" -X POST $SENT/estop/engage -d '{"reason":"x"}')" "403" "requester-cannot-estop"

# decider (Pat/human) approves -> signed token
TOK=$(curl -s -H "$D" -H 'Content-Type: application/json' -X POST $SENT/approvals/$AID/decide -d '{"decision":"approved","by":"taylor"}' | jget token)
[ -n "$TOK" ] && { echo "PASS decider-approve-token"; PASS=$((PASS+1)); } || { echo "FAIL decider-approve-token"; FAIL=$((FAIL+1)); }

# executor verifies the token (single-use)
ck "$(curl -s -o /dev/null -w '%{http_code}' -H "$R" -X POST $SENT/verify -d "{\"token\":\"$TOK\",\"action\":\"deploy.production\",\"payload\":{\"service\":\"pat\",\"sha\":\"abc\"},\"executor\":\"deployer\"}")" "200" "verify-token"
# replaying the same token is rejected
ck "$(curl -s -o /dev/null -w '%{http_code}' -H "$R" -X POST $SENT/verify -d "{\"token\":\"$TOK\",\"action\":\"deploy.production\",\"payload\":{\"service\":\"pat\",\"sha\":\"abc\"},\"executor\":\"deployer\"}")" "403" "replay-blocked"

# gate: allowed normally, blocked under e-stop, restored after release
ck "$(curl -s -H "$R" $SENT/gate/wade | jbool allowed)" "true" "gate-allowed"
curl -s -H "$D" -X POST $SENT/estop/engage  -d '{"by":"taylor","reason":"test"}' >/dev/null
ck "$(curl -s -H "$R" $SENT/gate/wade | jbool allowed)" "false" "gate-blocked-by-estop"
curl -s -H "$D" -X POST $SENT/estop/release -d '{"by":"taylor"}' >/dev/null
ck "$(curl -s -H "$R" $SENT/gate/wade | jbool allowed)" "true" "gate-restored"

# tamper-evident audit chain verifies
ck "$(curl -s -H "$D" $SENT/audit/verify | jbool ok)" "true" "audit-chain-ok"

echo "================  Sentinel acceptance: PASS=$PASS FAIL=$FAIL  ================"
