#!/usr/bin/env python3
"""Deploy controller (deployd) — the ONLY component that applies changes to running services.

Runs on the Proxmox host (it needs `pct`). It acts only on a valid, single-use Sentinel approval token
that is cryptographically bound to the exact file content being deployed:

  1. fetch the approval's exact payload from Sentinel,
  2. verify + consume the token with Sentinel (binds action + payload hash + scope, single-use),
  3. re-check the content against the approved sha256,
  4. deploy ONLY whitelisted target files: back up the live file, push the new one, restart the service,
  5. health-check the service; on any failure, restore the backup and restart (auto-rollback).

Wade never has access to this. The human triggers a deploy by approving a deploy.code request in Pat,
which forwards the resulting token here. Stdlib only — no pip on the host.
"""
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SENTINEL_URL   = os.environ.get("SENTINEL_URL", "").rstrip("/")
SENTINEL_TOKEN = os.environ.get("SENTINEL_REQUESTER_TOKEN", "")   # any valid token may call /verify + GET approval
CONTROL_TOKEN  = os.environ.get("DEPLOY_CONTROLLER_TOKEN", "")
BIND           = os.environ.get("DEPLOY_BIND", "0.0.0.0")
PORT           = int(os.environ.get("DEPLOY_PORT", "8040"))

# Whitelist of deployable files: repo path -> (ctid, container path, service unit, in-container healthz URL).
DEPLOY_MAP = {
    "services/pat/pat/app.py":          (102, "/opt/pat/pat/app.py",          "pat",   "http://127.0.0.1:8080/healthz"),
    "services/pat/web/index.html":      (102, "/opt/pat/web/index.html",      "pat",   "http://127.0.0.1:8080/healthz"),
    "services/forge/forge/__init__.py": (110, "/opt/forge/forge/__init__.py", "forge", "http://127.0.0.1:8050/healthz"),
    "services/forge/forge/app.py":      (110, "/opt/forge/forge/app.py",      "forge", "http://127.0.0.1:8050/healthz"),
    "services/wade/wade/handlers.py":   (103, "/opt/wade/wade/handlers.py",   "wade",  "http://127.0.0.1:8060/healthz"),
    "services/wade/wade/worker.py":     (103, "/opt/wade/wade/worker.py",     "wade",  "http://127.0.0.1:8060/healthz"),
    "services/wade/wade/db.py":         (103, "/opt/wade/wade/db.py",         "wade",  "http://127.0.0.1:8060/healthz"),
    "services/wade/wade/app.py":        (103, "/opt/wade/wade/app.py",        "wade",  "http://127.0.0.1:8060/healthz"),
}


def _sentinel(method, path, payload=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{SENTINEL_URL}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {SENTINEL_TOKEN}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
    return json.loads(b) if b else {}


def _pct(args, timeout=90):
    return subprocess.run(["pct"] + args, capture_output=True, timeout=timeout)


def _read_file(ctid, path):
    p = _pct(["exec", str(ctid), "--", "cat", path])
    return p.stdout if p.returncode == 0 else None


def _write_file(ctid, path, content):
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(content)
        tmp = tf.name
    try:
        p = _pct(["push", str(ctid), tmp, path])
        return p.returncode == 0, (p.stderr.decode()[:300] if p.stderr else "")
    finally:
        os.unlink(tmp)


def _restart(ctid, unit):
    p = _pct(["exec", str(ctid), "--", "systemctl", "restart", unit])
    return p.returncode == 0, (p.stderr.decode()[:300] if p.stderr else "")


def _healthz(ctid, url, tries=6):
    for _ in range(tries):
        p = _pct(["exec", str(ctid), "--", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url], timeout=15)
        if p.returncode == 0 and p.stdout.decode().strip() == "200":
            return True
        time.sleep(2)
    return False


def _delete_file(ctid, path):
    return _pct(["exec", str(ctid), "--", "rm", "-f", path]).returncode == 0


def _ensure_parent(ctid, path):
    d = os.path.dirname(path)
    if d:
        _pct(["exec", str(ctid), "--", "mkdir", "-p", d])


def _rollback(pushed):
    """Undo each file already pushed: restore its prior bytes, or delete it if it was newly created.
    Returns True only if every file was restored cleanly."""
    ok = True
    for x in pushed:
        if x.get("backup") is None:                     # file did not exist before — remove it
            ok = _delete_file(x["ctid"], x["cpath"]) and ok
        else:
            w, _ = _write_file(x["ctid"], x["cpath"], x["backup"])
            ok = w and ok
    return ok


def do_deploy(approval_id, token):
    appr = _sentinel("GET", f"/approvals/{approval_id}")
    if appr.get("action") != "deploy.code":
        return 400, {"ok": False, "error": f"approval action is {appr.get('action')}, not deploy.code"}
    payload = appr.get("payload") or {}

    # Normalise to a list of files. Supports the multi-file payload (files[]) and the legacy single-file one.
    files = payload.get("files")
    if not files and payload.get("target_file"):
        files = [{"path": payload["target_file"], "new_sha256": payload.get("new_sha256"),
                  "new_content_b64": payload.get("new_content_b64")}]
    if not files:
        return 400, {"ok": False, "error": "approval payload carries no files to deploy"}

    # Validate EVERY file before consuming the token or touching anything (fail closed, all-or-nothing).
    plan = []
    for f in files:
        path = f.get("path")
        if path not in DEPLOY_MAP:
            return 400, {"ok": False, "error": f"target {path!r} is not a deployable file"}
        cb64 = f.get("new_content_b64")
        if not cb64:
            return 400, {"ok": False, "error": f"no content for {path}"}
        new_bytes = base64.b64decode(cb64)
        if hashlib.sha256(new_bytes).hexdigest() != f.get("new_sha256"):
            return 400, {"ok": False, "error": f"content sha256 does not match the approved hash for {path}"}
        ctid, cpath, unit, health = DEPLOY_MAP[path]
        plan.append({"path": path, "bytes": new_bytes, "ctid": ctid, "cpath": cpath,
                     "unit": unit, "health": health, "is_new": bool(f.get("is_new"))})

    # Verify + consume the single-use token against the EXACT approved payload (binds all files at once).
    try:
        _sentinel("POST", "/verify", {"token": token, "action": "deploy.code", "payload": payload,
                                      "executor": "deploy-controller",
                                      "resource": ",".join(x["path"] for x in plan)})
    except urllib.error.HTTPError as e:
        return 403, {"ok": False, "error": f"token rejected: {e.read().decode()[:200]}"}

    # Back up every file (None => it did not exist and is being created).
    for x in plan:
        x["backup"] = _read_file(x["ctid"], x["cpath"])

    # Push every file; roll back everything already pushed if any push fails.
    pushed = []
    for x in plan:
        if x["backup"] is None:
            _ensure_parent(x["ctid"], x["cpath"])
        ok, err = _write_file(x["ctid"], x["cpath"], x["bytes"])
        if not ok:
            _rollback(pushed)
            return 500, {"ok": False, "error": f"push failed for {x['path']}: {err}; rolled_back=True"}
        pushed.append(x)

    # Restart each affected service once, then health-check each.
    services = []
    for x in plan:
        key = (x["ctid"], x["unit"], x["health"])
        if key not in services:
            services.append(key)
    restart_errs = []
    for (ctid, unit, _h) in services:
        rok, rerr = _restart(ctid, unit)
        if not rok:
            restart_errs.append(f"{unit}:{rerr}")
    healthy = all(_healthz(ctid, h) for (ctid, _u, h) in services)

    if not restart_errs and healthy:
        return 200, {"ok": True, "deployed": [x["path"] for x in plan],
                     "services": sorted({x["unit"] for x in plan}), "approval_id": approval_id}

    # Any failure: roll back all files and restart the affected services.
    rolled = _rollback(pushed)
    for (ctid, unit, _h) in services:
        _restart(ctid, unit)
    return 500, {"ok": False,
                 "error": f"deploy unhealthy (restart_errs={restart_errs} healthy={healthy}); rolled_back={rolled}",
                 "files": [x["path"] for x in plan]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/deploy":
            return self._send(404, {"error": "not found"})
        if not CONTROL_TOKEN or self.headers.get("Authorization", "") != f"Bearer {CONTROL_TOKEN}":
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        aid, tok = body.get("approval_id"), body.get("token")
        if not aid or not tok:
            return self._send(400, {"error": "approval_id and token are required"})
        try:
            code, obj = do_deploy(aid, tok)
            self._send(code, obj)
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)[:300]})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
