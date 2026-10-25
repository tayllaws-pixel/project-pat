"""Step engine for `patctl` (PAT-012).

A Step has an id, phase, a `check()` that is read-only and returns True when the step is already satisfied, an
`apply()` that converges the host, and an optional `rollback()`. The engine persists a state file so an interrupted
install resumes at the first unsatisfied step, and it never re-runs a destructive step without the explicit
`--i-understand <step-id>` token for that step (spec §31: "do not blindly repeat destructive operations").
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


class StepError(RuntimeError):
    pass


class RebootRequired(Exception):
    """Raised by apply() when the host must reboot before later steps can run."""


@dataclass
class Step:
    id: str
    phase: int
    title: str
    check: Callable[[], bool]
    apply: Callable[[], None]
    rollback: Optional[Callable[[], None]] = None
    destructive: bool = False
    depends: list[str] = field(default_factory=list)
    reboot_after: bool = False


@dataclass
class StepRecord:
    status: str = "pending"          # pending | satisfied | applied | failed | skipped | rolled_back
    last_run: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0


class State:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: dict = {"steps": {}, "reboot_required": False, "history": []}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except ValueError:
                # Corrupt state is preserved for forensics, never silently discarded.
                self.path.rename(self.path.with_suffix(".corrupt"))

    def rec(self, step_id: str) -> StepRecord:
        d = self.data["steps"].setdefault(step_id, {})
        return StepRecord(**{k: d.get(k, v) for k, v in StepRecord().__dict__.items()})

    def put(self, step_id: str, rec: StepRecord):
        self.data["steps"][step_id] = rec.__dict__
        self.save()

    def log(self, event: str, **kw):
        self.data["history"].append({"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "event": event, **kw})
        self.data["history"] = self.data["history"][-500:]
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)


@dataclass
class PlanRow:
    step: Step
    satisfied: Optional[bool]
    record: StepRecord
    blocked_by: list[str]


class Engine:
    def __init__(self, steps: list[Step], state: State, out: Callable[[str], None] = print):
        self.steps = steps
        self.by_id = {s.id: s for s in steps}
        self.state = state
        self.out = out
        for s in steps:
            for d in s.depends:
                if d not in self.by_id:
                    raise StepError(f"step {s.id} depends on unknown step {d}")

    # ---- planning ----
    def plan(self, phase: Optional[int] = None, only: Optional[list[str]] = None) -> list[PlanRow]:
        rows = []
        for s in self.steps:
            if phase is not None and s.phase > phase:
                continue
            if only and s.id not in only:
                continue
            try:
                sat = bool(s.check())
            except Exception as e:  # check must never crash a plan
                sat = None
                self.out(f"  [{s.id}] check error: {e}")
            rec = self.state.rec(s.id)
            if sat and rec.status in ("pending", "failed"):
                rec.status = "satisfied"
            blocked = [d for d in s.depends if not self._dep_ok(d)]
            rows.append(PlanRow(s, sat, rec, blocked))
        return rows

    def _dep_ok(self, dep_id: str) -> bool:
        try:
            return bool(self.by_id[dep_id].check())
        except Exception:
            return False

    # ---- execution ----
    def install(self, phase: Optional[int] = None, dry_run: bool = False, understood: tuple[str, ...] = (),
                only: Optional[list[str]] = None, stop_on_reboot: bool = True) -> dict:
        if self.state.data.get("reboot_required") and not dry_run:
            if os.path.exists("/var/lib/project-pat/reboot-required") or self.state.data.get("reboot_pending_check", True):
                self.out("A reboot is pending from a previous step. Reboot, then run install again (it resumes).")
                return {"status": "reboot_required"}
        summary = {"applied": [], "satisfied": [], "skipped": [], "failed": None}
        would: set[str] = set()
        for row in self.plan(phase, only):
            s, rec = row.step, row.record
            # dependencies are re-evaluated now, because earlier steps in this run may have just satisfied them
            row.blocked_by = [d for d in s.depends if not (self._dep_ok(d) or (dry_run and d in would))]
            if row.satisfied:
                rec.status = "satisfied"
                self.state.put(s.id, rec)
                summary["satisfied"].append(s.id)
                continue
            if row.blocked_by:
                self.out(f"[{s.id}] blocked by unsatisfied: {', '.join(row.blocked_by)}")
                summary["skipped"].append(s.id)
                continue
            if s.destructive and s.id not in understood:
                self.out(f"[{s.id}] DESTRUCTIVE — refusing without --i-understand {s.id}  ({s.title})")
                summary["skipped"].append(s.id)
                continue
            if s.destructive and rec.attempts > 0 and rec.status == "failed":
                self.out(f"[{s.id}] destructive step previously failed; use `repair --step {s.id}` after reviewing the host.")
                summary["skipped"].append(s.id)
                continue
            if dry_run:
                self.out(f"[{s.id}] would apply: {s.title}{' (destructive)' if s.destructive else ''}")
                summary["applied"].append(s.id)
                would.add(s.id)
                continue
            self.out(f"[{s.id}] applying: {s.title}")
            rec.attempts += 1
            rec.last_run = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            try:
                s.apply()
                if not s.check():
                    raise StepError("apply finished but check() still reports unsatisfied")
                rec.status, rec.error = "applied", None
                self.state.put(s.id, rec)
                self.state.log("applied", step=s.id)
                summary["applied"].append(s.id)
                if s.reboot_after:
                    self.state.data["reboot_required"] = True
                    self.state.save()
                    self.out(f"[{s.id}] reboot required. Reboot, then run install again.")
                    if stop_on_reboot:
                        summary["status"] = "reboot_required"
                        return summary
            except RebootRequired:
                rec.status = "applied"
                self.state.put(s.id, rec)
                self.state.data["reboot_required"] = True
                self.state.save()
                summary["applied"].append(s.id)
                summary["status"] = "reboot_required"
                return summary
            except Exception as e:
                rec.status, rec.error = "failed", f"{type(e).__name__}: {e}"
                self.state.put(s.id, rec)
                self.state.log("failed", step=s.id, error=rec.error)
                self.out(f"[{s.id}] FAILED: {rec.error}")
                summary["failed"] = s.id
                summary["status"] = "failed"
                return summary
        summary["status"] = "ok"
        return summary

    def clear_reboot(self):
        self.state.data["reboot_required"] = False
        self.state.save()

    def verify(self, phase: Optional[int] = None) -> list[PlanRow]:
        rows = self.plan(phase)
        for r in rows:
            mark = "✓" if r.satisfied else ("?" if r.satisfied is None else "✗")
            self.out(f"  {mark} [{r.step.id}] {r.step.title}")
        return rows

    def rollback(self, step_id: str) -> bool:
        s = self.by_id.get(step_id)
        if s is None:
            raise StepError(f"unknown step {step_id}")
        if s.rollback is None:
            self.out(f"[{s.id}] has no rollback procedure; see docs/PHASE1-PROXMOX.md §6")
            return False
        s.rollback()
        rec = self.state.rec(s.id)
        rec.status = "rolled_back"
        self.state.put(s.id, rec)
        self.state.log("rolled_back", step=s.id)
        return True

    def repair(self, step_id: str, understood: tuple[str, ...] = ()) -> dict:
        """Re-run a failed step after a human reviewed it. Resets the failed flag; destructive still needs the token."""
        rec = self.state.rec(step_id)
        rec.status = "pending"
        self.state.put(step_id, rec)
        return self.install(only=[step_id], understood=understood)

    def status(self) -> dict:
        return {"reboot_required": self.state.data.get("reboot_required", False),
                "steps": {sid: self.state.rec(sid).__dict__ for sid in self.by_id},
                "last_events": self.state.data.get("history", [])[-10:]}
