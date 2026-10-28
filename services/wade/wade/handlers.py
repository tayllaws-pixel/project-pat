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


_DEV_SYS = ("You are a careful software engineer. You are given ONE file's full contents (which may be "
            "empty when the file is new) and an instruction, plus brief read-only context about the other "
            "files changing in the same task. Return the COMPLETE updated contents of THIS file and nothing "
            "else — no explanation, no markdown code fences. Make the smallest change that satisfies the "
            "instruction and keep everything else byte-for-byte identical.")


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    return s


def _norm_targets(p: dict) -> list[dict]:
    """Accept multi-file ('files' or 'targets': list of paths or {path, instruction}) or the legacy
    single-file shape ('target_file')."""
    out = []
    spec = p.get("files") or p.get("targets")
    if spec:
        for f in spec:
            if isinstance(f, str):
                out.append({"path": f, "instruction": None})
            elif isinstance(f, dict):
                out.append({"path": f.get("path") or f.get("file"), "instruction": f.get("instruction")})
    elif p.get("target_file"):
        out.append({"path": p["target_file"], "instruction": None})
    return [f for f in out if f.get("path")]


def dev_implement(job: dict, ctx: dict):
    """Generate/edit SEVERAL files, test the whole set in Forge, then request ONE deploy approval that
    carries every file. Edits existing files and creates new ones. Raises on test failure (worker retries)."""
    p = job.get("payload") or {}
    repo_url = p.get("repo_url") or ctx.get("repo_url")
    instruction = p.get("instruction")
    base = p.get("base_branch", "main")
    targets = _norm_targets(p)
    if not repo_url:
        raise ValueError("no repo_url (payload.repo_url or ctx.repo_url)")
    if not instruction:
        raise ValueError("no instruction")
    if not targets:
        raise ValueError("no target files (payload.files, payload.targets, or payload.target_file)")

    work = tempfile.mkdtemp(dir=ctx.get("work_root", "/tmp"))
    try:
        _git(["clone", "--depth", "1", "-b", base, repo_url, work], cwd="/")

        # read current contents; a missing file means "create it"
        for t in targets:
            fp = os.path.join(work, t["path"])
            t["existed"] = os.path.exists(fp)
            t["current"] = open(fp, encoding="utf-8").read() if t["existed"] else ""

        # brief cross-file context so multi-file edits stay consistent
        others_ctx = ""
        if len(targets) > 1:
            others_ctx = "\n\n".join(
                f"# FILE {t['path']} ({'existing' if t['existed'] else 'NEW'}):\n{t['current'][:1500]}"
                for t in targets)

        changed = []
        for t in targets:
            per = t["instruction"] or instruction
            newnote = "" if t["existed"] else " This file does NOT exist yet — create it from scratch."
            ctx_block = (f"\n\nCONTEXT — other files changing in this same task (read-only, do NOT output "
                         f"them):\n{others_ctx}\n") if len(targets) > 1 else ""
            prompt = (f"TASK: {instruction}\n\nFILE TO WRITE: {t['path']}{newnote}\n"
                      f"INSTRUCTION FOR THIS FILE: {per}{ctx_block}\n"
                      f"--- current contents of {t['path']} ---\n{t['current']}\n--- end ---\n\n"
                      f"Return the complete updated contents of {t['path']} now:")
            new = _strip_fences(_llm(ctx, _DEV_SYS, prompt))
            if t["existed"] and len(new) < 10:
                raise ValueError(f"model returned empty/too-short content for {t['path']}")
            if not t["existed"] and len(new) < 1:
                raise ValueError(f"model returned empty content for new file {t['path']}")
            if not new.endswith("\n"):
                new += "\n"
            diff = "".join(difflib.unified_diff(
                t["current"].splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=("a/" + t["path"]) if t["existed"] else "/dev/null",
                tofile="b/" + t["path"]))
            if not diff.strip():
                continue                      # this file ended up unchanged — skip it
            os.makedirs(os.path.dirname(os.path.join(work, t["path"])) or work, exist_ok=True)
            open(os.path.join(work, t["path"]), "w", encoding="utf-8").write(new)
            t["new"] = new
            t["diff"] = diff[:6000]
            changed.append(t)

        if not changed:
            raise ValueError("no files changed — the model produced identical content for every target")

        # test the whole changed tree together in Forge
        test_cmds = p.get("test_commands")
        if not test_cmds:
            test_cmds = [f"python3 -m py_compile {t['path']}" if t["path"].endswith(".py")
                         else f"test -f {t['path']}" for t in changed] or ["true"]
        ws = _tar_tree_b64(work)
        fr = httpx.post(f"{ctx['forge_url']}/run",
                        headers={"Authorization": f"Bearer {ctx['forge_token']}"},
                        json={"workspace_b64": ws, "commands": test_cmds, "timeout": 180}, timeout=360)
        fr.raise_for_status()
        forge = fr.json()
        summary = "; ".join(f"{r['cmd']} → exit {r['exit']}" for r in forge.get("results", []))
        if not forge.get("ok"):
            tail = (forge.get("results") or [{}])[-1].get("output", "")[-600:]
            raise RuntimeError(f"Forge tests failed: {summary} :: {tail}")

        # ONE approval carrying every file. The token's payload hash binds the EXACT bytes of all of them,
        # so nothing can be swapped after approval.
        files_payload = []
        for t in changed:
            b = t["new"].encode()
            files_payload.append({"path": t["path"],
                                  "new_sha256": hashlib.sha256(b).hexdigest(),
                                  "new_content_b64": base64.b64encode(b).decode(),
                                  "is_new": not t["existed"],
                                  "diff": t["diff"]})
        paths = [t["path"] for t in changed]
        body = {"requester": {"kind": "supervisor", "id": ctx.get("actor_id", "wade")},
                "action": "deploy.code", "reason": instruction, "affected": paths,
                "consequences": (f"Replaces {len(paths)} file(s) after passing isolated tests in Forge: "
                                 + ", ".join(paths)),
                "validation_evidence": [summary],
                "rollback": "Deploy controller restores each file's previous content (or removes new files) and restarts.",
                "payload": {"base_branch": base, "files": files_payload}}
        ar = httpx.post(f"{ctx['sentinel_url']}/approvals",
                        headers={"Authorization": f"Bearer {ctx['requester_token']}"},
                        json=body, timeout=20)
        ar.raise_for_status()
        aid = ar.json().get("id")
        result = {"files": paths, "forge_ok": True, "forge_summary": summary, "approval_id": aid,
                  "diffs": {t["path"]: t["diff"] for t in changed}}
        evidence = {"forge": forge.get("results"), "approval_id": aid, "files": paths}
        return result, evidence, "awaiting_approval"
    finally:
        shutil.rmtree(work, ignore_errors=True)


HANDLERS = {
    "diagnostics.health": diagnostics_health,
    "dev.implement": dev_implement,
}
