"""Wade's worker loop. Before claiming any job — and again before every attempt — it asks Sentinel
whether it may run (e-stop / pause / disable). It FAILS CLOSED: if Sentinel can't be reached, Wade does
not run. A job interrupted by the e-stop goes back to the queue (it is not restarted until the gate clears).
"""
from __future__ import annotations

import threading
import time

import httpx

from .handlers import HANDLERS


class Worker:
    def __init__(self, store, *, actor_id: str, sentinel_url: str, requester_token: str,
                 default_targets: list, poll: float = 5.0):
        self.store = store
        self.actor_id = actor_id
        self.sentinel_url = sentinel_url.rstrip("/")
        self.token = requester_token
        self.default_targets = default_targets
        self.poll = poll
        self._stop = threading.Event()
        self.last_gate = {"allowed": None, "reason": "starting"}

    def stop(self):
        self._stop.set()

    def gate_allowed(self) -> tuple[bool, dict]:
        if not self.sentinel_url:
            return False, {"reason": "no sentinel configured (fail closed)"}
        try:
            r = httpx.get(f"{self.sentinel_url}/gate/{self.actor_id}",
                          headers={"Authorization": f"Bearer {self.token}"}, timeout=8)
            if r.status_code == 200:
                info = r.json()
                return bool(info.get("allowed")), info
            return False, {"reason": f"gate http {r.status_code}"}
        except Exception as e:
            return False, {"reason": f"sentinel unreachable (fail closed): {e}"}

    def run_forever(self):
        while not self._stop.is_set():
            try:
                self.store.reclaim_stale()
            except Exception:
                pass
            allowed, info = self.gate_allowed()
            self.last_gate = info if isinstance(info, dict) else {"allowed": allowed}
            self.last_gate["allowed"] = allowed
            if not allowed:
                self._wait()
                continue
            try:
                job = self.store.claim(self.actor_id)
            except Exception:
                self._wait()
                continue
            if not job:
                self._wait()
                continue
            self.execute(job)

    def _wait(self):
        self._stop.wait(self.poll)

    def execute(self, job: dict):
        jid = job["id"]
        kind = job["kind"]
        handler = HANDLERS.get(kind)
        maxa = int(job.get("max_attempts") or 3)
        if handler is None:
            self.store.to_state(jid, "needs_reassessment", f"no handler for kind '{kind}'",
                                failure_class="needs_capability", last_error=f"unknown kind {kind}")
            return
        for attempt in range(1, maxa + 1):
            # re-check the gate before each attempt: the e-stop may have engaged mid-job
            allowed, _ = self.gate_allowed()
            if not allowed:
                self.store.to_state(jid, "queued", "halted by safety gate — requeued",
                                    failure_class="halted")
                return
            self.store.set_attempt(jid, attempt)
            self.store.checkpoint(jid, attempt, "attempt_start")
            try:
                result, evidence = handler(job, {"default_targets": self.default_targets})
                self.store.checkpoint(jid, attempt, "completed", {"summary": str(result)[:400]})
                self.store.succeed(jid, result, evidence)
                return
            except Exception as e:
                self.store.checkpoint(jid, attempt, "error", {"error": str(e)[:400]})
                if attempt >= maxa:
                    self.store.to_state(jid, "needs_reassessment",
                                        f"failed after {maxa} attempts", failure_class="unknown",
                                        last_error=str(e))
                    return
                self._stop.wait(1.0)
