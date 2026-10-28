"""Wade's job handlers. Each returns (result, evidence[, state]) or raises on failure.

Kinds:
  * diagnostics.health — read-only service-health check.
  * dev.implement      — generate/edit a SET of files and request a gated deploy approval.

dev.implement works in two modes:
  * explicit  — the job payload names the files (`files`/`targets`/`target_file`).
  * planned   — the job gives only an `instruction`; Wade reads the repo (codebase awareness,
                rung 2), decides which files to edit/create, then proceeds.
All code is generated/tested against a throwaway clone and run inside Forge (rung 3), never here.
The deploy itself is done only by the deploy controller, only on a human-approved Sentinel token.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
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


# ---------------------------------------------------------------- helpers
def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True, timeout=180)


def _llm(ctx, system, prompt, timeout=300):
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


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    return s


def _extract_json(s: str):
    """Pull the first JSON array/object out of an LLM reply (tolerant of surrounding prose/fences)."""
    s = _strip_fences(s)
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = s.find(open_c), s.rfind(close_c)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    return json.loads(s)


def _norm_targets(p: dict) -> list[dict]:
    """Explicit file spec: 'files'/'targets' (list of paths or {path,instruction}) or legacy 'target_file'."""
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


# ---- rung 2: codebase awareness (plan which files to change) ----
_CODE_EXT = (".py", ".html", ".js", ".css", ".sh", ".sql", ".md", ".json", ".webmanifest",
             ".yaml", ".yml", ".toml", ".cfg", ".ini", ".txt", ".service")


def _repo_manifest(work: str, cap: int = 600) -> list[str]:
    files = []
    for base, dirs, fs in os.walk(work):
        if ".git" in dirs:
            dirs.remove(".git")
        for fn in fs:
            fp = os.path.join(base, fn)
            if fn.endswith(_CODE_EXT) or fn == "Dockerfile":
                try:
                    if os.path.getsize(fp) <= 200 * 1024:
                        files.append(os.path.relpath(fp, work))
                except OSError:
                    pass
    files.sort()
    return files[:cap]


_PLAN_READ_SYS = ("You are planning a code change in a repository. You are shown the file tree. Choose the "
                  "files you need to READ to make the change correctly. Respond with ONLY a JSON array of "
                  "file path strings (at most 10) — no prose, no code fences.")
_PLAN_SYS = ("You are planning a code change. Given the instruction and the contents of the relevant files, "
             "output a PLAN as a JSON array of objects, each {\"path\": str, \"action\": \"edit\" or "
             "\"create\", \"instruction\": str}. 'instruction' must be a precise, self-contained description "
             "of the change to THAT one file. Include only files that must change. Respond with ONLY the "
             "JSON array — no prose, no code fences.")


def _plan_files(ctx, instruction: str, work: str, manifest: list[str]) -> list[dict]:
    manifest_set = set(manifest)
    tree = "\n".join(manifest)
    # round 1 — which files to read
    try:
        want = _extract_json(_llm(ctx, _PLAN_READ_SYS,
                                  f"REPOSITORY FILES:\n{tree}\n\nCHANGE REQUESTED: {instruction}\n\n"
                                  f"JSON array of up to 10 paths to read:"))
        want = [p for p in want if isinstance(p, str) and p in manifest_set][:10]
    except Exception:
        want = []
    contents, budget = [], 24000
    for p in want:
        try:
            c = open(os.path.join(work, p), encoding="utf-8").read()[:6000]
        except Exception:
            continue
        if budget - len(c) < 0:
            break
        budget -= len(c)
        contents.append(f"# FILE: {p}\n{c}")
    ctxblock = "\n\n".join(contents) if contents else "(none read)"
    # round 2 — the plan
    plan = _extract_json(_llm(ctx, _PLAN_SYS,
                              f"CHANGE REQUESTED: {instruction}\n\nRELEVANT FILE CONTENTS:\n{ctxblock}\n\n"
                              f"FILE TREE (reference):\n{tree[:8000]}\n\nOutput the JSON plan now:"))
    out = []
    for item in (plan if isinstance(plan, list) else []):
        if not isinstance(item, dict) or not item.get("path"):
            continue
        p = item["path"]
        exists = os.path.exists(os.path.join(work, p))
        out.append({"path": p, "instruction": item.get("instruction") or instruction,
                    "action": item.get("action") or ("edit" if exists else "create")})
    return out[:12]


# ---- file generation ----
_DEV_SYS = ("You are a careful software engineer. You are given ONE file's full contents (which may be "
            "empty when the file is new) and an instruction, plus brief read-only context about the other "
            "files changing in the same task. Return the COMPLETE updated contents of THIS file and nothing "
            "else — no explanation, no markdown code fences. Make the smallest change that satisfies the "
            "instruction and keep everything else byte-for-byte identical.")


def _gen_file(ctx, instruction: str, t: dict, others_ctx: str, multi: bool) -> str:
    per = t["instruction"] or instruction
    newnote = "" if t["existed"] else " This file does NOT exist yet — create it from scratch."
    ctx_block = (f"\n\nCONTEXT — other files changing in this same task (read-only, do NOT output them):\n"
                 f"{others_ctx}\n") if multi else ""
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
    return new


# ---- rung 3: richer, dependency-safe testing in Forge ----
def _default_tests(changed: list[dict]) -> list[str]:
    cmds = []
    for t in changed:
        p = t["path"]
        if p.endswith(".py"):
            cmds.append(f"python3 -m py_compile {p}")
        elif p.endswith(".sh"):
            cmds.append(f"bash -n {p}")
        elif p.endswith((".json", ".webmanifest")):
            cmds.append(f"python3 -m json.tool {p} > /dev/null")
        else:
            cmds.append(f"test -f {p}")
    # compile every touched python package dir too, catching breakage in siblings
    for d in sorted({os.path.dirname(t["path"]) for t in changed if t["path"].endswith(".py")}):
        cmds.append(f"python3 -m compileall -q {d or '.'}")
    return cmds or ["true"]


def _forge_test(ctx, work: str, changed: list[dict], override):
    cmds = override or _default_tests(changed)
    ws = _tar_tree_b64(work)
    fr = httpx.post(f"{ctx['forge_url']}/run",
                    headers={"Authorization": f"Bearer {ctx['forge_token']}"},
                    json={"workspace_b64": ws, "commands": cmds, "timeout": 240}, timeout=420)
    fr.raise_for_status()
    forge = fr.json()
    summary = "; ".join(f"{r['cmd']} → exit {r['exit']}" for r in forge.get("results", []))
    if not forge.get("ok"):
        tail = (forge.get("results") or [{}])[-1].get("output", "")[-600:]
        raise RuntimeError(f"Forge tests failed: {summary} :: {tail}")
    return summary, forge.get("results")


def dev_implement(job: dict, ctx: dict):
    """Plan (if needed) → generate a file set → test in Forge → request ONE gated deploy approval."""
    p = job.get("payload") or {}
    repo_url = p.get("repo_url") or ctx.get("repo_url")
    instruction = p.get("instruction")
    base = p.get("base_branch", "main")
    explicit = _norm_targets(p)
    if not repo_url:
        raise ValueError("no repo_url (payload.repo_url or ctx.repo_url)")
    if not instruction:
        raise ValueError("no instruction")

    work = tempfile.mkdtemp(dir=ctx.get("work_root", "/tmp"))
    try:
        _git(["clone", "--depth", "1", "-b", base, repo_url, work], cwd="/")

        # rung 2: if the files were not named, read the repo and plan them.
        planned = False
        if explicit:
            targets = explicit
        else:
            targets = _plan_files(ctx, instruction, work, _repo_manifest(work))
            if not targets:
                raise ValueError("planner produced no files to change")
            planned = True

        for t in targets:
            fp = os.path.join(work, t["path"])
            t["existed"] = os.path.exists(fp)
            t["current"] = open(fp, encoding="utf-8").read() if t["existed"] else ""

        others_ctx = ""
        if len(targets) > 1:
            others_ctx = "\n\n".join(
                f"# FILE {t['path']} ({'existing' if t['existed'] else 'NEW'}):\n{t['current'][:1500]}"
                for t in targets)

        changed = []
        for t in targets:
            new = _gen_file(ctx, instruction, t, others_ctx, multi=len(targets) > 1)
            diff = "".join(difflib.unified_diff(
                t["current"].splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=("a/" + t["path"]) if t["existed"] else "/dev/null",
                tofile="b/" + t["path"]))
            if not diff.strip():
                continue
            os.makedirs(os.path.dirname(os.path.join(work, t["path"])) or work, exist_ok=True)
            open(os.path.join(work, t["path"]), "w", encoding="utf-8").write(new)
            t["new"] = new
            t["diff"] = diff[:6000]
            changed.append(t)

        if not changed:
            raise ValueError("no files changed — the model produced identical content for every target")

        summary, results = _forge_test(ctx, work, changed, p.get("test_commands"))

        files_payload = []
        for t in changed:
            b = t["new"].encode()
            files_payload.append({"path": t["path"],
                                  "new_sha256": hashlib.sha256(b).hexdigest(),
                                  "new_content_b64": base64.b64encode(b).decode(),
                                  "is_new": not t["existed"],
                                  "diff": t["diff"]})
        paths = [t["path"] for t in changed]
        plan_line = (" (Wade chose the files from the repo)" if planned else "")
        body = {"requester": {"kind": "supervisor", "id": ctx.get("actor_id", "wade")},
                "action": "deploy.code", "reason": instruction, "affected": paths,
                "consequences": (f"Replaces {len(paths)} file(s) after passing isolated tests in Forge"
                                 f"{plan_line}: " + ", ".join(paths)),
                "validation_evidence": [summary],
                "rollback": "Deploy controller restores each file's previous content (or removes new files) and restarts.",
                "payload": {"base_branch": base, "files": files_payload}}
        ar = httpx.post(f"{ctx['sentinel_url']}/approvals",
                        headers={"Authorization": f"Bearer {ctx['requester_token']}"},
                        json=body, timeout=20)
        ar.raise_for_status()
        aid = ar.json().get("id")
        result = {"files": paths, "planned": planned, "forge_ok": True, "forge_summary": summary,
                  "approval_id": aid, "diffs": {t["path"]: t["diff"] for t in changed}}
        evidence = {"forge": results, "approval_id": aid, "files": paths, "planned": planned}
        return result, evidence, "awaiting_approval"
    finally:
        shutil.rmtree(work, ignore_errors=True)


HANDLERS = {
    "diagnostics.health": diagnostics_health,
    "dev.implement": dev_implement,
}
