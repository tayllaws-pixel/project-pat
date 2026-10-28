#!/usr/bin/env bash
# Install PWA/push/2FA Python deps in the Pat container, generate a VAPID keypair ONCE
# (kept across redeploys so existing push subscriptions stay valid), and wire the web-push
# env vars into /etc/pat.env. Idempotent. Run on the Proxmox HOST.
set -euo pipefail
CTID=102
log(){ printf '[setup-pat-pwa] %s\n' "$*"; }

log "1/3 install deps (pyotp, qrcode, pywebpush)"
pct exec "$CTID" -- /opt/pat-venv/bin/pip install -q pyotp qrcode pywebpush

log "2/3 generate VAPID keypair if missing"
pct exec "$CTID" -- bash -c '
set -e
PEM=/opt/pat/vapid_private.pem
if [ ! -s "$PEM" ]; then
  /opt/pat-venv/bin/python - <<PY
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import base64, pathlib
k = ec.generate_private_key(ec.SECP256R1())
pem = k.private_bytes(serialization.Encoding.PEM,
                      serialization.PrivateFormat.TraditionalOpenSSL,
                      serialization.NoEncryption())
pathlib.Path("/opt/pat/vapid_private.pem").write_bytes(pem)
raw = k.public_key().public_bytes(serialization.Encoding.X962,
                                  serialization.PublicFormat.UncompressedPoint)
pathlib.Path("/opt/pat/vapid_public.txt").write_text(
    base64.urlsafe_b64encode(raw).rstrip(b"=").decode())
print("generated new VAPID keypair")
PY
  chmod 600 "$PEM"
else
  echo "VAPID keypair already present - keeping it"
fi'

log "3/3 ensure web-push env vars in /etc/pat.env"
PUB="$(pct exec "$CTID" -- cat /opt/pat/vapid_public.txt)"
pct exec "$CTID" -- bash -c "
set -e
touch /etc/pat.env
grep -q '^PAT_VAPID_PRIVATE=' /etc/pat.env || echo 'PAT_VAPID_PRIVATE=/opt/pat/vapid_private.pem' >> /etc/pat.env
grep -q '^PAT_VAPID_SUB='     /etc/pat.env || echo 'PAT_VAPID_SUB=mailto:taylor@pat.local'          >> /etc/pat.env
sed -i '/^PAT_VAPID_PUBLIC=/d' /etc/pat.env
echo 'PAT_VAPID_PUBLIC=${PUB}' >> /etc/pat.env
chmod 600 /etc/pat.env"
log "VAPID public (browser) key: ${PUB}"
log "done"
