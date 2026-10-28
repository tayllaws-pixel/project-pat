#!/usr/bin/env bash
# Acceptance test for the live deploy controller (deployd), run on the Proxmox host. Creates a deploy.code
# approval for a safe target, approves it, deploys via deployd, confirms the file changed + the service is
# healthy, confirms the token can't be replayed, then restores the original. Self-contained fixture.
set -uo pipefail
ETC=/etc/project-pat
# shellcheck disable=SC1091
source "$ETC/secrets.env"
SIP="$(pct exec 104 -- hostname -I | awk '{print $1}')"
HIP="$(hostname -I | awk '{print $1}')"
S="http://${SIP}:8070"; DD="http://${HIP}:8040"
R="Authorization: Bearer ${SENTINEL_REQUESTER_TOKEN}"
DEC="Authorization: Bearer ${SENTINEL_DECIDER_TOKEN}"
DC="Authorization: Bearer ${DEPLOY_CONTROLLER_TOKEN}"
PASS=0; FAIL=0
ck(){ if [ "$1" = "$2" ]; then echo "PASS $3"; PASS=$((PASS+1)); else echo "FAIL $3 (got '$1' want '$2')"; FAIL=$((FAIL+1)); fi; }
jbool(){ sed -n 's/.*"ok":\(true\|false\).*/\1/p'; }
idof(){ sed -n 's/.*"id":"\([^"]*\)".*/\1/p'; }
tokof(){ sed -n 's/.*"token":"\([^"]*\)".*/\1/p'; }

TARGET="services/forge/forge/__init__.py"; CPATH="/opt/forge/forge/__init__.py"
pct exec 110 -- cat "$CPATH" > /tmp/forge_init.orig
MARK="# deployd acceptance marker $(date +%s)"
cat /tmp/forge_init.orig > /tmp/forge_init.new; echo "$MARK" >> /tmp/forge_init.new
B64="$(base64 -w0 /tmp/forge_init.new)"; SHA="$(sha256sum /tmp/forge_init.new | awk '{print $1}')"

ck "$(curl -s -o /dev/null -w '%{http_code}' $DD/healthz)" "200" "deployd-healthz"

AID=$(curl -s -H "$R" -H 'Content-Type: application/json' -X POST $S/approvals \
  -d "{\"requester\":{\"kind\":\"supervisor\",\"id\":\"wade\"},\"action\":\"deploy.code\",\"reason\":\"deployd acceptance\",\"affected\":[\"$TARGET\"],\"payload\":{\"target_file\":\"$TARGET\",\"new_sha256\":\"$SHA\",\"base_branch\":\"main\",\"new_content_b64\":\"$B64\"}}" | idof)
[ -n "$AID" ] && { echo "PASS create-approval ($AID)"; PASS=$((PASS+1)); } || { echo "FAIL create-approval"; FAIL=$((FAIL+1)); }

TOK=$(curl -s -H "$DEC" -H 'Content-Type: application/json' -X POST $S/approvals/$AID/decide -d '{"decision":"approved","by":"taylor"}' | tokof)
[ -n "$TOK" ] && { echo "PASS approve-token"; PASS=$((PASS+1)); } || { echo "FAIL approve-token"; FAIL=$((FAIL+1)); }

OUT=$(curl -s -H "$DC" -H 'Content-Type: application/json' -X POST $DD/deploy -d "{\"approval_id\":\"$AID\",\"token\":\"$TOK\"}")
echo "  deployd -> $OUT"
ck "$(echo "$OUT" | jbool)" "true" "deploy-ok"
pct exec 110 -- grep -q "$MARK" "$CPATH" && { echo "PASS file-updated-in-container"; PASS=$((PASS+1)); } || { echo "FAIL file-updated-in-container"; FAIL=$((FAIL+1)); }
ck "$(pct exec 110 -- curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8050/healthz)" "200" "forge-healthy-after-deploy"

OUT2=$(curl -s -H "$DC" -H 'Content-Type: application/json' -X POST $DD/deploy -d "{\"approval_id\":\"$AID\",\"token\":\"$TOK\"}")
ck "$(echo "$OUT2" | jbool)" "false" "token-replay-rejected"

# restore original
pct push 110 /tmp/forge_init.orig "$CPATH" >/dev/null && pct exec 110 -- systemctl restart forge >/dev/null
sleep 2
ck "$(pct exec 110 -- curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8050/healthz)" "200" "restored-healthy"
rm -f /tmp/forge_init.orig /tmp/forge_init.new
echo "================ deployd acceptance: PASS=$PASS FAIL=$FAIL ================"
