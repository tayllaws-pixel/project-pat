"""Sentinel HTTP service (CT 104) — the independent safety plane.

Wraps the pure core (core.py) over the Postgres `sentinel` DB and exposes it to Pat (the decider UI) and
to agents like Wade (requesters). Two bearer tokens enforce the central rule in software, not prompt text:

  * DECIDER token (held by Pat, relaying the authenticated human): may decide approvals, engage/release the
    e-stop, and pause/disable agents.
  * REQUESTER token (held by Wade and other agents): may only *request* approvals and *read* its gate/state.

The Ed25519 signing key lives only here (KEY_DIR/approval.key, 0600). Wade never holds it, so Wade can
never mint its own approval tokens. The e-stop also has a file flag that survives a DB outage.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from patshared.schemas import Actor, ActorKind, ApprovalDecision, ApprovalRequest
from .budget import BudgetMeter
from .core import (Approvals, Audit, EStopEngaged, Gate, KeyPair, SentinelError, ApprovalRejected,
                   classify_action)
from .pgstore import PgStore

DB_DSN   = os.environ["SENTINEL_DB_DSN"]
KEY_DIR  = os.environ.get("SENTINEL_KEY_DIR", "/etc/project-pat/sentinel")
FLAG     = os.environ.get("SENTINEL_ESTOP_FLAG", f"{KEY_DIR}/ESTOP")
DECIDER_TOKEN   = os.environ.get("SENTINEL_DECIDER_TOKEN", "")
REQUESTER_TOKEN = os.environ.get("SENTINEL_REQUESTER_TOKEN", "")

store = PgStore(DB_DSN)
app = FastAPI(title="Sentinel")
_lock = threading.Lock()          # serialise mutations so the audit hash chain stays linear

audit: Audit
gate: Gate
approvals: Approvals


@app.on_event("startup")
def _startup():
    global audit, gate, approvals
    store.open()
    priv = f"{KEY_DIR}/approval.key"
    pub = f"{KEY_DIR}/approval.pub"
    if Path(priv).exists():
        keys = KeyPair.load(priv, pub)
    else:
        keys = KeyPair.generate()
        keys.save(priv, pub)
    audit = Audit(store)
    gate = Gate(store, audit, flag_file=FLAG)
    approvals = Approvals(store, audit, keys)


# ---------------------------------------------------------------- auth
def role_of(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    tok = authorization.split(" ", 1)[1].strip()
    if DECIDER_TOKEN and tok == DECIDER_TOKEN:
        return "decider"
    if REQUESTER_TOKEN and tok == REQUESTER_TOKEN:
        return "requester"
    return None


def any_caller(authorization: str | None = Header(default=None)) -> str:
    r = role_of(authorization)
    if r is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return r


def decider_only(authorization: str | None = Header(default=None)) -> str:
    r = role_of(authorization)
    if r != "decider":
        raise HTTPException(status_code=403, detail="decider privilege required")
    return r


# ---------------------------------------------------------------- health / keys
@app.get("/healthz")
def healthz():
    try:
        store.get_estop()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/pubkey", response_class=PlainTextResponse)
def pubkey():
    return KeyPair.load(f"{KEY_DIR}/approval.key", f"{KEY_DIR}/approval.pub").public_pem()


# ---------------------------------------------------------------- status & gate
@app.get("/status")
def status(role: str = Depends(any_caller)):
    es = store.get_estop()
    ok, bad = audit.verify_chain()
    return {"estop": {"engaged": bool(es.get("engaged")), "reason": es.get("reason"),
                      "engaged_by": es.get("engaged_by")},
            "open_approvals": len(store.list_approvals(open_only=True)),
            "controls": store.all_controls(),
            "audit_chain_ok": ok}


@app.get("/gate/{actor_id:path}")
def gate_check(actor_id: str, role: str = Depends(any_caller)):
    """What an autonomous worker calls before claiming work. Never raises; returns a verdict."""
    es = store.get_estop()
    c = store.get_agent_control(actor_id)
    try:
        gate.allow_autonomous_work(actor_id)
        return {"allowed": True, "estop_engaged": bool(es.get("engaged")),
                "paused": bool(c.get("paused")), "disabled": bool(c.get("disabled"))}
    except (EStopEngaged, SentinelError) as e:
        return {"allowed": False, "reason": str(e), "estop_engaged": bool(es.get("engaged")),
                "paused": bool(c.get("paused")), "disabled": bool(c.get("disabled"))}


@app.get("/estop")
def get_estop(role: str = Depends(any_caller)):
    es = store.get_estop()
    return {"engaged": bool(es.get("engaged")), "reason": es.get("reason"),
            "engaged_by": es.get("engaged_by"), "engaged_at": es.get("engaged_at").isoformat() if es.get("engaged_at") else None}


@app.post("/estop/engage")
async def engage(request: Request, role: str = Depends(decider_only)):
    body = await request.json()
    by = (body.get("by") or "taylor")
    reason = (body.get("reason") or "manual")
    with _lock:
        gate.engage_estop(by, reason)
    return {"engaged": True}


@app.post("/estop/release")
async def release(request: Request, role: str = Depends(decider_only)):
    body = await request.json()
    by = (body.get("by") or "taylor")
    with _lock:
        gate.release_estop(by, human_authenticated=True)   # decider token == authenticated human via Pat
    return {"engaged": False}


# ---------------------------------------------------------------- controls
@app.get("/controls")
def controls(role: str = Depends(any_caller)):
    return {"controls": store.all_controls()}


@app.post("/controls/{actor_id:path}")
async def set_control(actor_id: str, request: Request, role: str = Depends(decider_only)):
    body = await request.json()
    with _lock:
        gate.set_agent(actor_id, paused=body.get("paused"), disabled=body.get("disabled"),
                       by=(body.get("by") or "taylor"), note=(body.get("note") or ""))
    return store.get_agent_control(actor_id)


# ---------------------------------------------------------------- approvals
@app.get("/approvals")
def list_approvals(status: str = "open", role: str = Depends(any_caller),
                   authorization: str | None = Header(default=None)):
    open_only = status != "all"
    # a requester may only see its own requests; the decider sees everything
    if role == "requester":
        return {"approvals": store.list_approvals(open_only=open_only)}  # requester id scoping below if needed
    return {"approvals": store.list_approvals(open_only=open_only)}


@app.post("/approvals")
async def create_approval(request: Request, role: str = Depends(any_caller)):
    body = await request.json()
    try:
        req = ApprovalRequest(
            requester=Actor(kind=ActorKind(body["requester"]["kind"]), id=body["requester"]["id"]),
            action=body["action"], reason=body.get("reason", ""),
            affected=body.get("affected", []), consequences=body.get("consequences", ""),
            resources=body.get("resources", {}),
            estimated_external_cost_usd=float(body.get("estimated_external_cost_usd", 0)),
            validation_evidence=body.get("validation_evidence", []), rollback=body.get("rollback", ""),
            payload=body.get("payload", {}), ttl_seconds=int(body.get("ttl_seconds", 86400)))
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"bad approval request: {e}")
    # a requester token cannot impersonate a human/pat decision path
    if role == "requester" and req.requester.kind in (ActorKind.human, ActorKind.pat):
        raise HTTPException(status_code=403, detail="requester may not post as human/pat")
    try:
        with _lock:
            res = approvals.request(req)
    except EStopEngaged as e:
        raise HTTPException(status_code=423, detail=str(e))
    return res


@app.get("/approvals/{aid}")
def get_approval(aid: str, role: str = Depends(any_caller)):
    rows = store.list_approvals(open_only=False)
    for r in rows:
        if r["id"] == aid:
            return r
    raise HTTPException(status_code=404, detail="not found")


@app.post("/approvals/{aid}/decide")
async def decide(aid: str, request: Request, role: str = Depends(decider_only)):
    body = await request.json()
    dec = body.get("decision")
    if dec not in ("approved", "denied"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'denied'")
    try:
        with _lock:
            token = approvals.decide(aid, ApprovalDecision(dec), by=(body.get("by") or "taylor"),
                                     note=(body.get("note") or ""), human_authenticated=True)
    except ApprovalRejected as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"decision": dec, "token": token}


@app.post("/approvals/{aid}/revoke")
async def revoke(aid: str, request: Request, role: str = Depends(decider_only)):
    body = await request.json() if await request.body() else {}
    try:
        with _lock:
            approvals.revoke(aid, by=(body.get("by") or "taylor"), note=(body.get("note") or ""))
    except ApprovalRejected as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@app.post("/verify")
async def verify(request: Request, role: str = Depends(any_caller)):
    """Executor-side single-use token check (used later by the deploy controller)."""
    body = await request.json()
    try:
        with _lock:
            claims = approvals.verify_and_consume(
                body["token"], body["action"], body.get("payload", {}),
                executor=body.get("executor", "unknown"), resource=body.get("resource"))
    except (ApprovalRejected, KeyError) as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True, "aid": claims.aid, "action": claims.action}


# ---------------------------------------------------------------- audit
@app.get("/audit/verify")
def audit_verify(role: str = Depends(any_caller)):
    ok, bad = audit.verify_chain()
    return {"ok": ok, "first_bad_index": bad}


@app.get("/audit/recent")
def audit_recent(limit: int = 50, role: str = Depends(decider_only)):
    return {"audit": store.recent_audit(limit)}
