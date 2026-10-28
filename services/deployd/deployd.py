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


def do_deploy(approval_id, token):
    appr = _sentinel("GET", f"/approvals/{approval_id}")
    if appr.get("action") != "deploy.code":
        return 400, {"ok": False, "error": f"approval action is {appr.get('action')}, not deploy.code"}
    payload = appr.get("payload") or {}
    target = payload.get("target_file")
    if target not in DEPLOY_MAP:
        return 400, {"ok": False, "error": f"target {target!r} is not a deployable file"}
    content_b64 = payload.get("new_content_b64")
    if not content_b64:
        return 400, {"ok": False, "error": "approval payload carries no content to deploy"}

    # verify + consume the single-use token against the EXACT approved payload
    try:
        _sentinel("POST", "/verify", {"token": token, "action": "deploy.code", "payload": payload,
                                      "executor": "deploy-controller", "resource": target})
    except urllib.error.HTTPError as e:
        return 403, {"ok": False, "error": f"token rejected: {e.read().decode()[:200]}"}

    new_bytes = base64.b64decode(content_b64)
    if hashlib.sha256(new_bytes).hexdigest() != payload.get("new_sha256"):
        return 400, {"ok": False, "error": "content sha256 does not match the approved hash"}

    ctid, cpath, unit, health = DEPLOY_MAP[target]
    old = _read_file(ctid, cpath)                       # backup for rollback
    ok, err = _write_file(ctid, cpath, new_bytes)
    if not ok:
        return 500, {"ok": False, "error": f"push failed: {err}"}
    rok, rerr = _restart(ctid, unit)
    if rok and _healthz(ctid, health):
        return 200, {"ok": True, "deployed": target, "ctid": ctid, "service": unit, "approval_id": approval_id}

    # rollback
    rolled = False
    if old is not None:
        _write_file(ctid, cpath, old)
        _restart(ctid, unit)
        rolled = _healthz(ctid, health)
    return 500, {"ok": False, "error": f"deploy unhealthy (restart_ok={rok} {rerr}); rolled_back={rolled}",
                 "target": target}


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
