"""Sentinel core (PAT-026): the independent safety plane's pure logic.

Three guarantees, each enforced here rather than by prompt text:
  1. E-stop — authoritative flag; workers must call `gate.allow_autonomous_work()` before claiming any job.
  2. Approval tokens — Ed25519-signed, scoped, time-limited, bound to a payload hash, single-use (ADR-0005).
  3. Audit chain — hash-chained append-only log; `verify_chain()` detects edits, deletions and reordering.

Storage is abstracted by `Store` so the logic is tested with an in-memory store; the Postgres store maps 1:1 to
the `sentinel` database in db/migrations/0001_init.sql.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from patshared.schemas import (ApprovalDecision, ApprovalRequest, ApprovalTokenClaims, canonical_json, sha256_hex,
                               utcnow)

GENESIS = "0" * 64


class SentinelError(Exception):
    pass


class EStopEngaged(SentinelError):
    pass


class ApprovalRejected(SentinelError):
    pass


# ------------------------------------------------------------------ storage protocol
class Store(Protocol):
    def get_estop(self) -> dict: ...
    def set_estop(self, engaged: bool, actor: str, reason: str) -> None: ...
    def get_agent_control(self, actor_id: str) -> dict: ...
    def set_agent_control(self, actor_id: str, paused: Optional[bool], disabled: Optional[bool], by: str, note: str) -> None: ...
    def put_approval(self, req: ApprovalRequest, expires_at: float) -> None: ...
    def get_approval(self, aid: str) -> Optional[dict]: ...
    def decide_approval(self, aid: str, decision: ApprovalDecision, by: str, note: str) -> None: ...
    def put_token(self, nonce: str, aid: str, exp: float) -> None: ...
    def consume_token(self, nonce: str, by: str) -> bool: ...
    def append_audit(self, row: dict) -> None: ...
    def audit_rows(self) -> list[dict]: ...
    def last_audit_hash(self) -> str: ...


class MemoryStore:
    """In-memory Store for tests and for the `sentinel --selftest` command."""

    def __init__(self):
        self.estop = {"engaged": False, "engaged_by": None, "engaged_at": None, "reason": None, "released_by": None, "released_at": None}
        self.controls: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.audit: list[dict] = []

    def get_estop(self): return dict(self.estop)
    def set_estop(self, engaged, actor, reason):
        if engaged:
            self.estop.update(engaged=True, engaged_by=actor, engaged_at=time.time(), reason=reason)
        else:
            self.estop.update(engaged=False, released_by=actor, released_at=time.time())
    def get_agent_control(self, actor_id): return dict(self.controls.get(actor_id, {"paused": False, "disabled": False}))
    def set_agent_control(self, actor_id, paused, disabled, by, note):
        c = self.controls.setdefault(actor_id, {"paused": False, "disabled": False})
        if paused is not None: c["paused"] = paused
        if disabled is not None: c["disabled"] = disabled
        c.update(set_by=by, note=note)
    def put_approval(self, req, expires_at):
        self.approvals[req.id] = {"req": req, "expires_at": expires_at, "decision": None, "decided_by": None}
    def get_approval(self, aid): return self.approvals.get(aid)
    def decide_approval(self, aid, decision, by, note):
        self.approvals[aid].update(decision=decision, decided_by=by, note=note, decided_at=time.time())
    def put_token(self, nonce, aid, exp): self.tokens[nonce] = {"aid": aid, "exp": exp, "used_at": None}
    def consume_token(self, nonce, by):
        t = self.tokens.get(nonce)
        if t is None or t["used_at"] is not None:
            return False
        t["used_at"], t["used_by"] = time.time(), by
        return True
    def append_audit(self, row): self.audit.append(row)
    def audit_rows(self): return list(self.audit)
    def last_audit_hash(self): return self.audit[-1]["hash"] if self.audit else GENESIS


# ------------------------------------------------------------------ keys
@dataclass
class KeyPair:
    private: Optional[Ed25519PrivateKey]
    public: Ed25519PublicKey

    @classmethod
    def generate(cls) -> "KeyPair":
        k = Ed25519PrivateKey.generate()
        return cls(k, k.public_key())

    @classmethod
    def load(cls, private_path: Optional[str], public_path: Optional[str] = None) -> "KeyPair":
        if private_path and Path(private_path).exists():
            k = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
            return cls(k, k.public_key())
        pub = serialization.load_pem_public_key(Path(public_path).read_bytes())
        return cls(None, pub)

    def save(self, private_path: str, public_path: str) -> None:
        Path(private_path).parent.mkdir(parents=True, exist_ok=True)
        Path(private_path).write_bytes(self.private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        Path(private_path).chmod(0o600)
        Path(public_path).write_bytes(self.public.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

    def public_pem(self) -> str:
        return self.public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------ audit chain
class Audit:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def row_hash(prev_hash: str, row_wo_hash: dict) -> str:
        return sha256_hex(prev_hash + canonical_json(row_wo_hash))

    def record(self, actor: str, action: str, details: Optional[dict] = None) -> dict:
        prev = self.store.last_audit_hash()
        row = {"ts": utcnow().isoformat(timespec="microseconds"), "actor": actor, "action": action,
               "details": details or {}, "prev_hash": prev}
        row["hash"] = self.row_hash(prev, row)
        self.store.append_audit(row)
        return row

    def verify_chain(self) -> tuple[bool, Optional[int]]:
        """Return (ok, first_bad_index)."""
        prev = GENESIS
        for i, row in enumerate(self.store.audit_rows()):
            body = {k: v for k, v in row.items() if k != "hash"}
            if row.get("prev_hash") != prev or self.row_hash(prev, body) != row.get("hash"):
                return False, i
            prev = row["hash"]
        return True, None


# ------------------------------------------------------------------ e-stop and per-agent controls
class Gate:
    """What every autonomous worker consults before doing anything (spec §18)."""

    def __init__(self, store: Store, audit: Audit, flag_file: Optional[str] = None):
        self.store, self.audit, self.flag_file = store, audit, flag_file

    def estop_engaged(self) -> bool:
        if self.flag_file and Path(self.flag_file).exists():      # file flag survives DB outage
            return True
        return bool(self.store.get_estop()["engaged"])

    def engage_estop(self, actor: str, reason: str) -> None:
        self.store.set_estop(True, actor, reason)
        if self.flag_file:
            Path(self.flag_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self.flag_file).write_text(f"{actor} {utcnow().isoformat()} {reason}\n")
        self.audit.record(actor, "estop.engaged", {"reason": reason})

    def release_estop(self, actor: str, human_authenticated: bool) -> None:
        """Only an authenticated human may release (spec §18). Wade calling this is an audited violation."""
        if not human_authenticated:
            self.audit.record(actor, "estop.release_denied", {"why": "not an authenticated human"})
            raise SentinelError("e-stop release requires an authenticated human")
        self.store.set_estop(False, actor, "")
        if self.flag_file and Path(self.flag_file).exists():
            Path(self.flag_file).unlink()
        self.audit.record(actor, "estop.released", {})

    def set_agent(self, actor_id: str, *, paused: Optional[bool] = None, disabled: Optional[bool] = None, by: str, note: str = ""):
        self.store.set_agent_control(actor_id, paused, disabled, by, note)
        self.audit.record(by, "agent.control", {"actor": actor_id, "paused": paused, "disabled": disabled, "note": note})

    def allow_autonomous_work(self, actor_id: str) -> None:
        """Raise unless this agent may claim work right now."""
        if self.estop_engaged():
            raise EStopEngaged("emergency stop is engaged; no autonomous work may start")
        c = self.store.get_agent_control(actor_id)
        if c.get("disabled"):
            raise SentinelError(f"agent {actor_id} is disabled")
        if c.get("paused"):
            raise SentinelError(f"agent {actor_id} is paused")
        for parent in _ancestors(actor_id):            # disabling 'wade' disables 'wade/agent/x'
            pc = self.store.get_agent_control(parent)
            if pc.get("disabled") or pc.get("paused"):
                raise SentinelError(f"agent {actor_id} blocked because {parent} is {'disabled' if pc.get('disabled') else 'paused'}")


def _ancestors(actor_id: str) -> list[str]:
    parts = actor_id.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


# ------------------------------------------------------------------ approvals
# Actions that are automatic within limits vs. explicit (spec §17). Unknown actions default to explicit.
AUTOMATIC_ACTIONS = {
    "research.read", "project.read", "diagnostics.run", "monitoring.read", "forge.build", "forge.test",
    "job.retry", "benchmark.run", "agent.create_specialist", "maintenance.small_reversible",
}
EXPLICIT_ACTIONS = {
    "department.activate", "data.delete", "storage.wipe", "host.security_policy", "network.open_public_port",
    "purchase", "budget.exceed", "data.send_external", "infra.major_change", "permissions.modify",
    "db.migration_critical", "hardware.irreversible", "deploy.production", "guest.create", "guest.destroy",
    "model.replace_default", "estop.release",
    "deploy.code", "infra.migrate", "infra.provision", "infra.supervisor",   # code + infra always need a human
}


def classify_action(action: str) -> str:
    return "automatic" if action in AUTOMATIC_ACTIONS else "explicit"


@dataclass
class Approvals:
    store: Store
    audit: Audit
    keys: KeyPair
    max_token_ttl: int = 3600
    _clock: callable = field(default=time.time, repr=False)

    def request(self, req: ApprovalRequest) -> dict:
        if self.estop_blocks(req.action):
            raise EStopEngaged("e-stop engaged; only estop.release may be requested")
        expires_at = self._clock() + req.ttl_seconds
        self.store.put_approval(req, expires_at)
        cls = classify_action(req.action)
        self.audit.record(req.requester.id, "approval.requested",
                          {"aid": req.id, "action": req.action, "class": cls, "payload_sha256": req.payload_sha256,
                           "cost_usd": req.estimated_external_cost_usd})
        return {"id": req.id, "class": cls, "expires_at": expires_at}

    def estop_blocks(self, action: str) -> bool:
        return bool(self.store.get_estop()["engaged"]) and action != "estop.release"

    def decide(self, aid: str, decision: ApprovalDecision, by: str, note: str = "", human_authenticated: bool = True) -> Optional[str]:
        """Record a decision. Returns a signed token when approved."""
        rec = self.store.get_approval(aid)
        if rec is None:
            raise ApprovalRejected("unknown approval")
        if not human_authenticated:
            self.audit.record(by, "approval.decision_denied", {"aid": aid, "why": "not an authenticated human"})
            raise ApprovalRejected("approvals can only be decided by an authenticated human")
        if rec["decision"] is not None:
            raise ApprovalRejected(f"approval already {rec['decision'].value}")
        if self._clock() > rec["expires_at"]:
            self.store.decide_approval(aid, ApprovalDecision.expired, "sentinel", "expired before decision")
            self.audit.record("sentinel", "approval.expired", {"aid": aid})
            raise ApprovalRejected("approval request expired")
        self.store.decide_approval(aid, decision, by, note)
        self.audit.record(by, f"approval.{decision.value}", {"aid": aid, "note": note})
        if decision != ApprovalDecision.approved:
            return None
        return self._issue_token(rec["req"])

    def _issue_token(self, req: ApprovalRequest) -> str:
        if self.keys.private is None:
            raise SentinelError("this Sentinel instance holds no private key; cannot issue tokens")
        now = int(self._clock())
        claims = ApprovalTokenClaims(aid=req.id, action=req.action, payload_sha256=req.payload_sha256,
                                     scope=list(req.affected), nbf=now - 5, exp=now + min(self.max_token_ttl, req.ttl_seconds))
        body = canonical_json(claims.model_dump()).encode()
        sig = self.keys.private.sign(body)
        self.store.put_token(claims.nonce, req.id, claims.exp)
        self.audit.record("sentinel", "approval.token_issued", {"aid": req.id, "nonce": claims.nonce, "exp": claims.exp})
        return f"{_b64e(body)}.{_b64e(sig)}"

    def verify_and_consume(self, token: str, action: str, payload: dict, executor: str, resource=None) -> ApprovalTokenClaims:
        """Executor-side check. Any failure is audited and raises; success marks the nonce used (single-use)."""
        def deny(why: str):
            self.audit.record(executor, "approval.token_rejected", {"why": why, "action": action})
            raise ApprovalRejected(why)
        try:
            b, s = token.split(".")
            body, sig = _b64d(b), _b64d(s)
            self.keys.public.verify(sig, body)
            claims = ApprovalTokenClaims(**json.loads(body))
        except (ValueError, InvalidSignature, TypeError):
            deny("bad signature or malformed token")
        now = self._clock()
        if now < claims.nbf or now > claims.exp:
            deny("token expired or not yet valid")
        if claims.action != action:
            deny(f"token is for action {claims.action}, not {action}")
        if claims.payload_sha256 != sha256_hex(canonical_json(payload)):
            deny("payload does not match approved payload")
        if resource is not None and claims.scope:
            needed = resource if isinstance(resource, (list, tuple)) else [resource]
            outside = [r for r in needed if r not in claims.scope]
            if outside:
                deny(f"resource(s) {', '.join(map(str, outside))} outside approved scope")
        rec = self.store.get_approval(claims.aid)
        if rec is None or rec["decision"] != ApprovalDecision.approved:
            deny("approval revoked or missing")
        if self.estop_blocks(action):
            deny("e-stop engaged")
        if not self.store.consume_token(claims.nonce, executor):
            deny("token already used (replay)")
        self.audit.record(executor, "approval.token_consumed", {"aid": claims.aid, "nonce": claims.nonce})
        return claims

    def revoke(self, aid: str, by: str, note: str = "") -> None:
        rec = self.store.get_approval(aid)
        if rec is None:
            raise ApprovalRejected("unknown approval")
        self.store.decide_approval(aid, ApprovalDecision.revoked, by, note)
        self.audit.record(by, "approval.revoked", {"aid": aid, "note": note})
