#!/usr/bin/env bash
# Redeploy the Pat backend + web UI (incl. PWA assets) from the repo working copy
# into CT 102 and restart. Run on the Proxmox HOST after:
#   cd /opt/project-pat && git pull
set -euo pipefail
CTID=102
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
log(){ printf '[redeploy-pat] %s\n' "$*"; }

log "1/4 sync backend"
pct exec "$CTID" -- mkdir -p /opt/pat/pat /opt/pat/web/icons
pct push "$CTID" "$REPO/services/pat/pat/__init__.py" /opt/pat/pat/__init__.py
pct push "$CTID" "$REPO/services/pat/pat/app.py"      /opt/pat/pat/app.py

log "2/4 sync web UI + PWA assets"
pct push "$CTID" "$REPO/services/pat/web/index.html"           /opt/pat/web/index.html
pct push "$CTID" "$REPO/services/pat/web/manifest.webmanifest" /opt/pat/web/manifest.webmanifest
pct push "$CTID" "$REPO/services/pat/web/sw.js"                /opt/pat/web/sw.js
for f in "$REPO"/services/pat/web/icons/*; do
  [ -f "$f" ] || continue
  pct push "$CTID" "$f" "/opt/pat/web/icons/$(basename "$f")"
done

log "3/4 restart pat"
pct exec "$CTID" -- systemctl restart pat
sleep 3

log "4/4 verify"
pct exec "$CTID" -- bash -c 'curl -s http://127.0.0.1:8080/healthz; echo; \
  for u in manifest.webmanifest sw.js icons/icon-512.png icons/apple-touch-icon.png; do \
    r=$(curl -s -o /dev/null -w "%{http_code} %{content_type}" "http://127.0.0.1:8080/$u"); \
    echo "  $r  /$u"; done'
log "done"
