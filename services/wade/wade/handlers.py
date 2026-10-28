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


def _coerce_plan(plan):
    """Normalise the planner's JSON to a list of items. The model sometimes wraps the array in an
    object ({"plan": [...]}, {"files": [...]}) or returns a single object — accept all of these."""
    if isinstance(plan, list):
        return plan
    if isinstance(plan, dict):
        for v in plan.values():
            if isinstance(v, list):
                return v
        if plan.get("path"):
            return [plan]
    return []


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
    try:
        plan = _coerce_plan(_extract_json(_llm(ctx, _PLAN_SYS,
                            f"CHANGE REQUESTED: {instruction}\n\nRELEVANT FILE CONTENTS:\n{ctxblock}\n\n"
                            f"FILE TREE (reference):\n{tree[:8000]}\n\nOutput the JSON plan now:")))
    except Exception:
        plan = []
    out = []
    for item in plan:
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


# ---------------------------------------------------------------- rung 6: infrastructure changes
_MIG_SYS = ("You are writing a single PostgreSQL migration. Output ONLY SQL — no prose, no markdown fences. "
            "Make it idempotent (CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS). When you create a "
            "table, add a matching GRANT SELECT, INSERT, UPDATE, DELETE on it to the <db>_app role. Never "
            "drop, delete, or truncate anything.")


def _next_migration_version(work: str) -> str:
    import glob
    vers = [0]
    for f in glob.glob(os.path.join(work, "db", "migrations", "*.sql")):
        m = re.match(r"(\d{4})_", os.path.basename(f))
        if m:
            vers.append(int(m.group(1)))
    return f"{max(vers) + 1:04d}"


def infra_migrate(job: dict, ctx: dict):
    """Generate a numbered DB migration from an instruction and request an infra.migrate approval."""
    p = job.get("payload") or {}
    db = p.get("db")
    instruction = p.get("instruction")
    if db not in ("pat", "wade", "sentinel"):
        raise ValueError("payload.db must be pat, wade, or sentinel")
    if not instruction:
        raise ValueError("no instruction")
    repo_url = p.get("repo_url") or ctx.get("repo_url")
    base = p.get("base_branch", "main")
    work = tempfile.mkdtemp(dir=ctx.get("work_root", "/tmp"))
    try:
        _git(["clone", "--depth", "1", "-b", base, repo_url, work], cwd="/")
        import glob
        ctxsql = ""
        for f in sorted(glob.glob(os.path.join(work, "db", "migrations", "*.sql"))):
            ctxsql += f"# {os.path.basename(f)}\n{open(f, encoding='utf-8').read()[:4000]}\n\n"
        ver = _next_migration_version(work)
        slug = re.sub(r"[^a-z0-9]+", "_", instruction.lower()).strip("_")[:40] or "change"
        name = f"{ver}_{slug}.sql"
        sql = _strip_fences(_llm(ctx, _MIG_SYS,
                                 f"TARGET DATABASE: {db}\nTASK: {instruction}\n\n"
                                 f"EXISTING SCHEMA (migrations so far):\n{ctxsql[:16000]}\n\n"
                                 f"Output the SQL for the new migration now:"))
        if len(sql) < 5:
            raise ValueError("model returned empty SQL")
        sql = f"-- {name}: {instruction}\n-- @db {db}\n{sql}"
        if not sql.endswith("\n"):
            sql += "\n"
        b = sql.encode()
        body = {"requester": {"kind": "supervisor", "id": ctx.get("actor_id", "wade")},
                "action": "infra.migrate", "reason": instruction, "affected": [name],
                "consequences": f"Applies migration {name} to the {db} database.",
                "validation_evidence": ["generated SQL — review it before approving"],
                "rollback": "The migration runs in one transaction; any error rolls it back automatically.",
                "payload": {"db": db, "migration_name": name, "migration_sql": sql,
                            "migration_sha256": hashlib.sha256(b).hexdigest(),
                            "allow_destructive": bool(p.get("allow_destructive"))}}
        ar = httpx.post(f"{ctx['sentinel_url']}/approvals",
                        headers={"Authorization": f"Bearer {ctx['requester_token']}"}, json=body, timeout=20)
        ar.raise_for_status()
        aid = ar.json().get("id")
        return {"migration": name, "db": db, "approval_id": aid}, {"approval_id": aid}, "awaiting_approval"
    finally:
        shutil.rmtree(work, ignore_errors=True)


_SVC_SYS = ("You are writing a small self-contained Python web service using FastAPI. It runs in its own "
            "container with NO credentials and NO access to any other service. Output ONLY the complete "
            "contents of app.py — no prose, no markdown fences. It MUST define a FastAPI app named `app` and "
            "expose GET /healthz returning {\"ok\": True}. Use only the standard library plus fastapi. Bind "
            "host 0.0.0.0.")


def infra_provision(job: dict, ctx: dict):
    """Generate a small service and request an infra.provision approval to create its container."""
    p = job.get("payload") or {}
    name = (p.get("name") or "").strip()
    instruction = p.get("instruction")
    if not re.match(r"^[a-z][a-z0-9-]{1,30}$", name):
        raise ValueError("payload.name must be a lowercase slug (a-z, 0-9, -)")
    if not instruction:
        raise ValueError("no instruction")
    ctid = int(p.get("ctid", 130))
    port = int(p.get("port", 8120))
    deps = p.get("deps") or ["fastapi", "uvicorn[standard]"]
    work = tempfile.mkdtemp(dir=ctx.get("work_root", "/tmp"))
    try:
        appcode = _strip_fences(_llm(ctx, _SVC_SYS,
                                     f"SERVICE NAME: {name}\nPORT: {port}\nWHAT IT SHOULD DO: {instruction}\n\n"
                                     f"Write app.py now (must include GET /healthz):"))
        if len(appcode) < 20:
            raise ValueError("model returned empty service code")
        if not appcode.endswith("\n"):
            appcode += "\n"
        # Test flat at the workspace root: Forge cd's into a lone top-level dir, so a nested path
        # (name/app.py) would be looked up in the wrong place.
        open(os.path.join(work, "app.py"), "w", encoding="utf-8").write(appcode)
        summary, results = _forge_test(ctx, work, [{"path": "app.py"}], None)
        b = appcode.encode()
        files = [{"path": "app.py", "content_b64": base64.b64encode(b).decode(),
                  "sha256": hashlib.sha256(b).hexdigest()}]
        exec_start = f"/opt/{name}-venv/bin/uvicorn app:app --host 0.0.0.0 --port {port}"
        body = {"requester": {"kind": "supervisor", "id": ctx.get("actor_id", "wade")},
                "action": "infra.provision", "reason": instruction, "affected": [name],
                "consequences": (f"Creates container CT {ctid} ({name}) listening on port {port} — "
                                 f"unprivileged, no GPU, no credentials."),
                "validation_evidence": [summary],
                "rollback": "If the new service does not come up healthy, its container is destroyed.",
                "payload": {"name": name, "ctid": ctid, "cores": int(p.get("cores", 1)),
                            "memory_mb": int(p.get("memory_mb", 512)), "disk_gb": int(p.get("disk_gb", 8)),
                            "port": port, "deps": deps, "files": files, "exec_start": exec_start,
                            "healthz": "/healthz", "app_preview": appcode[:4000]}}
        ar = httpx.post(f"{ctx['sentinel_url']}/approvals",
                        headers={"Authorization": f"Bearer {ctx['requester_token']}"}, json=body, timeout=20)
        ar.raise_for_status()
        aid = ar.json().get("id")
        return ({"service": name, "ctid": ctid, "port": port, "approval_id": aid},
                {"approval_id": aid, "forge": results}, "awaiting_approval")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- rung 7: supervisor/agent factory
_SUP_SYS = ("You are writing the AGENT HANDLERS for a new Project PAT supervisor — a Python file called "
            "handlers.py. Output ONLY the complete handlers.py — no prose, no markdown fences. Define one or "
            "more handler functions with the signature fn(job, ctx) -> (result_dict, evidence_dict), and a "
            "module-level dict named HANDLERS mapping each job 'kind' (a short dotted string like "
            "'notes.add') to its function. `job` is a dict with a 'payload' dict; `ctx` has keys "
            "sentinel_url, requester_token, actor_id, inference_url, code_model. Handlers may compute and "
            "read, and may call the LLM at ctx['inference_url'] with httpx, but must NOT perform privileged "
            "actions directly. Use only the standard library plus (optionally) httpx. You MUST define HANDLERS.")


def infra_supervisor(job: dict, ctx: dict):
    """Generate a new supervisor's agent handlers and request an infra.supervisor approval. The trusted
    runtime scaffold (queue + fail-closed gated worker + control API) is added by the deploy controller;
    only these handlers are generated here."""
    p = job.get("payload") or {}
    name = (p.get("name") or "").strip()
    instruction = p.get("instruction")
    if not re.match(r"^[a-z][a-z0-9-]{1,30}$", name):
        raise ValueError("payload.name must be a lowercase slug (a-z, 0-9, -)")
    if not instruction:
        raise ValueError("no instruction")
    ctid = int(p.get("ctid", 150))
    port = int(p.get("port", 8150))
    work = tempfile.mkdtemp(dir=ctx.get("work_root", "/tmp"))
    try:
        code = _strip_fences(_llm(ctx, _SUP_SYS,
                                  f"SUPERVISOR NAME: {name}\nWHAT ITS AGENTS SHOULD DO: {instruction}\n\n"
                                  f"Write handlers.py now (it MUST define the HANDLERS dict):"))
        if "HANDLERS" not in code:
            raise ValueError("generated handlers.py does not define HANDLERS")
        if not code.endswith("\n"):
            code += "\n"
        open(os.path.join(work, "handlers.py"), "w", encoding="utf-8").write(code)
        summary, results = _forge_test(ctx, work, [{"path": "handlers.py"}], None)
        b = code.encode()
        body = {"requester": {"kind": "supervisor", "id": ctx.get("actor_id", "wade")},
                "action": "infra.supervisor", "reason": instruction, "affected": [name],
                "consequences": (f"Creates a NEW gated supervisor '{name}' (CT {ctid}, port {port}) with its "
                                 f"own database + queue, the trusted worker scaffold, and the agents below. "
                                 f"It can REQUEST approvals but cannot change anything without your sign-off."),
                "validation_evidence": [summary],
                "rollback": "If the supervisor does not come up healthy, its container and database are destroyed.",
                "payload": {"name": name, "ctid": ctid, "cores": int(p.get("cores", 1)),
                            "memory_mb": int(p.get("memory_mb", 512)), "disk_gb": int(p.get("disk_gb", 8)),
                            "port": port, "inference_url": ctx.get("inference_url", ""),
                            "code_model": p.get("code_model", ctx.get("code_model", "qwen3:8b")),
                            "handlers_b64": base64.b64encode(b).decode(),
                            "handlers_sha256": hashlib.sha256(b).hexdigest(),
                            "handlers_preview": code[:4000]}}
        ar = httpx.post(f"{ctx['sentinel_url']}/approvals",
                        headers={"Authorization": f"Bearer {ctx['requester_token']}"}, json=body, timeout=20)
        ar.raise_for_status()
        aid = ar.json().get("id")
        return ({"supervisor": name, "ctid": ctid, "port": port, "approval_id": aid},
                {"approval_id": aid, "forge": results}, "awaiting_approval")
    finally:
        shutil.rmtree(work, ignore_errors=True)


HANDLERS = {
    "diagnostics.health": diagnostics_health,
    "dev.implement": dev_implement,
    "infra.migrate": infra_migrate,
    "infra.provision": infra_provision,
    "infra.supervisor": infra_supervisor,
}
