"""Pat v0 — the assistant backend (PAT-040/041/042).

A small FastAPI service: password login (signed session cookie), conversations and messages persisted to
the `pat` database, and chat streamed from the local Ollama inference service. Clean, synchronous, and
dependency-light on purpose; multi-user auth (the sessions table), memory extraction, projects, and the
approval inbox come later. Config is entirely from environment variables.
"""
from __future__ import annotations

import base64
import base64
import hashlib
import hmac
import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pyotp
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# ---------------------------------------------------------------- config
DB_DSN       = os.environ["PAT_DB_DSN"]                       # postgresql://pat_app:...@infra-core/pat
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
SECRET       = os.environ["PAT_SESSION_SECRET"].encode()
ADMIN_USER   = os.environ.get("PAT_ADMIN_USER", "taylor")
ADMIN_PASS   = os.environ.get("PAT_ADMIN_PASSWORD", "")       # required to log in; set at provision time
DEFAULT_MODEL = os.environ.get("PAT_DEFAULT_MODEL", "qwen3:8b")
SYSTEM_PROMPT = os.environ.get("PAT_SYSTEM_PROMPT",
    "You are Pat, a capable and professional personal assistant. You run entirely locally on Taylor's own "
    "home server — a Proxmox machine with an NVIDIA RTX 4070 Ti SUPER GPU — so everything stays private on "
    "his hardware; you are not a cloud service. Be helpful, clear, and concise, and honest about what you "
    "don't know. When asked what you are, say you are Pat, Taylor's local assistant.")
SESSION_TTL  = int(os.environ.get("PAT_SESSION_TTL", str(30 * 24 * 3600)))
EMBED_MODEL   = os.environ.get("PAT_EMBED_MODEL", "nomic-embed-text")
EXTRACT_MODEL = os.environ.get("PAT_EXTRACT_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
EMBED_DIM     = 1024          # storage width of memory_embeddings.embedding; nomic is 768 (zero-padded)
MEM_TOPK      = int(os.environ.get("PAT_MEMORY_TOPK", "6"))
IMAGEGEN_URL  = os.environ.get("IMAGEGEN_URL", "").rstrip("/")   # http://imagegen:8188; "" disables images
IMAGES_DIR    = Path(os.environ.get("PAT_IMAGES_DIR", "/opt/pat/images"))
IMG_MODEL_NAME = os.environ.get("PAT_IMAGE_MODEL", "stable-diffusion-xl")
SENTINEL_URL   = os.environ.get("SENTINEL_URL", "").rstrip("/")        # safety plane; "" disables the inbox
SENTINEL_TOKEN = os.environ.get("SENTINEL_DECIDER_TOKEN", "")          # Pat holds the decider token
DEPLOY_URL     = os.environ.get("DEPLOY_URL", "").rstrip("/")          # deploy controller; "" = no auto-ship
DEPLOY_TOKEN   = os.environ.get("DEPLOY_CONTROLLER_TOKEN", "")
VOICE_DIR      = Path(os.environ.get("PAT_VOICE_DIR", "/opt/pat/voice"))    # piper voice models live here
WHISPER_MODEL  = os.environ.get("PAT_WHISPER_MODEL", "base")                # faster-whisper model size
PIPER_VOICE    = os.environ.get("PAT_PIPER_VOICE", "en_US-amy-medium")      # Pat's speaking voice
WEB_DIR      = Path(__file__).resolve().parent.parent / "web"
# Web Push (VAPID): browser push notifications for installed PWAs (iOS 16.4+, Android, desktop).
VAPID_PRIVATE = os.environ.get("PAT_VAPID_PRIVATE", "")   # PEM private key (generated at provision time)
VAPID_PUBLIC  = os.environ.get("PAT_VAPID_PUBLIC", "")    # base64url uncompressed P-256 public key for the browser
VAPID_SUB     = os.environ.get("PAT_VAPID_SUB", "mailto:pat@local")
# Push a notification when a new approval appears in Sentinel. Seconds between polls; 0 disables.
APPROVAL_PUSH_POLL = int(os.environ.get("PAT_APPROVAL_PUSH_POLL", "15"))
# Refresh the session cookie when it's older than this (rolling 30-day window keeps active users signed in).
SESSION_REFRESH_AFTER = int(os.environ.get("PAT_SESSION_REFRESH_AFTER", str(24 * 3600)))

pool = ConnectionPool(DB_DSN, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=False)
app = FastAPI(title="Pat")


# ---------------------------------------------------------------- auth helpers
def _hash_pw(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)


def verify_password(password: str) -> bool:
    # v0: single admin, credential from env. Constant-time compare.
    if not ADMIN_PASS:
        return False
    return hmac.compare_digest(password, ADMIN_PASS)


def make_cookie(username: str) -> str:
    issued = str(int(time.time()))
    msg = f"{username}.{issued}".encode()
    sig = hmac.new(SECRET, msg, hashlib.sha256).hexdigest()
    return f"{username}.{issued}.{sig}"


def read_cookie(value: str | None) -> str | None:
    if not value:
        return None
    try:
        username, issued, sig = value.rsplit(".", 2)
    except ValueError:
        return None
    expected = hmac.new(SECRET, f"{username}.{issued}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if int(time.time()) - int(issued) > SESSION_TTL:
        return None
    return username


def require_user(request: Request) -> dict:
    username = read_cookie(request.cookies.get("pat_session"))
    if username != ADMIN_USER:
        raise HTTPException(status_code=401, detail="not authenticated")
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, username, display_name, role, totp_enabled FROM users WHERE username=%s",
            (username,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="unknown user")
    return row


# ---- session cookie: issue + rolling refresh --------------------------------
def set_session_cookie(resp, username: str) -> None:
    resp.set_cookie("pat_session", make_cookie(username), httponly=True, samesite="lax",
                    max_age=SESSION_TTL, secure=False)   # secure=False: served over tailnet/LAN http


def _cookie_issued(value: str | None) -> int | None:
    if not value:
        return None
    try:
        _, issued, _ = value.rsplit(".", 2)
        return int(issued)
    except Exception:
        return None


def refresh_session(request: Request, resp) -> None:
    """Roll the 30-day cookie forward once it's older than SESSION_REFRESH_AFTER. An actively-used
    install (it calls /api/me on every launch) stays signed in; only 30 days idle forces a re-login."""
    raw = request.cookies.get("pat_session")
    if read_cookie(raw) != ADMIN_USER:
        return
    issued = _cookie_issued(raw)
    if issued is None or (int(time.time()) - issued) >= SESSION_REFRESH_AFTER:
        set_session_cookie(resp, ADMIN_USER)


# ---- TOTP (two-factor authentication) ---------------------------------------
def _user_totp(username: str) -> dict:
    with pool.connection() as conn:
        row = conn.execute("SELECT totp_secret, totp_enabled FROM users WHERE username=%s",
                           (username,)).fetchone()
    return row or {"totp_secret": None, "totp_enabled": False}


def _totp_ok(secret: str | None, code: str | None) -> bool:
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(str(code).strip().replace(" ", ""), valid_window=1)
    except Exception:
        return False


def _qr_svg(data: str) -> str:
    """Render an otpauth URI as a scalable SVG QR (no Pillow needed)."""
    import qrcode
    from qrcode.image.svg import SvgPathImage
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(buf)
    return buf.getvalue().decode("utf-8")


# ---------------------------------------------------------------- lifecycle
@app.on_event("startup")
def _startup() -> None:
    pool.open()
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO users (username, display_name, role) VALUES (%s, %s, 'admin') "
            "ON CONFLICT (username) DO NOTHING",
            (ADMIN_USER, ADMIN_USER.capitalize()))
        conn.commit()
    # Note: the 2FA columns + push_subscriptions table are created by migration 0003 (applied as the
    # postgres owner inside infra-core); the pat_app role can't ALTER tables it doesn't own.
    # Background: notify the phone when a new approval appears in Sentinel.
    if APPROVAL_PUSH_POLL > 0:
        threading.Thread(target=_approval_poller, daemon=True).start()


# ---------------------------------------------------------------- auth routes
@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    code = body.get("code") or ""
    if username != ADMIN_USER or not verify_password(password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    tf = _user_totp(username)
    if tf.get("totp_enabled"):
        if not code:
            # password is correct but a 2FA code is required — the UI reveals the code field on this
            raise HTTPException(status_code=401, detail="2fa_required")
        if not _totp_ok(tf.get("totp_secret"), code):
            raise HTTPException(status_code=401, detail="2fa_invalid")
    resp = JSONResponse({"ok": True, "user": username})
    set_session_cookie(resp, username)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("pat_session")
    return resp


@app.get("/api/me")
def me(request: Request, user: dict = Depends(require_user)):
    resp = JSONResponse({"username": user["username"], "display_name": user["display_name"],
                         "role": user["role"], "totp_enabled": bool(user.get("totp_enabled"))})
    refresh_session(request, resp)     # roll the session forward so active use never logs out
    return resp


# ---- two-factor management (all require an authenticated session) -----------
@app.get("/api/auth/2fa/status")
def twofa_status(user: dict = Depends(require_user)):
    return {"enabled": bool(user.get("totp_enabled"))}


@app.post("/api/auth/2fa/setup")
def twofa_setup(user: dict = Depends(require_user)):
    # fresh pending secret; not active until confirmed with a valid code via /enable
    secret = pyotp.random_base32()
    with pool.connection() as conn:
        conn.execute("UPDATE users SET totp_secret=%s, totp_enabled=false WHERE username=%s",
                     (secret, user["username"]))
        conn.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name="Pat")
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": _qr_svg(uri)}


@app.post("/api/auth/2fa/enable")
async def twofa_enable(request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    tf = _user_totp(user["username"])
    if not tf.get("totp_secret"):
        raise HTTPException(status_code=400, detail="run setup first")
    if not _totp_ok(tf.get("totp_secret"), body.get("code")):
        raise HTTPException(status_code=400, detail="invalid code")
    with pool.connection() as conn:
        conn.execute("UPDATE users SET totp_enabled=true WHERE username=%s", (user["username"],))
        conn.commit()
    return {"ok": True, "enabled": True}


@app.post("/api/auth/2fa/disable")
async def twofa_disable(request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    tf = _user_totp(user["username"])
    if tf.get("totp_enabled") and not _totp_ok(tf.get("totp_secret"), body.get("code")):
        raise HTTPException(status_code=400, detail="invalid code")
    with pool.connection() as conn:
        conn.execute("UPDATE users SET totp_secret=NULL, totp_enabled=false WHERE username=%s",
                     (user["username"],))
        conn.commit()
    return {"ok": True, "enabled": False}


# ---------------------------------------------------------------- models
@app.get("/api/models")
def models(user: dict = Depends(require_user)):
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        r.raise_for_status()
        names = sorted(m["name"] for m in r.json().get("models", []))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"inference service unreachable: {e}")
    return {"models": names, "default": DEFAULT_MODEL if DEFAULT_MODEL in names else (names[0] if names else None)}


# ---------------------------------------------------------------- memory (PAT-043/044)
def _embed(text: str):
    """Return a padded embedding vector (EMBED_DIM) for `text`, or None if the embed model is unavailable."""
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/embed", json={"model": EMBED_MODEL, "input": text[:4000]}, timeout=30)
        r.raise_for_status()
        v = (r.json().get("embeddings") or [[]])[0]
        if not v:
            return None
        return (list(v) + [0.0] * EMBED_DIM)[:EMBED_DIM]
    except Exception:
        return None


def _veclit(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def retrieve_memories(user_id, query: str, k: int = MEM_TOPK) -> list[str]:
    qv = _embed(query)
    if qv is None:
        return []
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT mv.content FROM memory_embeddings me "
            "JOIN memory_versions mv ON mv.id = me.memory_version_id "
            "JOIN memories m ON m.id = mv.memory_id "
            "WHERE m.user_id=%s AND mv.deleted_at IS NULL "
            "ORDER BY me.embedding <=> %s::vector LIMIT %s",
            (user_id, _veclit(qv), k)).fetchall()
    return [r["content"] for r in rows]


_EXTRACT_PROMPT = (
    "You maintain long-term memory about the user (Taylor). From the exchange below, extract any NEW, "
    "durable facts worth remembering: stable preferences, ongoing projects, personal details, goals, and "
    "decisions. Ignore questions, trivia, and anything ephemeral. Reply with ONLY a compact JSON array of "
    "short fact strings, e.g. [\"Prefers concise answers\",\"Working on a home AI server\"]. "
    "If there is nothing durable, reply exactly []."
)


def extract_memories(user_id, conversation_id, user_msg: str, assistant_msg: str) -> None:
    """Background: ask a fast model for durable facts in this exchange and store them with embeddings."""
    try:
        prompt = f"{_EXTRACT_PROMPT}\n\nUser: {user_msg}\nAssistant: {assistant_msg}"
        r = httpx.post(f"{OLLAMA_URL}/api/generate",
                       json={"model": EXTRACT_MODEL, "prompt": prompt, "stream": False,
                             "think": False, "options": {"temperature": 0}}, timeout=90)
        r.raise_for_status()
        text = re.sub(r"<think>.*?</think>", "", r.json().get("response", ""), flags=re.S)
        m = re.search(r"\[.*\]", text, re.S)
        facts = json.loads(m.group(0)) if m else []
    except Exception:
        return
    for fact in facts:
        if not isinstance(fact, str) or not fact.strip():
            continue
        fact = fact.strip()[:500]
        ev = _embed(fact)
        try:
            with pool.connection() as conn:
                mem = conn.execute("INSERT INTO memories (user_id, kind) VALUES (%s,'personal') RETURNING id",
                                   (user_id,)).fetchone()
                ver = conn.execute(
                    "INSERT INTO memory_versions (memory_id, version, content, claim_status, extracted_by, "
                    "source_conversation_id) VALUES (%s, 1, %s, 'considered', %s, %s) RETURNING id",
                    (mem["id"], fact, EXTRACT_MODEL, conversation_id)).fetchone()
                if ev is not None:
                    conn.execute("INSERT INTO memory_embeddings (memory_version_id, model, dims, embedding) "
                                 "VALUES (%s,%s,768,%s::vector)", (ver["id"], EMBED_MODEL, _veclit(ev)))
                conn.commit()
        except Exception:
            continue


@app.get("/api/memories")
def list_memories(user: dict = Depends(require_user)):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT m.id, mv.content, mv.claim_status, mv.created_at FROM memories m "
            "JOIN memory_versions mv ON mv.memory_id=m.id "
            "WHERE m.user_id=%s AND mv.deleted_at IS NULL ORDER BY mv.created_at DESC LIMIT 500",
            (user["id"],)).fetchall()
    return {"memories": [{"id": str(r["id"]), "content": r["content"], "status": r["claim_status"],
                          "created_at": r["created_at"].isoformat()} for r in rows]}


@app.delete("/api/memories/{mid}")
def delete_memory(mid: str, user: dict = Depends(require_user)):
    try:
        uuid.UUID(mid)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE memory_versions SET deleted_at=now() WHERE memory_id=%s "
            "AND memory_id IN (SELECT id FROM memories WHERE user_id=%s)", (mid, user["id"]))
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- conversations
@app.get("/api/conversations")
def list_conversations(user: dict = Depends(require_user)):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at FROM conversations "
            "WHERE user_id=%s AND deleted_at IS NULL AND archived_at IS NULL "
            "ORDER BY created_at DESC LIMIT 200", (user["id"],)).fetchall()
    return {"conversations": [{"id": str(r["id"]), "title": r["title"] or "New chat",
                               "created_at": r["created_at"].isoformat()} for r in rows]}


@app.post("/api/conversations")
async def create_conversation(request: Request, user: dict = Depends(require_user)):
    body = await request.json() if await _has_body(request) else {}
    title = (body.get("title") or "New chat")[:200]
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id, created_at",
            (user["id"], title)).fetchone()
        conn.commit()
    return {"id": str(row["id"]), "title": title, "created_at": row["created_at"].isoformat()}


@app.get("/api/conversations/{cid}")
def get_conversation(cid: str, user: dict = Depends(require_user)):
    with pool.connection() as conn:
        _own(conn, cid, user)
        rows = conn.execute(
            "SELECT role, content, model, created_at FROM messages "
            "WHERE conversation_id=%s ORDER BY seq", (cid,)).fetchall()
    return {"messages": [{"role": r["role"], "content": r["content"], "model": r["model"],
                          "created_at": r["created_at"].isoformat()} for r in rows]}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, user: dict = Depends(require_user)):
    with pool.connection() as conn:
        _own(conn, cid, user)
        conn.execute("UPDATE conversations SET deleted_at=now() WHERE id=%s", (cid,))
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- chat (streaming)
@app.post("/api/conversations/{cid}/chat")
async def chat(cid: str, request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    content = (body.get("content") or "").strip()
    model = body.get("model") or DEFAULT_MODEL
    if not content:
        raise HTTPException(status_code=400, detail="empty message")

    with pool.connection() as conn:
        _own(conn, cid, user)
        # persist the user message
        conn.execute(
            "INSERT INTO messages (conversation_id, role, actor_id, content) VALUES (%s,'user',%s,%s)",
            (cid, user["username"], content))
        # first user message becomes the conversation title
        conn.execute("UPDATE conversations SET title=%s WHERE id=%s AND (title IS NULL OR title='New chat')",
                     (content[:60], cid))
        conn.commit()
        history = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY seq", (cid,)).fetchall()

    # recall relevant long-term memories and fold them into the system prompt
    mems = retrieve_memories(user["id"], content)
    sys_prompt = SYSTEM_PROMPT
    if mems:
        sys_prompt += ("\n\nWhat you already know about Taylor (use only when relevant, don't force it):\n"
                       + "\n".join(f"- {m}" for m in mems))
    ollama_messages = ([{"role": "system", "content": sys_prompt}] +
                       [{"role": r["role"], "content": r["content"]} for r in history])

    def generate():
        assistant = []
        try:
            with httpx.stream("POST", f"{OLLAMA_URL}/api/chat", timeout=None,
                              json={"model": model, "messages": ollama_messages, "stream": True}) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        assistant.append(piece)
                        yield piece
                    if chunk.get("done"):
                        break
        except Exception as e:
            yield f"\n\n[inference error: {e}]"
        finally:
            text = "".join(assistant).strip()
            if text:
                with pool.connection() as conn:
                    conn.execute(
                        "INSERT INTO messages (conversation_id, role, actor_id, content, model) "
                        "VALUES (%s,'assistant','pat',%s,%s)", (cid, text, model))
                    conn.commit()
                # learn durable facts from this exchange, in the background
                threading.Thread(target=extract_memories, args=(user["id"], cid, content, text),
                                 daemon=True).start()

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------- images (spec §23)
_IMG_PROMPT_SYS = (
    "You are an expert text-to-image prompt engineer for Stable Diffusion XL. Rewrite the user's idea as a "
    "single vivid, detailed prompt: concrete subject, setting, lighting, composition, colour, style, and a "
    "few quality terms. Keep it under 70 words. Output ONLY the finished prompt — no preamble, no quotes, no "
    "explanation, no lists.")


def _enhance_prompt(description: str, model: str) -> str:
    """Image Prompt Creator: turn a plain idea into a detailed SDXL prompt using the local LLM."""
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/generate",
                       json={"model": model, "system": _IMG_PROMPT_SYS,
                             "prompt": f"Idea: {description}\n\nImage prompt:",
                             "stream": False, "think": False, "options": {"temperature": 0.7}},
                       timeout=90)
        r.raise_for_status()
        text = re.sub(r"<think>.*?</think>", "", r.json().get("response", ""), flags=re.S)
        text = text.strip().strip('"').strip()
        return text or description
    except Exception:
        return description


def _imagegen(path: str, payload: dict) -> tuple[bytes, float]:
    """Call the image service; return (png_bytes, gen_seconds). Raises HTTPException on failure."""
    if not IMAGEGEN_URL:
        raise HTTPException(status_code=503, detail="image generation is not configured")
    try:
        r = httpx.post(f"{IMAGEGEN_URL}{path}", json=payload, timeout=300)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        msg = e.response.text[:200] if e.response is not None else str(e)
        code = e.response.status_code if e.response is not None else 502
        raise HTTPException(status_code=code if code == 503 else 502, detail=f"image service: {msg}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"image service unreachable: {e}")
    return r.content, float(r.headers.get("X-Gen-Seconds", 0) or 0)


def _save_image(user_id, data: bytes, *, mode, prompt, source_prompt, negative, seed, steps,
                guidance, width, height, gen_seconds, conversation_id=None, source_image_id=None) -> dict:
    img_id = uuid.uuid4()
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGES_DIR / f"{img_id}.png"
    path.write_bytes(data)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO images (id,user_id,conversation_id,mode,prompt,source_prompt,negative_prompt,"
            "seed,steps,guidance,width,height,model,source_image_id,file_path,bytes,gen_seconds) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (img_id, user_id, conversation_id, mode, prompt, source_prompt, negative, seed, steps,
             guidance, width, height, IMG_MODEL_NAME, source_image_id, str(path), len(data), gen_seconds))
        conn.commit()
    return {"id": str(img_id), "url": f"/api/images/{img_id}/file", "mode": mode, "prompt": prompt,
            "source_prompt": source_prompt, "seed": seed, "width": width, "height": height,
            "source_image_id": str(source_image_id) if source_image_id else None,
            "gen_seconds": round(gen_seconds, 1) if gen_seconds else None}


@app.get("/api/images/config")
def images_config(user: dict = Depends(require_user)):
    return {"enabled": bool(IMAGEGEN_URL)}


@app.post("/api/images/prompt")
async def image_prompt(request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    desc = (body.get("description") or "").strip()
    if not desc:
        raise HTTPException(status_code=400, detail="empty description")
    return {"prompt": _enhance_prompt(desc, body.get("model") or DEFAULT_MODEL)}


@app.post("/api/images/generate")
async def image_generate(request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="empty prompt")
    source_prompt = None
    if body.get("enhance"):
        source_prompt = prompt
        prompt = _enhance_prompt(prompt, body.get("model") or DEFAULT_MODEL)
    steps = max(1, min(60, int(body.get("steps") or 28)))
    width = int(body.get("width") or 1024)
    height = int(body.get("height") or 1024)
    seed = body.get("seed")
    seed = int(seed) if seed not in (None, "") else None
    negative = (body.get("negative_prompt") or "").strip() or None
    payload = {"prompt": prompt, "steps": steps, "width": width, "height": height}
    if seed is not None:
        payload["seed"] = seed
    if negative:
        payload["negative_prompt"] = negative
    data, secs = _imagegen("/generate", payload)
    return _save_image(user["id"], data, mode="generate", prompt=prompt, source_prompt=source_prompt,
                       negative=negative, seed=seed, steps=steps, guidance=None, width=width,
                       height=height, gen_seconds=secs)


@app.post("/api/images/edit")
async def image_edit(request: Request, user: dict = Depends(require_user)):
    """img2img: transform an existing image (by id) or an uploaded one (base64) with a new prompt."""
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="empty prompt")
    mask = body.get("mask")                       # base64 PNG: white = change this area (inpaint)
    default_strength = 0.9 if mask else 0.6       # inpaint repaints the masked area more fully
    strength = max(0.05, min(1.0, float(body.get("strength") or default_strength)))
    steps = max(1, min(60, int(body.get("steps") or 30)))
    source_image_id = None
    b64 = body.get("image")
    sid = body.get("source_id")
    if sid:
        try:
            uuid.UUID(sid)
        except ValueError:
            raise HTTPException(status_code=404, detail="not found")
        with pool.connection() as conn:
            row = conn.execute("SELECT file_path FROM images WHERE id=%s AND user_id=%s "
                               "AND deleted_at IS NULL", (sid, user["id"])).fetchone()
        if not row or not Path(row["file_path"]).exists():
            raise HTTPException(status_code=404, detail="source image not found")
        b64 = base64.b64encode(Path(row["file_path"]).read_bytes()).decode()
        source_image_id = sid
    if not b64:
        raise HTTPException(status_code=400, detail="no source image")
    if mask:
        data, secs = _imagegen("/inpaint", {"prompt": prompt, "image": b64, "mask": mask,
                                            "strength": strength, "steps": steps})
        mode = "inpaint"
    else:
        data, secs = _imagegen("/img2img", {"prompt": prompt, "image": b64,
                                            "strength": strength, "steps": steps})
        mode = "img2img"
    return _save_image(user["id"], data, mode=mode, prompt=prompt, source_prompt=None,
                       negative=None, seed=None, steps=steps, guidance=None, width=None, height=None,
                       gen_seconds=secs, source_image_id=source_image_id)


@app.get("/api/images")
def list_images(user: dict = Depends(require_user)):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id,mode,prompt,source_prompt,seed,width,height,source_image_id,bytes,gen_seconds,"
            "created_at FROM images WHERE user_id=%s AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 200", (user["id"],)).fetchall()
    return {"images": [{"id": str(r["id"]), "url": f"/api/images/{r['id']}/file", "mode": r["mode"],
                        "prompt": r["prompt"], "source_prompt": r["source_prompt"], "seed": r["seed"],
                        "width": r["width"], "height": r["height"],
                        "source_image_id": str(r["source_image_id"]) if r["source_image_id"] else None,
                        "gen_seconds": float(r["gen_seconds"]) if r["gen_seconds"] else None,
                        "created_at": r["created_at"].isoformat()} for r in rows]}


@app.get("/api/images/{iid}/file")
def image_file(iid: str, user: dict = Depends(require_user)):
    try:
        uuid.UUID(iid)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    with pool.connection() as conn:
        row = conn.execute("SELECT file_path FROM images WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
                           (iid, user["id"])).fetchone()
    if not row or not Path(row["file_path"]).exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(row["file_path"], media_type="image/png")


@app.delete("/api/images/{iid}")
def delete_image(iid: str, user: dict = Depends(require_user)):
    try:
        uuid.UUID(iid)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    with pool.connection() as conn:
        conn.execute("UPDATE images SET deleted_at=now() WHERE id=%s AND user_id=%s", (iid, user["id"]))
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- safety / approvals (Sentinel proxy)
# The logged-in human (require_user) acts through Pat, which holds Sentinel's decider token. This is the
# "authenticated human" path for decisions and the e-stop; agents like Wade never get this token.
def _sentinel(method: str, path: str, payload=None, timeout: float = 15):
    if not SENTINEL_URL:
        raise HTTPException(status_code=503, detail="safety plane not configured")
    headers = {"Authorization": f"Bearer {SENTINEL_TOKEN}", "Content-Type": "application/json"}
    try:
        r = httpx.request(method, f"{SENTINEL_URL}{path}", headers=headers, json=payload, timeout=timeout)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"safety plane unreachable: {e}")
    if r.status_code >= 400:
        code = r.status_code if r.status_code in (401, 403, 404, 409, 423) else 502
        raise HTTPException(status_code=code, detail=f"safety plane: {r.text[:200]}")
    return r.json() if r.content else {}


@app.get("/api/safety/config")
def safety_config(user: dict = Depends(require_user)):
    return {"enabled": bool(SENTINEL_URL)}


@app.get("/api/safety/status")
def safety_status(user: dict = Depends(require_user)):
    return _sentinel("GET", "/status")


@app.get("/api/approvals")
def approvals_list(user: dict = Depends(require_user)):
    return _sentinel("GET", "/approvals?status=open")


@app.post("/api/approvals/{aid}/decide")
async def approvals_decide(aid: str, request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    dec = body.get("decision")
    if dec not in ("approved", "denied"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'denied'")
    res = _sentinel("POST", f"/approvals/{aid}/decide",
                    {"decision": dec, "by": user["username"], "note": body.get("note", "")})
    # If an approved request is a code deploy and the controller is configured, ship it now.
    token = res.get("token")
    if dec == "approved" and token and DEPLOY_URL:
        try:
            appr = _sentinel("GET", f"/approvals/{aid}")
            if appr.get("action") == "deploy.code":
                dr = httpx.post(f"{DEPLOY_URL}/deploy",
                                headers={"Authorization": f"Bearer {DEPLOY_TOKEN}"},
                                json={"approval_id": aid, "token": token}, timeout=180)
                res["deploy"] = dr.json() if dr.content else {"ok": dr.status_code == 200}
        except Exception as e:
            res["deploy"] = {"ok": False, "error": str(e)[:200]}
    res.pop("token", None)   # never surface the raw token to the browser
    return res


@app.post("/api/estop/engage")
async def estop_engage(request: Request, user: dict = Depends(require_user)):
    body = await request.json() if await _has_body(request) else {}
    return _sentinel("POST", "/estop/engage",
                     {"by": user["username"], "reason": body.get("reason", "engaged from Pat")})


@app.post("/api/estop/release")
def estop_release(user: dict = Depends(require_user)):
    return _sentinel("POST", "/estop/release", {"by": user["username"]})


# ---------------------------------------------------------------- voice (local STT + TTS)
# Speech-to-text with faster-whisper and text-to-speech with Piper, both on CPU so they never contend
# with the GPU. Models are loaded lazily and cached. If the libraries/models aren't installed, the voice
# config reports disabled and the UI hides the mic — Pat keeps working as text-only.
_whisper = None
_whisper_lock = threading.Lock()
_piper = None
_piper_lock = threading.Lock()


def _voice_ready() -> bool:
    try:
        import faster_whisper  # noqa: F401
        import piper           # noqa: F401
    except Exception:
        return False
    return (VOICE_DIR / f"{PIPER_VOICE}.onnx").exists()


def _get_whisper():
    global _whisper
    if _whisper is None:
        with _whisper_lock:
            if _whisper is None:
                from faster_whisper import WhisperModel
                _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper


def _get_piper():
    global _piper
    if _piper is None:
        with _piper_lock:
            if _piper is None:
                try:
                    from piper import PiperVoice
                except ImportError:
                    from piper.voice import PiperVoice
                _piper = PiperVoice.load(str(VOICE_DIR / f"{PIPER_VOICE}.onnx"))
    return _piper


def _plain_for_speech(text: str) -> str:
    """Strip markdown so the voice doesn't read out asterisks and backticks."""
    text = re.sub(r"```.*?```", " code block omitted ", text, flags=re.S)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


@app.get("/api/voice/config")
def voice_config(user: dict = Depends(require_user)):
    return {"enabled": _voice_ready(), "voice": PIPER_VOICE}


@app.post("/api/voice/transcribe")
async def voice_transcribe(request: Request, user: dict = Depends(require_user)):
    if not _voice_ready():
        raise HTTPException(status_code=503, detail="voice is not installed")
    import subprocess
    import tempfile
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="no audio")
    src = tempfile.NamedTemporaryFile(suffix=".webm", delete=False).name
    wav = src + ".wav"
    try:
        with open(src, "wb") as f:
            f.write(data)
        # normalise whatever the browser recorded to 16kHz mono wav for Whisper
        subprocess.run(["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", wav],
                       capture_output=True, timeout=60)
        # Read the wav into a float32 array ourselves and hand that to Whisper — this bypasses
        # faster-whisper's internal PyAV decode (whose av.open kwargs differ across versions).
        import wave as _wave
        import numpy as _np
        with _wave.open(wav, "rb") as w:
            pcm = w.readframes(w.getnframes())
        audio = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.float32) / 32768.0
        if audio.size == 0:
            return {"text": ""}
        segs, _info = _get_whisper().transcribe(audio, beam_size=1, language="en")
        text = "".join(s.text for s in segs).strip()
    finally:
        for p in (src, wav):
            try:
                os.unlink(p)
            except OSError:
                pass
    return {"text": text}


@app.post("/api/voice/speak")
async def voice_speak(request: Request, user: dict = Depends(require_user)):
    if not _voice_ready():
        raise HTTPException(status_code=503, detail="voice is not installed")
    import io
    import wave
    body = await request.json()
    text = _plain_for_speech(body.get("text") or "")[:2000]
    if not text:
        raise HTTPException(status_code=400, detail="no text")
    voice = _get_piper()
    buf = io.BytesIO()
    with _piper_lock:
        wf = wave.open(buf, "wb")
        first = True
        for ch in voice.synthesize(text):
            if first:
                wf.setnchannels(ch.sample_channels)
                wf.setsampwidth(ch.sample_width)
                wf.setframerate(ch.sample_rate)
                first = False
            wf.writeframes(ch.audio_int16_bytes)
        wf.close()
    if first:
        raise HTTPException(status_code=500, detail="no audio produced")
    return Response(content=buf.getvalue(), media_type="audio/wav")


# ---------------------------------------------------------------- web push (PWA notifications)
# Browser push for installed PWAs. On iOS this works only when Pat is added to the Home Screen
# (iOS 16.4+). We store one subscription per browser/device and send via VAPID/pywebpush.
def _push_enabled() -> bool:
    return bool(VAPID_PRIVATE and VAPID_PUBLIC)


def _admin_user_id():
    try:
        with pool.connection() as conn:
            r = conn.execute("SELECT id FROM users WHERE username=%s", (ADMIN_USER,)).fetchone()
        return r["id"] if r else None
    except Exception:
        return None


def send_push(user_id, title: str, body: str, url: str = "/", tag: str = "pat") -> int:
    """Send a notification to every registered device for a user. Prunes dead subscriptions."""
    if not _push_enabled():
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    with pool.connection() as conn:
        subs = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=%s",
            (user_id,)).fetchall()
    sent, ok_ids, dead = 0, [], []
    for s in subs:
        info = {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}
        try:
            webpush(subscription_info=info, data=payload,
                    vapid_private_key=VAPID_PRIVATE, vapid_claims={"sub": VAPID_SUB}, ttl=3600)
            sent += 1
            ok_ids.append(s["id"])
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):          # gone — the browser dropped this subscription
                dead.append(s["id"])
        except Exception:
            pass
    if ok_ids or dead:
        with pool.connection() as conn:
            if dead:
                conn.execute("DELETE FROM push_subscriptions WHERE id = ANY(%s)", (dead,))
            if ok_ids:
                conn.execute("UPDATE push_subscriptions SET last_ok=now() WHERE id = ANY(%s)", (ok_ids,))
            conn.commit()
    return sent


_seen_approvals: set = set()


def _approval_poller():
    """Poll Sentinel for open approvals; push a notification when a genuinely new one appears.
    On startup, pre-existing open approvals are marked seen (no notification storm after a restart)."""
    seeded = False
    while True:
        try:
            if SENTINEL_URL and _push_enabled():
                data = _sentinel("GET", "/approvals?status=open")
                items = (data or {}).get("approvals") or []
                if not seeded:
                    for a in items:
                        if a.get("id"):
                            _seen_approvals.add(a["id"])
                    seeded = True
                else:
                    uid = _admin_user_id()
                    for a in items:
                        aid = a.get("id")
                        if not aid or aid in _seen_approvals:
                            continue
                        _seen_approvals.add(aid)
                        who = a.get("requester_id") or "An agent"
                        action = a.get("action") or "needs your approval"
                        if uid:
                            send_push(uid, "Pat — approval needed",
                                      f"{who}: {action}", url="/?view=approvals", tag=f"appr-{aid}")
        except Exception:
            pass
        time.sleep(max(5, APPROVAL_PUSH_POLL))


@app.get("/api/push/key")
def push_key(user: dict = Depends(require_user)):
    return {"enabled": _push_enabled(), "publicKey": VAPID_PUBLIC}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, user: dict = Depends(require_user)):
    sub = await request.json()
    endpoint = (sub or {}).get("endpoint")
    keys = (sub or {}).get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (endpoint and p256dh and auth):
        raise HTTPException(status_code=400, detail="invalid subscription")
    ua = request.headers.get("user-agent", "")[:300]
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth, "
            "user_agent=EXCLUDED.user_agent, user_id=EXCLUDED.user_id",
            (user["id"], endpoint, p256dh, auth, ua))
        conn.commit()
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, user: dict = Depends(require_user)):
    endpoint = (await request.json() or {}).get("endpoint")
    if endpoint:
        with pool.connection() as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint=%s AND user_id=%s",
                         (endpoint, user["id"]))
            conn.commit()
    return {"ok": True}


@app.post("/api/push/test")
def push_test(user: dict = Depends(require_user)):
    n = send_push(user["id"], "Pat", "Notifications are working — this is a test from Pat.",
                  url="/", tag="pat-test")
    return {"ok": True, "sent": n}


# ---------------------------------------------------------------- static UI
@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


# ---- PWA (installable "add to Home Screen" app) assets -----------------------
@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        WEB_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    # no-cache so the browser always checks for an updated service worker
    return FileResponse(
        WEB_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/icons/{name}")
def icon(name: str):
    # serve only real files directly inside web/icons (no path traversal)
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=404, detail="not found")
    p = (WEB_DIR / "icons" / name).resolve()
    icons_dir = (WEB_DIR / "icons").resolve()
    if not str(p).startswith(str(icons_dir) + os.sep) or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def healthz():
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


# ---------------------------------------------------------------- helpers
def _own(conn: psycopg.Connection, cid: str, user: dict) -> None:
    try:
        uuid.UUID(cid)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    row = conn.execute("SELECT 1 FROM conversations WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
                       (cid, user["id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")


async def _has_body(request: Request) -> bool:
    return bool((await request.body()).strip())
