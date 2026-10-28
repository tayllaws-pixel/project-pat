"""Wade control API + worker host (CT 103).

A small FastAPI surface for enqueuing jobs and reading status (used by Pat's "what has Wade done?"
view later), with the worker loop running in a background thread. Wade holds only the Sentinel REQUESTER
token and its own DB role — no Proxmox token, no Sentinel signing key, no deploy rights (ADR-0003).
"""
from __future__ import annotations

import json
import os
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import JobStore
from .worker import Worker

DB_DSN          = os.environ["WADE_DB_DSN"]
SENTINEL_URL    = os.environ.get("SENTINEL_URL", "").rstrip("/")
REQUESTER_TOKEN = os.environ.get("SENTINEL_REQUESTER_TOKEN", "")
CONTROL_TOKEN   = os.environ.get("WADE_CONTROL_TOKEN", "")
ACTOR_ID        = os.environ.get("WADE_ACTOR_ID", "wade")
INFERENCE_URL   = os.environ.get("INFERENCE_URL", "").rstrip("/")
CODE_MODEL      = os.environ.get("WADE_CODE_MODEL", "qwen3:8b")
FORGE_URL       = os.environ.get("FORGE_URL", "").rstrip("/")
FORGE_TOKEN     = os.environ.get("FORGE_TOKEN", "")
REPO_URL        = os.environ.get("WADE_REPO_URL", "")
WORK_ROOT       = os.environ.get("WADE_WORK_ROOT", "/var/wade/work")
try:
    DEFAULT_TARGETS = json.loads(os.environ.get("WADE_DEFAULT_TARGETS", "[]"))
except Exception:
    DEFAULT_TARGETS = []

store = JobStore(DB_DSN)
app = FastAPI(title="Wade")
worker: Worker


@app.on_event("startup")
def _startup():
    global worker
    store.open()
    os.makedirs(WORK_ROOT, exist_ok=True)
    handler_ctx = {"inference_url": INFERENCE_URL, "code_model": CODE_MODEL,
                   "forge_url": FORGE_URL, "forge_token": FORGE_TOKEN,
                   "repo_url": REPO_URL, "work_root": WORK_ROOT}
    worker = Worker(store, actor_id=ACTOR_ID, sentinel_url=SENTINEL_URL,
                    requester_token=REQUESTER_TOKEN, default_targets=DEFAULT_TARGETS,
                    handler_ctx=handler_ctx)
    threading.Thread(target=worker.run_forever, daemon=True).start()


def control(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    tok = authorization.split(" ", 1)[1].strip()
    if not CONTROL_TOKEN or tok != CONTROL_TOKEN:
        raise HTTPException(status_code=403, detail="control privilege required")
    return "control"


@app.get("/healthz")
def healthz():
    try:
        store.status()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/jobs")
async def enqueue(request: Request, _: str = Depends(control)):
    body = await request.json()
    kind = body.get("kind")
    title = body.get("title") or kind
    if not kind:
        raise HTTPException(status_code=400, detail="kind required")
    jid = store.enqueue(kind, title, body.get("payload", {}),
                        priority=body.get("priority", "normal"),
                        depends_on=body.get("depends_on", []),
                        created_by=body.get("created_by", "pat"),
                        max_attempts=int(body.get("max_attempts", 3)))
    return {"id": jid, "state": "queued"}


@app.get("/jobs")
def jobs(limit: int = 50, _: str = Depends(control)):
    return {"jobs": store.list_jobs(limit)}


@app.get("/status")
def status(_: str = Depends(control)):
    s = store.status()
    s["gate"] = getattr(worker, "last_gate", {})
    s["actor"] = ACTOR_ID
    return s
