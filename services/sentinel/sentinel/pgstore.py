"""Postgres-backed `Store` for Sentinel — maps the pure core (core.py) 1:1 onto the `sentinel` database
(db/migrations/0001_init.sql). The core holds the logic; this holds the rows.

Audit rows are read back with their timestamp normalised to UTC isoformat(microseconds) so the hash chain
verifies byte-for-byte against what core.Audit.record() hashed at write time, regardless of session TZ.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from patshared.schemas import Actor, ActorKind, ApprovalDecision, ApprovalRequest

GENESIS = "0" * 64


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


class PgStore:
    def __init__(self, dsn: str):
        self.pool = ConnectionPool(dsn, min_size=1, max_size=8,
                                   kwargs={"row_factory": dict_row}, open=False)

    def open(self):
        self.pool.open()

    # ---------------------------------------------------------------- e-stop
    def get_estop(self) -> dict:
        with self.pool.connection() as c:
            r = c.execute("SELECT engaged, engaged_by, engaged_at, reason, released_by, released_at "
                          "FROM estop WHERE singleton").fetchone()
        return r or {"engaged": False}

    def set_estop(self, engaged: bool, actor: str, reason: str) -> None:
        with self.pool.connection() as c:
            if engaged:
                c.execute("UPDATE estop SET engaged=true, engaged_by=%s, engaged_at=now(), reason=%s "
                          "WHERE singleton", (actor, reason))
            else:
                c.execute("UPDATE estop SET engaged=false, released_by=%s, released_at=now() "
                          "WHERE singleton", (actor,))
            c.commit()

    # ---------------------------------------------------------------- agent controls
    def get_agent_control(self, actor_id: str) -> dict:
        with self.pool.connection() as c:
            r = c.execute("SELECT paused, disabled, set_by, set_at, note FROM agent_controls WHERE actor_id=%s",
                          (actor_id,)).fetchone()
        return r or {"paused": False, "disabled": False}

    def set_agent_control(self, actor_id, paused, disabled, by, note) -> None:
        with self.pool.connection() as c:
            cur = c.execute("SELECT paused, disabled FROM agent_controls WHERE actor_id=%s", (actor_id,)).fetchone()
            p = cur["paused"] if cur else False
            d = cur["disabled"] if cur else False
            if paused is not None:
                p = paused
            if disabled is not None:
                d = disabled
            c.execute("INSERT INTO agent_controls (actor_id, paused, disabled, set_by, note) "
                      "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (actor_id) DO UPDATE SET "
                      "paused=EXCLUDED.paused, disabled=EXCLUDED.disabled, set_by=EXCLUDED.set_by, "
                      "set_at=now(), note=EXCLUDED.note", (actor_id, p, d, by, note))
            c.commit()

    def all_controls(self) -> list[dict]:
        with self.pool.connection() as c:
            return c.execute("SELECT actor_id, paused, disabled, set_by, set_at, note FROM agent_controls "
                             "WHERE paused OR disabled ORDER BY actor_id").fetchall()

    # ---------------------------------------------------------------- approvals
    def put_approval(self, req: ApprovalRequest, expires_at: float) -> None:
        with self.pool.connection() as c:
            c.execute(
                "INSERT INTO approvals (id, requester_kind, requester_id, action, reason, affected, "
                "consequences, resources, estimated_external_cost_usd, validation_evidence, rollback, "
                "payload, payload_sha256, requested_at, expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (req.id, req.requester.kind.value, req.requester.id, req.action, req.reason,
                 list(req.affected), req.consequences, Jsonb(req.resources),
                 req.estimated_external_cost_usd, list(req.validation_evidence), req.rollback,
                 Jsonb(req.payload), req.payload_sha256, req.requested_at, _dt(expires_at)))
            c.commit()

    def _row_to_req(self, r: dict) -> ApprovalRequest:
        ttl = max(1, int(_epoch(r["expires_at"]) - _epoch(r["requested_at"])))
        return ApprovalRequest(
            id=str(r["id"]), requester=Actor(kind=ActorKind(r["requester_kind"]), id=r["requester_id"]),
            action=r["action"], reason=r["reason"], affected=list(r["affected"] or []),
            consequences=r["consequences"] or "", resources=r["resources"] or {},
            estimated_external_cost_usd=float(r["estimated_external_cost_usd"] or 0),
            validation_evidence=list(r["validation_evidence"] or []), rollback=r["rollback"] or "",
            payload=r["payload"] or {}, ttl_seconds=ttl, requested_at=r["requested_at"])

    def get_approval(self, aid: str) -> Optional[dict]:
        with self.pool.connection() as c:
            r = c.execute("SELECT * FROM approvals WHERE id=%s", (aid,)).fetchone()
        if not r:
            return None
        return {"req": self._row_to_req(r), "expires_at": _epoch(r["expires_at"]),
                "decision": ApprovalDecision(r["decision"]) if r["decision"] else None,
                "decided_by": r["decided_by"]}

    def decide_approval(self, aid: str, decision: ApprovalDecision, by: str, note: str) -> None:
        with self.pool.connection() as c:
            c.execute("UPDATE approvals SET decision=%s, decided_by=%s, decided_at=now(), decision_note=%s "
                      "WHERE id=%s", (decision.value, by, note, aid))
            c.commit()

    def list_approvals(self, *, open_only: bool, requester_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        q = ("SELECT id, requester_kind, requester_id, action, reason, affected, consequences, "
             "estimated_external_cost_usd, payload, requested_at, expires_at, decision, decided_by, "
             "decided_at, decision_note FROM approvals")
        conds, args = [], []
        if open_only:
            conds.append("decision IS NULL")
        if requester_id:
            conds.append("requester_id=%s")
            args.append(requester_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY requested_at DESC LIMIT %s"
        args.append(limit)
        with self.pool.connection() as c:
            rows = c.execute(q, tuple(args)).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": str(r["id"]), "requester": r["requester_id"], "requester_kind": r["requester_kind"],
                "action": r["action"], "reason": r["reason"], "affected": list(r["affected"] or []),
                "consequences": r["consequences"] or "",
                "estimated_external_cost_usd": float(r["estimated_external_cost_usd"] or 0),
                "payload": r["payload"] or {},
                "requested_at": r["requested_at"].isoformat(),
                "expires_at": r["expires_at"].isoformat(),
                "decision": r["decision"], "decided_by": r["decided_by"],
                "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None,
                "decision_note": r["decision_note"]})
        return out

    # ---------------------------------------------------------------- tokens
    def put_token(self, nonce: str, aid: str, exp: float) -> None:
        with self.pool.connection() as c:
            c.execute("INSERT INTO approval_tokens (nonce, approval_id, exp) VALUES (%s,%s,%s)",
                      (nonce, aid, _dt(exp)))
            c.commit()

    def consume_token(self, nonce: str, by: str) -> bool:
        with self.pool.connection() as c:
            r = c.execute("UPDATE approval_tokens SET used_at=now(), used_by=%s "
                          "WHERE nonce=%s AND used_at IS NULL RETURNING nonce", (by, nonce)).fetchone()
            c.commit()
        return r is not None

    # ---------------------------------------------------------------- audit (hash chain)
    def append_audit(self, row: dict) -> None:
        with self.pool.connection() as c:
            c.execute("INSERT INTO audit (ts, actor, action, details, prev_hash, hash) "
                      "VALUES (%s,%s,%s,%s,%s,%s)",
                      (row["ts"], row["actor"], row["action"], Jsonb(row["details"]),
                       row["prev_hash"], row["hash"]))
            c.commit()

    def audit_rows(self) -> list[dict]:
        with self.pool.connection() as c:
            rows = c.execute("SELECT ts, actor, action, details, prev_hash, hash FROM audit ORDER BY id").fetchall()
        out = []
        for r in rows:
            out.append({"ts": r["ts"].astimezone(timezone.utc).isoformat(timespec="microseconds"),
                        "actor": r["actor"], "action": r["action"], "details": r["details"] or {},
                        "prev_hash": r["prev_hash"], "hash": r["hash"]})
        return out

    def last_audit_hash(self) -> str:
        with self.pool.connection() as c:
            r = c.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        return r["hash"] if r else GENESIS

    def recent_audit(self, limit: int = 50) -> list[dict]:
        with self.pool.connection() as c:
            rows = c.execute("SELECT ts, actor, action, details FROM audit ORDER BY id DESC LIMIT %s",
                             (limit,)).fetchall()
        return [{"ts": r["ts"].astimezone(timezone.utc).isoformat(), "actor": r["actor"],
                 "action": r["action"], "details": r["details"] or {}} for r in rows]
