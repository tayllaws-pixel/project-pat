"""
Pat ImageGen — a small, purpose-built local image service for Project PAT.

Runs Stable Diffusion XL on the home GPU via HuggingFace `diffusers` and exposes a
clean REST API that Pat's backend drives:

    POST /generate   text -> image            (txt2img)
    POST /img2img    image + text -> image     (transform an existing image)
    POST /inpaint    image + mask + text       (edit a masked region)
    GET  /healthz     liveness + CUDA status
    GET  /info        model + device info

Design choices (see ADR-0007 resource policy):
  * `enable_model_cpu_offload()` keeps the ~7GB of SDXL weights in CPU RAM and streams
    submodules to the GPU only during a step. Peak VRAM stays low (~3-4GB) so image
    generation can coexist with Ollama's LLMs on the single 16GB card instead of
    fighting them for VRAM. The cost is slower generation (CPU<->GPU transfers).
  * A single process-wide lock serialises GPU work: one image at a time on one GPU.
  * img2img and inpaint are built with `from_pipe()` so they reuse the already-loaded
    weights (no second copy in RAM or VRAM).

Images are returned as raw PNG bytes; the caller owns storage and history.
"""
import base64
import io
import json
import os
import threading
import time
import urllib.request
from typing import Optional

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from PIL import Image
from pydantic import BaseModel, Field

MODEL_ID = os.environ.get("IMAGEGEN_MODEL", "stabilityai/stable-diffusion-xl-base-1.0")
MAX_STEPS = int(os.environ.get("IMAGEGEN_MAX_STEPS", "60"))
MAX_DIM = int(os.environ.get("IMAGEGEN_MAX_DIM", "1280"))
DTYPE = torch.float16

# VRAM coordination: the GPU is shared with Ollama (inference) on one 16GB card. Before a render we
# ask Ollama to unload its models so SDXL has room; after, we release our cache so chat can reload.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "").rstrip("/")
FREE_VRAM = os.environ.get("IMAGEGEN_FREE_VRAM", "1").lower() not in ("0", "false", "no")

app = FastAPI(title="Pat ImageGen", version="0.1.0")

_lock = threading.Lock()          # one GPU -> serialise all generation
_load_lock = threading.Lock()     # guard the (slow) first load
_t2i = None
_i2i = None
_inpaint = None
_loaded_at: Optional[float] = None


def _load():
    """Load SDXL once and derive img2img/inpaint from the same weights."""
    global _t2i, _i2i, _inpaint, _loaded_at
    if _t2i is not None:
        return
    with _load_lock:
        if _t2i is not None:
            return
        from diffusers import (
            AutoPipelineForImage2Image,
            AutoPipelineForInpainting,
            AutoPipelineForText2Image,
            StableDiffusionXLPipeline,
        )
        kw = dict(torch_dtype=DTYPE, use_safetensors=True)
        if MODEL_ID.endswith(".safetensors") and os.path.exists(MODEL_ID):
            # A single-file SDXL finetune checkpoint (the kind used in ComfyUI/Automatic1111).
            t2i = StableDiffusionXLPipeline.from_single_file(MODEL_ID, torch_dtype=DTYPE, use_safetensors=True)
        else:
            # A HuggingFace diffusers repo id. Prefer the fp16 weight variant when it ships one.
            try:
                t2i = AutoPipelineForText2Image.from_pretrained(MODEL_ID, variant="fp16", **kw)
            except Exception:
                t2i = AutoPipelineForText2Image.from_pretrained(MODEL_ID, **kw)
        t2i.enable_model_cpu_offload()
        try:
            t2i.enable_vae_slicing()
            t2i.enable_vae_tiling()
        except Exception:
            pass
        i2i = AutoPipelineForImage2Image.from_pipe(t2i)
        inpaint = AutoPipelineForInpainting.from_pipe(t2i)
        _t2i, _i2i, _inpaint, _loaded_at = t2i, i2i, inpaint, time.time()


def _http_json(method: str, url: str, payload=None, timeout: float = 10.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


def _free_ollama_vram():
    """Best-effort: unload Ollama's loaded models so the GPU is free for a render."""
    if not (FREE_VRAM and OLLAMA_URL):
        return
    try:
        ps = _http_json("GET", f"{OLLAMA_URL}/api/ps", timeout=5)
    except Exception:
        return
    freed = False
    for m in (ps.get("models") or []):
        name = m.get("name") or m.get("model")
        if not name:
            continue
        try:
            _http_json("POST", f"{OLLAMA_URL}/api/generate",
                       {"model": name, "keep_alive": 0}, timeout=30)
            freed = True
        except Exception:
            pass
    if freed:
        time.sleep(0.6)  # let the driver reclaim the freed VRAM
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass


def _run(call):
    """Serialise GPU work, free peer VRAM first, always release our cache after."""
    with _lock:
        _free_ollama_vram()
        try:
            return call()
        except torch.cuda.OutOfMemoryError:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            raise HTTPException(
                status_code=503,
                detail="GPU out of memory — another model is using the card. Try again in a moment.",
            )
        finally:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _decode_image(data: str) -> Image.Image:
    """Accept a data URL or bare base64 PNG/JPEG."""
    if "," in data and data.strip().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad image data: {e}")


def _decode_mask(data: str) -> Image.Image:
    if "," in data and data.strip().startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
        return Image.open(io.BytesIO(raw)).convert("L")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad mask data: {e}")


def _clamp_dim(n: int) -> int:
    n = max(512, min(MAX_DIM, int(n)))
    return (n // 8) * 8  # SDXL needs multiples of 8


def _fit_sdxl(img: Image.Image, target: int = 1024) -> Image.Image:
    """Resize so the longer side ~= target px, both dims multiples of 8 within [512, MAX_DIM], aspect
    preserved. SDXL only behaves near ~1024px; feeding img2img a large/tall photo at its native size is
    what produces duplicated faces and melted limbs. Editing must fit the input into this range first."""
    w, h = img.size
    scale = target / float(max(w, h))
    nw = int(round(w * scale))
    nh = int(round(h * scale))
    nw = max(512, min(MAX_DIM, nw - nw % 8))
    nh = max(512, min(MAX_DIM, nh - nh % 8))
    if (nw, nh) == (w, h):
        return img
    return img.resize((nw, nh), Image.LANCZOS)


def _generator(seed: Optional[int]):
    if seed is None:
        return None
    return torch.Generator(device="cuda").manual_seed(int(seed))


NEG_DEFAULT = "low quality, blurry, distorted, deformed, text, watermark, signature"


class GenReq(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    negative_prompt: Optional[str] = NEG_DEFAULT
    steps: int = 28
    guidance: float = 6.0
    width: int = 1024
    height: int = 1024
    seed: Optional[int] = None


class Img2ImgReq(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    image: str
    negative_prompt: Optional[str] = NEG_DEFAULT
    strength: float = 0.6
    steps: int = 30
    guidance: float = 6.0
    seed: Optional[int] = None


class InpaintReq(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    image: str
    mask: str
    negative_prompt: Optional[str] = NEG_DEFAULT
    strength: float = 0.85
    steps: int = 30
    guidance: float = 7.0
    seed: Optional[int] = None


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "cuda": torch.cuda.is_available(),
        "model": MODEL_ID,
        "loaded": _t2i is not None,
    }


@app.get("/info")
def info():
    dev = None
    vram = None
    if torch.cuda.is_available():
        dev = torch.cuda.get_device_name(0)
        vram = round(torch.cuda.get_device_properties(0).total_memory / 1e9, 1)
    return {
        "model": MODEL_ID,
        "device": dev,
        "vram_gb": vram,
        "loaded": _t2i is not None,
        "max_steps": MAX_STEPS,
        "max_dim": MAX_DIM,
    }


@app.post("/warmup")
def warmup():
    _load()
    return {"loaded": True, "model": MODEL_ID}


@app.post("/generate")
def generate(r: GenReq):
    _load()
    steps = max(1, min(MAX_STEPS, r.steps))
    w, h = _clamp_dim(r.width), _clamp_dim(r.height)
    t0 = time.time()
    img = _run(lambda: _t2i(
        prompt=r.prompt,
        negative_prompt=r.negative_prompt or None,
        num_inference_steps=steps,
        guidance_scale=r.guidance,
        width=w,
        height=h,
        generator=_generator(r.seed),
    ).images[0])
    return Response(
        content=_png(img),
        media_type="image/png",
        headers={"X-Gen-Seconds": f"{time.time() - t0:.1f}", "X-Gen-Steps": str(steps)},
    )


@app.post("/img2img")
def img2img(r: Img2ImgReq):
    _load()
    steps = max(1, min(MAX_STEPS, r.steps))
    init = _fit_sdxl(_decode_image(r.image))
    strength = max(0.05, min(1.0, r.strength))
    t0 = time.time()
    img = _run(lambda: _i2i(
        prompt=r.prompt,
        negative_prompt=r.negative_prompt or None,
        image=init,
        strength=strength,
        num_inference_steps=steps,
        guidance_scale=r.guidance,
        generator=_generator(r.seed),
    ).images[0])
    return Response(
        content=_png(img),
        media_type="image/png",
        headers={"X-Gen-Seconds": f"{time.time() - t0:.1f}"},
    )


@app.post("/inpaint")
def inpaint(r: InpaintReq):
    _load()
    steps = max(1, min(MAX_STEPS, r.steps))
    init = _fit_sdxl(_decode_image(r.image))
    mask = _decode_mask(r.mask).resize(init.size)
    strength = max(0.05, min(1.0, r.strength))
    t0 = time.time()
    img = _run(lambda: _inpaint(
        prompt=r.prompt,
        negative_prompt=r.negative_prompt or None,
        image=init,
        mask_image=mask,
        strength=strength,
        num_inference_steps=steps,
        guidance_scale=r.guidance,
        generator=_generator(r.seed),
    ).images[0])
    return Response(
        content=_png(img),
        media_type="image/png",
        headers={"X-Gen-Seconds": f"{time.time() - t0:.1f}"},
    )
