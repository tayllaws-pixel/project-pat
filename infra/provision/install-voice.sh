#!/usr/bin/env bash
# Install local voice into the Pat container (CT 102): faster-whisper (speech-to-text) + Piper (text-to-
# speech) + ffmpeg, all CPU-only so they never touch the GPU. Voice models live in /opt/pat/voice.
# Idempotent. Run after create-pat.sh. Change the voice by setting PAT_PIPER_VOICE in /etc/pat.env.
set -euo pipefail

CTID=102
VOICE="${PAT_PIPER_VOICE:-en_US-amy-medium}"
WMODEL="${PAT_WHISPER_MODEL:-base}"
log(){ printf '[install-voice] %s\n' "$*"; }

# derive the HuggingFace path: en_US-amy-medium -> en/en_US/amy/medium
lang="${VOICE%%-*}"; rest="${VOICE#*-}"; name="${rest%%-*}"; qual="${rest##*-}"; langfam="${lang%%_*}"
HF="https://huggingface.co/rhasspy/piper-voices/resolve/main/${langfam}/${lang}/${name}/${qual}/${VOICE}"

log "1/4 system deps (ffmpeg)"
pct exec "$CTID" -- bash -c 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq ffmpeg >/dev/null'

log "2/4 python deps (faster-whisper, piper-tts)"
pct exec "$CTID" -- /opt/pat-venv/bin/pip install -q faster-whisper piper-tts

log "3/4 download Piper voice ${VOICE}"
pct exec "$CTID" -- bash -c "mkdir -p /opt/pat/voice; cd /opt/pat/voice; \
  curl -fsSL -o '${VOICE}.onnx'      '${HF}.onnx'; \
  curl -fsSL -o '${VOICE}.onnx.json' '${HF}.onnx.json'; \
  ls -la /opt/pat/voice"

log "4/4 warm the Whisper model (${WMODEL}) and set the voice in /etc/pat.env"
pct exec "$CTID" -- /opt/pat-venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('${WMODEL}', device='cpu', compute_type='int8'); print('whisper ${WMODEL} ready')"
pct exec "$CTID" -- bash -c "grep -q '^PAT_PIPER_VOICE=' /etc/pat.env || echo 'PAT_PIPER_VOICE=${VOICE}' >> /etc/pat.env; \
  grep -q '^PAT_VOICE_DIR=' /etc/pat.env || echo 'PAT_VOICE_DIR=/opt/pat/voice' >> /etc/pat.env; \
  sed -i 's#^PAT_PIPER_VOICE=.*#PAT_PIPER_VOICE=${VOICE}#' /etc/pat.env"

pct exec "$CTID" -- systemctl restart pat
sleep 3
echo "----- voice ready check -----"
pct exec "$CTID" -- /opt/pat-venv/bin/python -c "import faster_whisper, piper; from pathlib import Path; print('libs ok; voice present:', Path('/opt/pat/voice/${VOICE}.onnx').exists())" || true
echo
log "voice installed (${VOICE}). Change PAT_PIPER_VOICE in /etc/pat.env + re-run to swap voices."
