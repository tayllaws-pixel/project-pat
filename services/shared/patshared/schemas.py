"""Shared message contracts for all Project PAT services (PAT-025, spec §26).

Every supervisor, agent, and control-plane service speaks these. Adding a department means adding a
`supervisor` value, never changing Pat's core.
"""
from __future__ import annotations

import enum
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def canonical_json(obj: Any) -> str:
    """Deterministic JSON used for hashing payloads and audit rows."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


# ------------------------------------------------------------------ actors
class ActorKind(str, enum.Enum):
    human = "human"
    pat = "pat"
    supervisor = "supervisor"
    agent = "agent"
    system = "system"          # sentinel, installer, timers


class Actor(BaseModel):
    kind: ActorKind
    id: str                    # "taylor", "pat", "wade", "wade/agent/test-writer-3", "sentinel"
    version: Optional[str] = None


# ------------------------------------------------------------------ memory provenance (spec §6)
class ClaimStatus(str, enum.Enum):
    considered = "considered"
    decided = "decided"
    approved = "approved"
    purchased = "purchased"
    installed = "installed"
    completed = "completed"
    retracted = "retracted"


class Provenance(BaseModel):
    source_conversation_id: Optional[str] = None
    source_message_id: Optional[str] = None
    extracted_by: Optional[str] = None       # model id + version, or "human"
    recorded_at: datetime = Field(default_factory=utcnow)


# ------------------------------------------------------------------ tasks & jobs (spec §12, §28)
class Priority(str, enum.Enum):
    foreground = "foreground"      # Taylor is waiting
    high = "high"
    normal = "normal"
    background = "background"      # speculative/autonomous work; first to be throttled


class JobState(str, enum.Enum):
    queued = "queued"
    claimed = "claimed"
    running = "running"
    awaiting_approval = "awaiting_approval"
    awaiting_validation = "awaiting_validation"
    succeeded = "succeeded"
    failed = "failed"
    needs_reassessment = "needs_reassessment"   # after 3 failed local repairs (spec §12)
    cancelled = "cancelled"
    paused = "paused"


class TaskMessage(BaseModel):
    """A unit of work handed to a supervisor/agent."""
    id: str = Field(default_factory=new_id)
    kind: str                                  # "dev.implement", "research", "engineering.compare_designs", ...
    title: str
    requested_by: Actor
    assigned_to: Optional[str] = None          # actor id
    project_id: Optional[str] = None
    priority: Priority = Priority.normal
    depends_on: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = 3
    created_at: datetime = Field(default_factory=utcnow)
    deadline: Optional[datetime] = None

    @field_validator("max_attempts")
    @classmethod
    def _bounded(cls, v):
        if not 1 <= v <= 10:
            raise ValueError("max_attempts must be 1..10 (no infinite retry loops)")
        return v


class Checkpoint(BaseModel):
    job_id: str
    attempt: int
    stage: str                                 # "planned", "coded", "tests_written", "built", "tested", ...
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=utcnow)


# ------------------------------------------------------------------ events (spec §26)
class Event(BaseModel):
    id: str = Field(default_factory=new_id)
    type: str                                  # dotted: "job.failed", "approval.requested", "estop.engaged"
    actor: Actor
    subject: Optional[str] = None              # id of job/task/project/guest it concerns
    severity: Literal["debug", "info", "notice", "warning", "critical"] = "info"
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=utcnow)
    notify: bool = False                       # True → Pat decides whether to push to Taylor (spec §27)


class StatusReport(BaseModel):
    """Morning/evening or on-demand report from a supervisor. Must cite evidence (spec §28)."""
    supervisor: str
    period_start: datetime
    period_end: datetime
    completed: list[dict[str, Any]] = Field(default_factory=list)   # {task_id, title, evidence_uri}
    in_progress: list[dict[str, Any]] = Field(default_factory=list)
    blocked: list[dict[str, Any]] = Field(default_factory=list)
    planned: list[dict[str, Any]] = Field(default_factory=list)
    decisions_needed: list[str] = Field(default_factory=list)     # approval ids
    metrics: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ approvals (spec §17, ADR-0005)
class ActionClass(str, enum.Enum):
    automatic = "automatic"                    # within approved limits; logged only
    explicit = "explicit"                      # needs a human approval


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=new_id)
    requester: Actor
    action: str                                # "deploy.service", "guest.create", "external_ai.call", "data.delete", ...
    reason: str
    affected: list[str] = Field(default_factory=list)         # files, services, guests, datasets
    consequences: str = ""
    resources: dict[str, Any] = Field(default_factory=dict)   # {"cpu": 2, "ram_mb": 2048, "gpu_vram_mb": 0}
    estimated_external_cost_usd: float = 0.0
    validation_evidence: list[str] = Field(default_factory=list)   # URIs to test results, build logs
    rollback: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)     # the exact thing that will be executed
    ttl_seconds: int = 24 * 3600
    requested_at: datetime = Field(default_factory=utcnow)

    @property
    def payload_sha256(self) -> str:
        return sha256_hex(canonical_json(self.payload))


class ApprovalDecision(str, enum.Enum):
    approved = "approved"
    denied = "denied"
    expired = "expired"
    revoked = "revoked"


class ApprovalTokenClaims(BaseModel):
    aid: str                    # approval id
    action: str
    payload_sha256: str
    scope: list[str]            # affected resources the executor may touch
    nbf: int                    # not before, epoch seconds
    exp: int                    # expiry, epoch seconds
    nonce: str = Field(default_factory=new_id)
    issuer: str = "sentinel"


# ------------------------------------------------------------------ supervisor manifest (spec §13)
class ResourceQuota(BaseModel):
    cpu_cores: float = 1
    ram_mb: int = 1024
    gpu_vram_mb: int = 0
    disk_gb: int = 5
    max_concurrent_jobs: int = 2
    external_budget_usd_month: float = 0.0


class SupervisorManifest(BaseModel):
    name: str                          # "wade", "shuri"
    version: str                       # semver
    mission: str
    permissions: list[str]             # capability ids from the registry, e.g. "repo.read", "forge.exec", "inference.chat"
    agents: list[dict[str, Any]] = Field(default_factory=list)
    quotas: ResourceQuota = Field(default_factory=ResourceQuota)
    report_schedule: dict[str, str] = Field(default_factory=lambda: {"morning": "07:00", "evening": "19:00"})
    activated: bool = False            # activation requires human approval (spec §13)
    restricted_data_scopes: list[str] = Field(default_factory=list)   # e.g. ["project:lake-alapaha"] → denied


def export_json_schemas(out_dir: str) -> list[str]:
    import os
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for model in (Actor, Provenance, TaskMessage, Checkpoint, Event, StatusReport, ApprovalRequest,
                  ApprovalTokenClaims, SupervisorManifest, ResourceQuota):
        p = os.path.join(out_dir, f"{model.__name__}.schema.json")
        with open(p, "w") as f:
            json.dump(model.model_json_schema(), f, indent=2)
        written.append(p)
    return written
