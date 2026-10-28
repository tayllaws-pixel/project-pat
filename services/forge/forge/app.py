"""Forge executor — the isolated runner where Wade's proposed changes are built and tested.

Wade sends a workspace (a gzip tarball of the repo tree with its change applied) and a list of shell
commands (the build/test steps). Forge extracts the workspace into a throwaway directory, runs each
command with a timeout, and returns pass/fail plus captured output. It then deletes the workspace.

Isolation model: Forge holds NO secrets — no DB credentials, no Sentinel key, no tokens to other
services — only its own FORGE_TOKEN for inbound auth. It runs as an unprivileged user in its own
container. (v1 is a locked-down LXC; the architecture's target is a kernel-isolated VM on vmbr1 before
Forge is ever asked to run untrusted/third-party code — v1 runs only our own repo's checks.)
"""
from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import time

from fastapi import FastAPI, Header, HTTPException, Request

TOKEN     = os.environ.get("FORGE_TOKEN", "")
WORK_ROOT = os.environ.get("FORGE_WORK", "/var/forge/work")
MAX_CMDS  = int(os.environ.get("FORGE_MAX_CMDS", "25"))
MAX_TOTAL = int(os.environ.get("FORGE_MAX_SECONDS", "600"))

app = FastAPI(title="Forge")


def _auth(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not TOKEN or authorization.split(" ", 1)[1].strip() != TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/run")
async def run(request: Request, authorization: str | None = Header(default=None)):
    _auth(authorization)
    body = await request.json()
    ws = body.get("workspace_b64")
    cmds = body.get("commands") or []
    per_cmd_timeout = min(int(body.get("timeout", 120)), MAX_TOTAL)
    if not ws or not cmds:
        raise HTTPException(status_code=400, detail="workspace_b64 and commands are required")
    if len(cmds) > MAX_CMDS:
        raise HTTPException(status_code=400, detail=f"too many commands (max {MAX_CMDS})")

    os.makedirs(WORK_ROOT, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=WORK_ROOT)
    try:
        try:
            raw = base64.b64decode(ws)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
                for m in tf.getmembers():
                    parts = m.name.split("/")
                    if m.name.startswith("/") or ".." in parts:
                        raise HTTPException(status_code=400, detail=f"unsafe path in workspace: {m.name}")
                tf.extractall(workdir)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad workspace archive: {e}")

        # if the tar had a single top-level dir, run inside it
        entries = [e for e in os.listdir(workdir) if not e.startswith(".")]
        run_dir = workdir
        if len(entries) == 1 and os.path.isdir(os.path.join(workdir, entries[0])):
            run_dir = os.path.join(workdir, entries[0])

        results = []
        ok = True
        budget = MAX_TOTAL
        for cmd in cmds:
            if budget <= 0:
                results.append({"cmd": cmd, "exit": 124, "output": "<global time budget exhausted>", "secs": 0})
                ok = False
                break
            t0 = time.time()
            try:
                p = subprocess.run(cmd, shell=True, cwd=run_dir, capture_output=True, text=True,
                                   timeout=min(per_cmd_timeout, budget),
                                   env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": run_dir,
                                        "LANG": "C.UTF-8"})
                out = (p.stdout + p.stderr)[-6000:]
                code = p.returncode
            except subprocess.TimeoutExpired:
                out, code = "<command timed out>", 124
            secs = round(time.time() - t0, 1)
            budget -= secs
            results.append({"cmd": cmd, "exit": code, "output": out, "secs": secs})
            if code != 0:
                ok = False
                break
        return {"ok": ok, "results": results}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
