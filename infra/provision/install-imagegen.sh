#!/usr/bin/env bash
# Install PyTorch (CUDA build) + the diffusers stack INSIDE the imagegen container, then pre-download
# the image model into a persistent HF cache. Verifies torch can see the GPU BEFORE the multi-GB model
# download, so a driver/torch mismatch surfaces early instead of after a long wait. Idempotent.
set -e
export DEBIAN_FRONTEND=noninteractive
export PIP_ROOT_USER_ACTION=ignore
export HF_HOME="${HF_HOME:-/opt/imagegen-cache}"
MODEL_ID="${IMAGEGEN_MODEL:-stabilityai/stable-diffusion-xl-base-1.0}"

apt-get update -qq
apt-get install -y -qq python3 python3-venv git ca-certificates libgl1 libglib2.0-0 curl >/dev/null

test -d /opt/imagegen-venv || python3 -m venv /opt/imagegen-venv
PY=/opt/imagegen-venv/bin/python
PIP=/opt/imagegen-venv/bin/pip
$PIP install -q --upgrade pip

# 1) PyTorch — CUDA 12.4 wheels. The runtime they bundle is forward-compatible with the newer host
#    driver, and only needs libcuda.so (copied in from the host by create-imagegen.sh).
if ! $PY -c 'import torch' 2>/dev/null; then
  echo "[install-imagegen] installing torch (cu124)…"
  $PIP install -q torch torchvision --index-url https://download.pytorch.org/whl/cu124
fi
echo "[install-imagegen] torch $($PY -c 'import torch;print(torch.__version__)')"

CUDA_OK="$($PY -c 'import torch;print(torch.cuda.is_available())' 2>/dev/null || echo False)"
echo "[install-imagegen] cuda available: ${CUDA_OK}"
if [ "$CUDA_OK" != "True" ]; then
  echo "[install-imagegen] ERROR: torch cannot see the GPU. Stopping before the model download."
  $PY -c 'import torch; torch.zeros(1).cuda()' 2>&1 | tail -5 || true
  exit 2
fi
echo "[install-imagegen] GPU: $($PY -c 'import torch;print(torch.cuda.get_device_name(0))')"

# 2) diffusers stack
$PIP install -q diffusers transformers accelerate safetensors sentencepiece \
  fastapi "uvicorn[standard]" pillow

# 3) pre-download the model into the persistent HF cache so the first user request isn't a long wait.
mkdir -p "$HF_HOME"
echo "[install-imagegen] pre-downloading ${MODEL_ID} into ${HF_HOME} (several GB)…"
HF_HOME="$HF_HOME" HF_HUB_DISABLE_TELEMETRY=1 IMAGEGEN_MODEL="$MODEL_ID" $PY - <<'PY'
import os, torch
from diffusers import AutoPipelineForText2Image
mid = os.environ["IMAGEGEN_MODEL"]
kw = dict(torch_dtype=torch.float16, use_safetensors=True)
try:
    AutoPipelineForText2Image.from_pretrained(mid, variant="fp16", **kw)
    print("downloaded (fp16 variant)")
except Exception as e:
    print("fp16 variant unavailable, falling back to default weights:", repr(e)[:160])
    AutoPipelineForText2Image.from_pretrained(mid, **kw)
    print("downloaded (default weights)")
PY

echo "[install-imagegen] done."
