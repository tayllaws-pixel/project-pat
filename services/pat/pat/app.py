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
import re
import threading
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
EMBED_MODEL   = os.environ.get("PAT_EMBED_MODEL", "nomic-embed-text")
EXTRACT_MODEL = os.environ.get("PAT_EXTRACT_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
EMBED_DIM     = 1024          # storage width of memory_embeddings.embedding; nomic is 768 (zero-padded)
MEM_TOPK      = int(os.environ.get("PAT_MEMORY_TOPK", "6"))
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
