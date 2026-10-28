"""Wade's persistent job queue over the `wade` database (db/migrations/0001_init.sql).

Durable by design (spec §12): failed jobs never disappear, every state transition is logged (job_log),
progress is checkpointed (job_checkpoints), and claims take a time-boxed lease so a dead worker's job is
reclaimable. Retries are bounded; after max_attempts a job goes to needs_reassessment, not an infinite loop.
"""
from __future__ import annotations

import uuid
from typing import Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class JobStore:
    def __init__(self, dsn: str, lease_seconds: int = 300):
        self.pool = ConnectionPool(dsn, min_size=1, max_size=6, kwargs={"row_factory": dict_row}, open=False)
        self.lease = lease_seconds

    def open(self):
        self.pool.open()

    # ---------------------------------------------------------------- enqueue
    def enqueue(self, kind: str, title: str, payload: dict, *, priority: str = "normal",
                depends_on: Optional[list] = None, created_by: str = "pat", max_attempts: int = 3) -> str:
        jid = uuid.uuid4()
        with self.pool.connection() as c:
            c.execute("INSERT INTO jobs (id,kind,title,priority,state,payload,depends_on,max_attempts,created_by) "
                      "VALUES (%s,%s,%s,%s,'queued',%s,%s,%s,%s)",
                      (jid, kind, title, priority, Jsonb(payload or {}), depends_on or [], max_attempts, created_by))
            c.execute("INSERT INTO job_log (job_id, from_state, to_state, note) VALUES (%s,NULL,'queued','enqueued')",
                      (jid,))
            c.commit()
        return str(jid)

    # ---------------------------------------------------------------- claim (atomic, leased)
    def claim(self, worker_id: str) -> Optional[dict]:
        with self.pool.connection() as c:
            row = c.execute(
                "UPDATE jobs SET state='running', claimed_by=%s, "
                "locked_until=now() + (%s || ' seconds')::interval, updated_at=now() "
                "WHERE id = ("
                "  SELECT j.id FROM jobs j WHERE j.state='queued' "
                "    AND (j.locked_until IS NULL OR j.locked_until < now()) "
                "    AND NOT EXISTS (SELECT 1 FROM unnest(j.depends_on) d "
                "        WHERE d NOT IN (SELECT id FROM jobs WHERE state='succeeded')) "
                "  ORDER BY j.priority, j.created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING *", (worker_id, str(self.lease))).fetchone()
            if row:
                c.execute("INSERT INTO job_log (job_id, from_state, to_state, note) "
                          "VALUES (%s,'queued','running',%s)", (row["id"], f"claimed by {worker_id}"))
            c.commit()
        return row

    def set_attempt(self, job_id, attempt: int):
        with self.pool.connection() as c:
            c.execute("UPDATE jobs SET attempt=%s, updated_at=now() WHERE id=%s", (attempt, job_id))
            c.commit()

    def checkpoint(self, job_id, attempt: int, stage: str, data: Optional[dict] = None):
        with self.pool.connection() as c:
            c.execute("INSERT INTO job_checkpoints (job_id, attempt, stage, data) VALUES (%s,%s,%s,%s)",
                      (job_id, attempt, stage, Jsonb(data or {})))
            c.commit()

    # ---------------------------------------------------------------- terminal transitions
    def succeed(self, job_id, result: dict, evidence: dict):
        with self.pool.connection() as c:
            c.execute("UPDATE jobs SET state='succeeded', result=%s, evidence=%s, finished_at=now(), "
                      "updated_at=now(), locked_until=NULL WHERE id=%s",
                      (Jsonb(result or {}), Jsonb(evidence or {}), job_id))
            c.execute("INSERT INTO job_log (job_id, from_state, to_state, note) VALUES (%s,'running','succeeded','')",
                      (job_id,))
            c.commit()

    def mark_result(self, job_id, state: str, result: dict, evidence: dict, note: str = ""):
        """Record a non-failure terminal-ish state that carries a result (e.g. awaiting_approval)."""
        finished = state in ("succeeded", "cancelled")
        with self.pool.connection() as c:
            c.execute("UPDATE jobs SET state=%s, result=%s, evidence=%s, updated_at=now(), locked_until=NULL, "
                      "finished_at=CASE WHEN %s THEN now() ELSE finished_at END WHERE id=%s",
                      (state, Jsonb(result or {}), Jsonb(evidence or {}), finished, job_id))
            c.execute("INSERT INTO job_log (job_id, from_state, to_state, note) VALUES (%s,'running',%s,%s)",
                      (job_id, state, (note or "")[:300]))
            c.commit()

    def to_state(self, job_id, state: str, note: str, *, failure_class: Optional[str] = None,
                 last_error: Optional[str] = None):
        """Move a running job to queued (retry/halt) or needs_reassessment/failed."""
        finished = state in ("needs_reassessment", "failed", "cancelled")
        with self.pool.connection() as c:
            c.execute("UPDATE jobs SET state=%s, last_error=COALESCE(%s,last_error), "
                      "failure_class=COALESCE(%s,failure_class), updated_at=now(), locked_until=NULL, "
                      "finished_at=CASE WHEN %s THEN now() ELSE finished_at END WHERE id=%s",
                      (state, (last_error or "")[:2000], failure_class, finished, job_id))
            c.execute("INSERT INTO job_log (job_id, from_state, to_state, note) VALUES (%s,'running',%s,%s)",
                      (job_id, state, note[:300]))
            c.commit()

    # ---------------------------------------------------------------- reads
    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self.pool.connection() as c:
            rows = c.execute("SELECT id,kind,title,priority,state,attempt,max_attempts,last_error,"
                             "created_at,finished_at FROM jobs ORDER BY created_at DESC LIMIT %s",
                             (limit,)).fetchall()
        return [{"id": str(r["id"]), "kind": r["kind"], "title": r["title"], "priority": r["priority"],
                 "state": r["state"], "attempt": r["attempt"], "max_attempts": r["max_attempts"],
                 "last_error": r["last_error"],
                 "created_at": r["created_at"].isoformat(),
                 "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None} for r in rows]

    def status(self) -> dict:
        with self.pool.connection() as c:
            counts = {r["state"]: r["n"] for r in
                      c.execute("SELECT state, count(*) n FROM jobs GROUP BY state").fetchall()}
            running = c.execute("SELECT id,kind,title,attempt FROM jobs WHERE state='running' "
                                "ORDER BY updated_at DESC").fetchall()
            recent = c.execute("SELECT id,kind,title,state,result,finished_at FROM jobs "
                               "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 10").fetchall()
        return {"counts": counts,
                "running": [{"id": str(r["id"]), "kind": r["kind"], "title": r["title"],
                             "attempt": r["attempt"]} for r in running],
                "recent": [{"id": str(r["id"]), "kind": r["kind"], "title": r["title"], "state": r["state"],
                            "result": r["result"],
                            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None}
                           for r in recent]}

    def reclaim_stale(self) -> int:
        """Return leased-but-expired running jobs to the queue (worker died mid-job)."""
        with self.pool.connection() as c:
            rows = c.execute("UPDATE jobs SET state='queued', locked_until=NULL, updated_at=now() "
                             "WHERE state='running' AND locked_until < now() RETURNING id").fetchall()
            for r in rows:
                c.execute("INSERT INTO job_log (job_id, from_state, to_state, note) "
                          "VALUES (%s,'running','queued','lease expired — reclaimed')", (r["id"],))
            c.commit()
        return len(rows)
