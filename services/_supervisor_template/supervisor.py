"""Generic Project PAT supervisor runtime — the FIXED, trusted scaffold.

Every supervisor Wade builds runs this exact file; only handlers.py (the agents) differs. The safety
invariants here do NOT depend on the generated handlers:

  * The worker asks Sentinel whether it may run before claiming any job AND before every attempt, and
    FAILS CLOSED: if Sentinel can't be reached, nothing runs.
  * This process holds ONLY a Sentinel *requester* token (it may REQUEST approvals, never decide, verify,
    or deploy them), its own database role, and read-only service URLs. No Sentinel signing key, no Proxmox
    token, no deploy rights, no other service's credentials.
  * Handlers run in-process here; anything they want to change in the world must go through a
    human-approved Sentinel approval — exactly like Wade.

The e-stop and per-actor pause/disable in Sentinel apply to this supervisor like any other agent.
"""
from __future__ import annotations

import os
import threading
import uuid

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from handlers import HANDLERS   # the generated agents: {kind: fn(job, ctx) -> (result, evidence[, state])}

DB_DSN        = os.environ["SUP_DB_DSN"]
SENTINEL_URL  = os.environ.get("SENTINEL_URL", "").rstrip("/")
REQ_TOKEN     = os.environ.get("SENTINEL_REQUESTER_TOKEN", "")
ACTOR_ID      = os.environ.get("SUP_ACTOR_ID", "supervisor")
CONTROL_TOKEN = os.environ.get("SUP_CONTROL_TOKEN", "")
POLL          = float(os.environ.get("SUP_POLL", "5"))
LEASE_SECONDS = int(os.environ.get("SUP_LEASE", "300"))
HANDLER_CTX = {
    "sentinel_url": SENTINEL_URL, "requester_token": REQ_TOKEN, "actor_id": ACTOR_ID,
    "inference_url": os.environ.get("INFERENCE_URL", "").rstrip("/"),
    "code_model": os.environ.get("SUP_CODE_MODEL", "qwen3:8b"),
}

pool = ConnectionPool(DB_DSN, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=False)
app = FastAPI(title=f"supervisor:{ACTOR_ID}")
_stop = threading.Event()
last_gate = {"allowed": None, "reason": "starting"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        text NOT NULL,
  title       text NOT NULL,
  state       text NOT NULL DEFAULT 'queued',
  payload     jsonb NOT NULL DEFAULT '{}',
  attempt     int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 3,
  result      jsonb,
  evidence    jsonb,
  last_error  text,
  locked_until timestamptz,
  created_by  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_queued ON jobs(state, created_at);
"""


def _ensure_schema():
    with pool.connection() as c:
        c.execute(_SCHEMA)
        c.commit()


def gate_allowed():
    """Fail closed: only a 200 with allowed=true lets the worker run."""
    if not SENTINEL_URL:
        return False, {"reason": "no sentinel configured (fail closed)"}
    try:
        r = httpx.get(f"{SENTINEL_URL}/gate/{ACTOR_ID}",
                      headers={"Authorization": f"Bearer {REQ_TOKEN}"}, timeout=8)
        if r.status_code == 200:
            info = r.json()
            return bool(info.get("allowed")), info
        return False, {"reason": f"gate http {r.status_code}"}
    except Exception as e:
        return False, {"reason": f"sentinel unreachable (fail closed): {e}"}


def _claim():
    with pool.connection() as c:
        row = c.execute(
            "UPDATE jobs SET state='running', locked_until=now() + (%s || ' seconds')::interval, "
            "updated_at=now() WHERE id = (SELECT id FROM jobs WHERE state='queued' "
            "AND (locked_until IS NULL OR locked_until < now()) ORDER BY created_at "
            "FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *", (str(LEASE_SECONDS),)).fetchone()
        c.commit()
    return row


def _finish(jid, state, result=None, evidence=None, last_error=None):
    finished = state in ("succeeded", "needs_reassessment", "failed", "cancelled")
    with pool.connection() as c:
        c.execute("UPDATE jobs SET state=%s, result=%s, evidence=%s, last_error=COALESCE(%s,last_error), "
                  "updated_at=now(), locked_until=NULL, "
                  "finished_at=CASE WHEN %s THEN now() ELSE finished_at END WHERE id=%s",
                  (state, Jsonb(result or {}), Jsonb(evidence or {}), last_error, finished, jid))
        c.commit()


def _reclaim():
    with pool.connection() as c:
        c.execute("UPDATE jobs SET state='queued', locked_until=NULL, updated_at=now() "
                  "WHERE state='running' AND locked_until < now()")
        c.commit()


def _run_job(job):
    jid, kind = job["id"], job["kind"]
    maxa = int(job.get("max_attempts") or 3)
    handler = HANDLERS.get(kind)
    if handler is None:
        _finish(jid, "needs_reassessment", last_error=f"no handler for kind {kind!r}")
        return
    for attempt in range(1, maxa + 1):
        allowed, _ = gate_allowed()
        if not allowed:
            _finish(jid, "queued")      # halted by the gate — requeue, do not finish
            return
        with pool.connection() as c:
            c.execute("UPDATE jobs SET attempt=%s, updated_at=now() WHERE id=%s", (attempt, jid))
            c.commit()
        try:
            out = handler(job, dict(HANDLER_CTX))
            if isinstance(out, tuple) and len(out) == 3:
                result, evidence, state = out
            elif isinstance(out, tuple) and len(out) == 2:
                result, evidence, state = out[0], out[1], "succeeded"
            else:
                result, evidence, state = out, {}, "succeeded"
            _finish(jid, state, result, evidence)
            return
        except Exception as e:
            if attempt >= maxa:
                _finish(jid, "needs_reassessment", last_error=str(e)[:2000])
                return
            _stop.wait(1.0)


def run_forever():
    while not _stop.is_set():
        try:
            _reclaim()
        except Exception:
            pass
        allowed, info = gate_allowed()
        g = dict(info or {})
        g["allowed"] = allowed
        globals()["last_gate"] = g
        if not allowed:
            _stop.wait(POLL)
            continue
        try:
            job = _claim()
        except Exception:
            _stop.wait(POLL)
            continue
        if not job:
            _stop.wait(POLL)
            continue
        _run_job(job)


@app.on_event("startup")
def _startup():
    pool.open()
    _ensure_schema()
    threading.Thread(target=run_forever, daemon=True).start()


def control(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not CONTROL_TOKEN or authorization.split(" ", 1)[1].strip() != CONTROL_TOKEN:
        raise HTTPException(status_code=403, detail="control privilege required")
    return "control"


@app.get("/healthz")
def healthz():
    try:
        with pool.connection() as c:
            c.execute("SELECT 1")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/jobs")
async def enqueue(request: Request, _: str = Depends(control)):
    b = await request.json()
    kind = b.get("kind")
    if not kind:
        raise HTTPException(status_code=400, detail="kind required")
    jid = uuid.uuid4()
    with pool.connection() as c:
        c.execute("INSERT INTO jobs (id,kind,title,payload,max_attempts,created_by) VALUES (%s,%s,%s,%s,%s,%s)",
                  (jid, kind, b.get("title") or kind, Jsonb(b.get("payload") or {}),
                   int(b.get("max_attempts", 3)), b.get("created_by", "pat")))
        c.commit()
    return {"id": str(jid), "state": "queued"}


@app.get("/jobs")
def jobs(limit: int = 50, _: str = Depends(control)):
    with pool.connection() as c:
        rows = c.execute("SELECT id,kind,title,state,attempt,last_error,created_at,finished_at "
                         "FROM jobs ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    return {"jobs": [{"id": str(r["id"]), "kind": r["kind"], "title": r["title"], "state": r["state"],
                      "attempt": r["attempt"], "last_error": r["last_error"],
                      "created_at": r["created_at"].isoformat(),
                      "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None}
                     for r in rows]}


@app.get("/status")
def status(_: str = Depends(control)):
    with pool.connection() as c:
        counts = {r["state"]: r["n"] for r in
                  c.execute("SELECT state, count(*) n FROM jobs GROUP BY state").fetchall()}
    return {"actor": ACTOR_ID, "gate": last_gate, "counts": counts}
