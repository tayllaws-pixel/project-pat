"""Pat v0 — the assistant backend (PAT-040/041/042).

A small FastAPI service: password login (signed session cookie), conversations and messages persisted to
the `pat` database, and chat streamed from the local Ollama inference service. Clean, synchronous, and
dependency-light on purpose; multi-user auth (the sessions table), memory extraction, projects, and the
approval inbox come later. Config is entirely from environment variables.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

import httpx
import psycopg
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
WEB_DIR      = Path(__file__).resolve().parent.parent / "web"

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
        row = conn.execute("SELECT id, username, display_name, role FROM users WHERE username=%s",
                           (username,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="unknown user")
    return row


# ---------------------------------------------------------------- lifecycle
@app.on_event("startup")
def _startup() -> None:
    pool.open()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO users (username, display_name, role) VALUES (%s, %s, 'admin') "
            "ON CONFLICT (username) DO NOTHING",
            (ADMIN_USER, ADMIN_USER.capitalize()))
        conn.commit()


# ---------------------------------------------------------------- auth routes
@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if username != ADMIN_USER or not verify_password(password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    resp = JSONResponse({"ok": True, "user": username})
    resp.set_cookie("pat_session", make_cookie(username), httponly=True, samesite="lax",
                    max_age=SESSION_TTL, secure=False)   # secure=False: served over tailnet/LAN http for v0
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("pat_session")
    return resp


@app.get("/api/me")
def me(user: dict = Depends(require_user)):
    return {"username": user["username"], "display_name": user["display_name"], "role": user["role"]}


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

    ollama_messages = ([{"role": "system", "content": SYSTEM_PROMPT}] +
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

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------- static UI
@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


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
