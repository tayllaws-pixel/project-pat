#!/usr/bin/env bash
# Host script: create the image-generation LXC (CT 107), share the host GPU into it (same proven
# mechanism as the inference CT — copy the host's exact-version driver libs, no apt/DKMS), install
# PyTorch + diffusers, deploy the Pat ImageGen service (SDXL), and verify a real image renders.
# Idempotent. ADR-0010 (brownfield LVM-thin) / spec §23 (local image generation).
set -euo pipefail

CTID=107
NAME=imagegen
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
TEMPL_STORAGE="${TEMPL_STORAGE:-local}"
MEM=14336; CORES=6; DISK=80; BRIDGE=vmbr0
MODEL_ID="${IMAGEGEN_MODEL:-stabilityai/stable-diffusion-xl-base-1.0}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="/etc/pve/lxc/${CTID}.conf"
log(){ printf '[create-imagegen] %s\n' "$*"; }

DRV_VER="$(sed -nE 's/.*Module for x86_64 *([0-9.]+).*/\1/p' /proc/driver/nvidia/version | head -1)"
[ -n "$DRV_VER" ] || { echo "cannot read host NVIDIA driver version"; exit 1; }
log "host NVIDIA driver: ${DRV_VER}"

log "1/8 template"
TEMPL="$(pveam list "$TEMPL_STORAGE" 2>/dev/null | awk '/debian-13-standard/{print $1}' | head -1 || true)"
[ -n "${TEMPL:-}" ] || { echo "debian-13 template missing; run create-inference.sh first"; exit 1; }

log "2/8 create CT ${CTID}"
if ! pct config "$CTID" >/dev/null 2>&1; then
  pct create "$CTID" "$TEMPL" \
    --hostname "$NAME" --cores "$CORES" --cpuunits 1024 --memory "$MEM" --swap 2048 \
    --rootfs "${DISK_STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=1" \
    --nameserver "1.1.1.1 8.8.8.8" --searchdomain home.arpa \
    --unprivileged 1 --features nesting=1 --onboot 1 --start 0 \
    --tags "pat;gpu" --description "imagegen: SDXL image generation (Project PAT)"
else
  log "    CT ${CTID} exists — keeping"
fi

log "3/8 share GPU devices"
i=0
for d in /dev/nvidia0 /dev/nvidiactl /dev/nvidia-modeset /dev/nvidia-uvm /dev/nvidia-uvm-tools \
         /dev/nvidia-caps/nvidia-cap1 /dev/nvidia-caps/nvidia-cap2; do
  [ -e "$d" ] || continue
  if ! grep -qE "^dev[0-9]+: ${d}(,|$)" "$CONF"; then
    pct set "$CTID" "--dev${i}" "${d},gid=0,mode=0666"
  fi
  i=$((i+1))
done
pct status "$CTID" | grep -q running || pct start "$CTID"

log "4/8 prep container + wait for network"
pct push "$CTID" "$REPO/infra/provision/prep-container.sh" /opt/prep-container.sh --perms 0755
pct exec "$CTID" -- bash /opt/prep-container.sh
for _ in $(seq 1 30); do pct exec "$CTID" -- getent hosts huggingface.co >/dev/null 2>&1 && break; sleep 2; done

log "5/8 copy matching userspace driver libs (exact ${DRV_VER})"
pct exec "$CTID" -- bash -c '
  export DEBIAN_FRONTEND=noninteractive
  pkgs=$(dpkg -l 2>/dev/null | awk "/nvidia/{print \$2}")
  [ -n "$pkgs" ] && apt-get purge -y -qq $pkgs >/dev/null 2>&1 || true'
n=0
for f in /usr/lib/x86_64-linux-gnu/*.so."${DRV_VER}"; do
  [ -e "$f" ] || continue
  pct push "$CTID" "$f" "$f" --perms 0644
  n=$((n+1))
done
log "    copied ${n} driver libs"
pct exec "$CTID" -- ldconfig
pct push "$CTID" /usr/bin/nvidia-smi /usr/local/bin/nvidia-smi --perms 0755
pct exec "$CTID" -- /usr/local/bin/nvidia-smi -L || { echo "GPU NOT visible in CT ${CTID}"; exit 1; }

log "6/8 install torch + diffusers + pre-download model (downloads several GB — be patient)"
pct push "$CTID" "$REPO/infra/provision/install-imagegen.sh" /opt/install-imagegen.sh --perms 0755
pct exec "$CTID" -- bash -c "IMAGEGEN_MODEL='${MODEL_ID}' HF_HOME=/opt/imagegen-cache bash /opt/install-imagegen.sh"

log "7/8 deploy service + systemd"
INFER_IP="$(pct exec 101 -- hostname -I | awk '{print $1}')"
[ -n "$INFER_IP" ] || { echo "inference (101) must be running to resolve OLLAMA_URL"; exit 1; }
log "    inference (Ollama) = ${INFER_IP}:11434"
pct exec "$CTID" -- mkdir -p /opt/imagegen
pct push "$CTID" "$REPO/services/imagegen/app.py" /opt/imagegen/app.py
pct exec "$CTID" -- bash -c "cat > /etc/imagegen.env <<EOF
IMAGEGEN_MODEL=${MODEL_ID}
HF_HOME=/opt/imagegen-cache
HF_HUB_DISABLE_TELEMETRY=1
OLLAMA_URL=http://${INFER_IP}:11434
IMAGEGEN_FREE_VRAM=1
EOF"
pct exec "$CTID" -- bash -c 'cat > /etc/systemd/system/imagegen.service <<EOF
[Unit]
Description=Pat ImageGen (SDXL)
After=network-online.target

[Service]
EnvironmentFile=/etc/imagegen.env
WorkingDirectory=/opt/imagegen
ExecStart=/opt/imagegen-venv/bin/uvicorn app:app --host 0.0.0.0 --port 8188 --timeout-keep-alive 600
Restart=always
RestartSec=5
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now imagegen'

log "8/8 verify + generate smoke test"
sleep 8
IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo "----- healthz -----"
pct exec "$CTID" -- bash -c 'curl -s http://127.0.0.1:8188/healthz' || true
echo
log "    running a test generation (first call loads the model; may take 1-2 min)…"
pct exec "$CTID" -- bash -c 'curl -s -X POST http://127.0.0.1:8188/generate \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"a red apple on a wooden table, studio photograph\",\"steps\":20,\"width\":768,\"height\":768}" \
  -o /tmp/smoke.png -w "    HTTP %{http_code}  %{time_total}s\n" --max-time 360' || true
pct exec "$CTID" -- /opt/imagegen-venv/bin/python - <<'PY' || true
import os
p="/tmp/smoke.png"
if os.path.exists(p):
    d=open(p,"rb").read()
    print(f"    smoke.png: {len(d)} bytes, PNG={d[:4]==b'\x89PNG'}")
else:
    print("    smoke.png not produced")
PY
echo "========================================================"
echo "  ImageGen live at:  http://${IP}:8188   (model: ${MODEL_ID})"
echo "  Endpoints: /generate /img2img /inpaint /healthz /info"
echo "========================================================"
