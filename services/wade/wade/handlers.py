"""Wade's v1 job handlers. Each returns (result, evidence) or raises on failure.

v1 ships one safe, read-only kind — service-health diagnostics — which is enough to prove the gated
engine end-to-end. The code-writing pipeline ("dev.implement": plan -> code -> Forge build/test ->
request deploy approval) is the next increment and will run all code execution inside Forge, never here.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
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


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True, timeout=180)


def _llm(ctx, system, prompt, timeout=240):
    r = httpx.post(f"{ctx['inference_url']}/api/generate",
                   json={"model": ctx["code_model"], "system": system, "prompt": prompt,
                         "stream": False, "think": False, "options": {"temperature": 0.2}},
                   timeout=timeout)
    r.raise_for_status()
    return re.sub(r"<think>.*?</think>", "", r.json().get("response", ""), flags=re.S)


def _tar_tree_b64(root: str) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for base, dirs, files in os.walk(root):
            if ".git" in dirs:
                dirs.remove(".git")
            for fn in files:
                fp = os.path.join(base, fn)
                tf.add(fp, arcname=os.path.relpath(fp, root))
    return base64.b64encode(buf.getvalue()).decode()


_DEV_SYS = ("You are a careful software engineer. You are given one file's full contents and an "
            "instruction. Return the COMPLETE updated file and nothing else — no explanation, no "
            "markdown code fences. Make the smallest change that satisfies the instruction and keep the "
            "rest of the file byte-for-byte identical.")


def dev_implement(job: dict, ctx: dict):
    """Plan→generate→test-in-Forge→request deploy approval. Raises on test failure (worker retries)."""
    p = job.get("payload") or {}
    repo_url = p.get("repo_url") or ctx.get("repo_url")
    target = p["target_file"]
    instruction = p["instruction"]
    base = p.get("base_branch", "main")
    test_cmds = p.get("test_commands")
    if not test_cmds:
        test_cmds = [f"python3 -m py_compile {target}"] if target.endswith(".py") else [f"test -f {target}"]
    if not repo_url:
        raise ValueError("no repo_url (payload.repo_url or ctx.repo_url)")

    work = tempfile.mkdtemp(dir=ctx.get("work_root", "/tmp"))
    try:
        _git(["clone", "--depth", "1", "-b", base, repo_url, work], cwd="/")
        tpath = os.path.join(work, target)
        if not os.path.exists(tpath):
            raise ValueError(f"target file {target} not found in repo")
        current = open(tpath, encoding="utf-8").read()

        prompt = (f"FILE: {target}\nINSTRUCTION: {instruction}\n\n--- current contents ---\n{current}"
                  f"\n--- end of current contents ---\n\nReturn the complete updated file now:")
        new = _llm(ctx, _DEV_SYS, prompt).strip()
        if new.startswith("```"):
            new = re.sub(r"^```[a-zA-Z0-9]*\n", "", new)
            new = re.sub(r"\n```$", "", new)
        if len(new) < 10:
            raise ValueError("model returned empty/too-short content")
        if not new.endswith("\n"):
            new += "\n"
        open(tpath, "w", encoding="utf-8").write(new)

        diff = "".join(difflib.unified_diff(current.splitlines(keepends=True),
                                            new.splitlines(keepends=True),
                                            fromfile="a/" + target, tofile="b/" + target))[:8000]
        if not diff.strip():
            raise ValueError("generated file is identical to the original — no change produced")

        ws = _tar_tree_b64(work)
        fr = httpx.post(f"{ctx['forge_url']}/run",
                        headers={"Authorization": f"Bearer {ctx['forge_token']}"},
                        json={"workspace_b64": ws, "commands": test_cmds, "timeout": 120}, timeout=300)
        fr.raise_for_status()
        forge = fr.json()
        summary = "; ".join(f"{r['cmd']} → exit {r['exit']}" for r in forge.get("results", []))
        if not forge.get("ok"):
            tail = (forge.get("results") or [{}])[-1].get("output", "")[-600:]
            raise RuntimeError(f"Forge tests failed: {summary} :: {tail}")

        sha = hashlib.sha256(new.encode()).hexdigest()
        body = {"requester": {"kind": "supervisor", "id": ctx.get("actor_id", "wade")},
                "action": "deploy.code", "reason": instruction, "affected": [target],
                "consequences": f"Replaces {target} after passing isolated tests in Forge.",
                "validation_evidence": [summary], "rollback": "Discard the branch; the live file is unchanged.",
                "payload": {"target_file": target, "new_sha256": sha, "base_branch": base}}
        ar = httpx.post(f"{ctx['sentinel_url']}/approvals",
                        headers={"Authorization": f"Bearer {ctx['requester_token']}"},
                        json=body, timeout=15)
        ar.raise_for_status()
        aid = ar.json().get("id")
        result = {"target_file": target, "forge_ok": True, "forge_summary": summary,
                  "approval_id": aid, "diff": diff}
        evidence = {"forge": forge.get("results"), "approval_id": aid}
        return result, evidence, "awaiting_approval"
    finally:
        shutil.rmtree(work, ignore_errors=True)


HANDLERS = {
    "diagnostics.health": diagnostics_health,
    "dev.implement": dev_implement,
}
