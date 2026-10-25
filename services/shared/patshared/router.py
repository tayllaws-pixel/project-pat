"""Config-driven model router (ADR-0006). Pure logic: reads models.toml, picks a model per role from recorded
benchmarks; the inference HTTP call lives in the service that uses it."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Candidate:
    name: str
    vram_mb: int
    context: int
    notes: str = ""


@dataclass(frozen=True)
class Role:
    name: str
    metric: str                 # which benchmark metric decides the winner ("quality", "tokens_per_s", "accuracy")
    candidates: tuple[Candidate, ...]
    always_resident: bool = False
    tier: str = "large"         # large | small


class Router:
    def __init__(self, config_path: str | Path, benchmarks: dict[str, dict[str, float]] | None = None,
                 vram_budget_mb: int = 16384):
        cfg = tomllib.loads(Path(config_path).read_text())
        self.vram_budget_mb = int(cfg.get("policy", {}).get("vram_budget_mb", vram_budget_mb))
        self.max_large_resident = int(cfg.get("policy", {}).get("max_large_resident", 2))
        self.roles: dict[str, Role] = {}
        for rname, r in cfg["roles"].items():
            cands = tuple(Candidate(c["name"], int(c["vram_mb"]), int(c.get("context", 8192)), c.get("notes", "")) for c in r["candidates"])
            self.roles[rname] = Role(rname, r.get("metric", "quality"), cands, bool(r.get("always_resident", False)), r.get("tier", "large"))
        self.benchmarks = benchmarks or {}     # {model_name: {metric: value}}
        self.pinned: dict[str, str] = cfg.get("pinned", {})   # role -> model, set by a recorded decision

    def choose(self, role: str) -> Candidate:
        r = self.roles[role]
        if role in self.pinned:
            return next(c for c in r.candidates if c.name == self.pinned[role])
        scored = [(self.benchmarks.get(c.name, {}).get(r.metric), c) for c in r.candidates]
        measured = [(s, c) for s, c in scored if s is not None]
        if measured:
            return max(measured, key=lambda t: t[0])[1]
        # No benchmark yet: the first candidate is the documented default, never "largest wins" (spec §15).
        return r.candidates[0]

    def fits(self, resident: list[Candidate], extra: Candidate) -> bool:
        total = sum(c.vram_mb for c in resident) + extra.vram_mb
        large = sum(1 for c in resident + [extra] if c.vram_mb > 6000)
        return total <= self.vram_budget_mb and large <= self.max_large_resident

    def eviction_order(self, resident: list[tuple[Candidate, str]]) -> list[Candidate]:
        """Given (candidate, priority) pairs, return who to evict first: background large models before foreground."""
        rank = {"background": 0, "normal": 1, "high": 2, "foreground": 3}
        resident_roles = {c.name: r for r in self.roles.values() for c in r.candidates}
        return [c for c, _ in sorted(resident, key=lambda t: (rank.get(t[1], 1), -t[0].vram_mb))
                if not resident_roles.get(c.name, Role("", "", ())).always_resident]
