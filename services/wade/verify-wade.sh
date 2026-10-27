#!/usr/bin/env bash
# Acceptance test for the live Wade worker (CT 103): a job runs to success; the Sentinel e-stop blocks a
# newly-enqueued job from starting; releasing the e-stop lets it run. Proves Sentinel governs Wade.
# Run on the Proxmox host. Leaves the e-stop released.
set -uo pipefail
ETC=/etc/project-pat
# shellcheck disable=SC1091
source "$ETC/secrets.env"
WIP="$(pct exec 103 -- hostname -I | awk '{print $1}')"
SIP="$(pct exec 104 -- hostname -I | awk '{print $1}')"
PIP="$(pct exec 102 -- hostname -I | awk '{print $1}')"
W="http://${WIP}:8060"; S="http://${SIP}:8070"
WC="Authorization: Bearer ${WADE_CONTROL_TOKEN}"
SD="Authorization: Bearer ${SENTINEL_DECIDER_TOKEN}"
PASS=0; FAIL=0
ck(){ if [ "$1" = "$2" ]; then echo "PASS $3"; PASS=$((PASS+1)); else echo "FAIL $3 (got '$1' want '$2')"; FAIL=$((FAIL+1)); fi; }
idof(){ sed -n 's/.*"id":"\([^"]*\)".*/\1/p'; }
stateof(){ python3 -c "import sys,json;js=json.load(sys.stdin)['jobs'];print(next((j['state'] for j in js if j['id']=='$1'),''))" 2>/dev/null; }

TARGETS="[{\"name\":\"pat\",\"url\":\"http://${PIP}:8080/healthz\"},{\"name\":\"sentinel\",\"url\":\"http://${SIP}:8070/healthz\"}]"
curl -s -H "$SD" -X POST $S/estop/release -d '{"by":"taylor"}' >/dev/null   # clean start

echo "Wade @ ${W}"
ck "$(curl -s -o /dev/null -w '%{http_code}' $W/healthz)" "200" "healthz"
ck "$(curl -s -o /dev/null -w '%{http_code}' $W/jobs)" "401" "no-token-rejected"

# 1) enqueue a diagnostics job and wait for success
J1=$(curl -s -H "$WC" -H 'Content-Type: application/json' -X POST $W/jobs \
  -d "{\"kind\":\"diagnostics.health\",\"title\":\"health sweep\",\"payload\":{\"targets\":$TARGETS}}" | idof)
[ -n "$J1" ] && { echo "PASS enqueue ($J1)"; PASS=$((PASS+1)); } || { echo "FAIL enqueue"; FAIL=$((FAIL+1)); }
st=""; for i in $(seq 1 15); do st=$(curl -s -H "$WC" $W/jobs | stateof "$J1"); [ "$st" = "succeeded" ] && break; sleep 2; done
ck "$st" "succeeded" "job-runs-and-succeeds"

# 2) engage e-stop, enqueue, confirm it stays queued (Wade is halted)
curl -s -H "$SD" -X POST $S/estop/engage -d '{"by":"taylor","reason":"acceptance test"}' >/dev/null
sleep 2
J2=$(curl -s -H "$WC" -H 'Content-Type: application/json' -X POST $W/jobs \
  -d "{\"kind\":\"diagnostics.health\",\"title\":\"blocked sweep\",\"payload\":{\"targets\":$TARGETS}}" | idof)
sleep 9
ck "$(curl -s -H "$WC" $W/jobs | stateof "$J2")" "queued" "estop-blocks-new-job"

# 3) release e-stop, confirm the blocked job now runs to success
curl -s -H "$SD" -X POST $S/estop/release -d '{"by":"taylor"}' >/dev/null
st=""; for i in $(seq 1 15); do st=$(curl -s -H "$WC" $W/jobs | stateof "$J2"); [ "$st" = "succeeded" ] && break; sleep 2; done
ck "$st" "succeeded" "release-resumes-job"

echo "================  Wade acceptance: PASS=$PASS FAIL=$FAIL  ================"
