"""Wade's v1 job handlers. Each returns (result, evidence) or raises on failure.

v1 ships one safe, read-only kind — service-health diagnostics — which is enough to prove the gated
engine end-to-end. The code-writing pipeline ("dev.implement": plan -> code -> Forge build/test ->
request deploy approval) is the next increment and will run all code execution inside Forge, never here.
"""
from __future__ import annotations

import time

import httpx


def diagnostics_health(job: dict, ctx: dict):
    """Check each target's /healthz. The job succeeds if the check runs; unhealthy services are reported."""
    targets = (job.get("payload") or {}).get("targets") or ctx.get("default_targets") or []
    if not targets:
        raise ValueError("no targets to check (payload.targets or default_targets)")
    results = []
    for t in targets:
        name, url = t.get("name"), t.get("url")
        ok, detail = False, ""
        try:
            r = httpx.get(url, timeout=8)
            ok = r.status_code == 200
            detail = r.text[:120]
        except Exception as e:
            detail = str(e)[:120]
        results.append({"name": name, "url": url, "ok": ok, "detail": detail})
    healthy = sum(1 for x in results if x["ok"])
    result = {"healthy": healthy, "total": len(results), "all_ok": healthy == len(results),
              "services": results}
    evidence = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return result, evidence


HANDLERS = {
    "diagnostics.health": diagnostics_health,
}
