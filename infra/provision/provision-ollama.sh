#!/usr/bin/env bash
# Host script: install Ollama in CT 101, restore the preserved models from /var/lib/vz/ollama-backup
# (no re-download), configure the service for GPU + LAN, and verify. Idempotent. Run after create-inference.sh.
set -euo pipefail

CTID=101
BACKUP="${BACKUP:-/var/lib/vz/ollama-backup}"
ETC=/etc/project-pat
log(){ printf '[provision-ollama] %s\n' "$*"; }

pct exec "$CTID" -- /usr/local/bin/nvidia-smi -L >/dev/null 2>&1 || { echo "GPU not verified in CT $CTID; run create-inference.sh first"; exit 1; }

log "1/5 install Ollama"
pct exec "$CTID" -- bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v ollama >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates >/dev/null
    curl -fsSL https://ollama.com/install.sh | sh
  fi'

log "2/5 stop ollama for restore"
pct exec "$CTID" -- systemctl stop ollama 2>/dev/null || true

if [ -d "$BACKUP" ] && [ -n "$(ls -A "$BACKUP" 2>/dev/null)" ]; then
  log "3/5 restore models from ${BACKUP} ($(du -sh "$BACKUP" | cut -f1))"
  pct exec "$CTID" -- mkdir -p /usr/share/ollama
  tar -C "$BACKUP" -cf - . | pct exec "$CTID" -- tar -C /usr/share/ollama -xf -
  pct exec "$CTID" -- chown -R ollama:ollama /usr/share/ollama
else
  log "3/5 no backup at ${BACKUP}; skipping restore (models can be pulled later)"
fi

log "4/5 configure service (GPU, LAN bind, keep-alive)"
pct push "$CTID" "$ETC/secrets.env" /etc/project-pat/secrets.env --perms 0600 2>/dev/null || true
pct exec "$CTID" -- mkdir -p /etc/systemd/system/ollama.service.d
pct exec "$CTID" -- bash -c 'cat > /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_MODELS=/usr/share/ollama/.ollama/models"
Environment="OLLAMA_MAX_LOADED_MODELS=3"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_FLASH_ATTENTION=1"
EOF
systemctl daemon-reload
systemctl enable --now ollama'

log "5/5 verify"
sleep 6
echo "----- ollama list -----"
pct exec "$CTID" -- ollama list || true
IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
log "inference service at http://${IP}:11434"
