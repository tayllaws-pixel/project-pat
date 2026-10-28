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
import re
import shlex
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
    "services/forge/forge/version.py":  (110, "/opt/forge/forge/version.py",  "forge", "http://127.0.0.1:8050/healthz"),
    "services/forge/forge/app.py":      (110, "/opt/forge/forge/app.py",      "forge", "http://127.0.0.1:8050/healthz"),
    "services/wade/wade/handlers.py":   (103, "/opt/wade/wade/handlers.py",   "wade",  "http://127.0.0.1:8060/healthz"),
    "services/wade/wade/worker.py":     (103, "/opt/wade/wade/worker.py",     "wade",  "http://127.0.0.1:8060/healthz"),
    "services/wade/wade/db.py":         (103, "/opt/wade/wade/db.py",         "wade",  "http://127.0.0.1:8060/healthz"),
    "services/wade/wade/app.py":        (103, "/opt/wade/wade/app.py",        "wade",  "http://127.0.0.1:8060/healthz"),
}

# ---- rung 6: infrastructure-change limits (hard caps the executor enforces, whatever is requested) ----
INFRA_CT       = 100                     # infra-core container runs postgres
MIGRATE_DBS    = {"pat", "wade", "sentinel"}
PROV_STORAGE   = os.environ.get("PROV_STORAGE", "local-lvm")
PROV_CTID_MIN, PROV_CTID_MAX = 120, 199  # reserved range for Wade-provisioned services
PROV_PORT_MIN, PROV_PORT_MAX = 8100, 8199
PROV_CORES_MAX = 2
PROV_MEM_MIN, PROV_MEM_MAX = 128, 2048   # MB
PROV_DISK_MIN, PROV_DISK_MAX = 2, 16     # GB
PROV_FILES_MAX = 30
PROV_FILES_BYTES_MAX = 2_000_000
PROV_DEPS_MAX = 20
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_DEP_RE  = re.compile(r"^[A-Za-z0-9_.\-\[\]<>=,~!]+$")
_MIGNAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")
# Statements refused unless the approval explicitly set allow_destructive.
_DESTRUCTIVE = re.compile(
    r"\b(drop\s+(table|database|schema|role|user|owned|index)|truncate|delete\s+from|"
    r"alter\s+table\s+\S+\s+drop\s+(column|constraint)|grant\s+all|revoke)\b", re.I)


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


def _verify_token(token, action, payload, resource):
    """Verify + consume the single-use token against the EXACT approved payload. Raises on rejection."""
    _sentinel("POST", "/verify", {"token": token, "action": action, "payload": payload,
                                  "executor": "deploy-controller", "resource": resource})


def dispatch(approval_id, token):
    """Fetch the approval and route to the right executor by action."""
    appr = _sentinel("GET", f"/approvals/{approval_id}")
    action = appr.get("action")
    if action == "deploy.code":
        return do_deploy(appr, token)
    if action == "infra.migrate":
        return do_migrate(appr, token)
    if action == "infra.provision":
        return do_provision(appr, token)
    return 400, {"ok": False, "error": f"action {action!r} is not something deployd applies"}


def do_deploy(appr, token):
    approval_id = appr.get("id")
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
        _verify_token(token, "deploy.code", payload, [x["path"] for x in plan])
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


# ---------------------------------------------------------------- rung 6: DB migration
def _pg(db, sql, timeout=60):
    p = _pct(["exec", str(INFRA_CT), "--", "sudo", "-u", "postgres", "psql", "-tA", "-d", db, "-c", sql],
             timeout=timeout)
    return p.stdout.decode() if p.returncode == 0 else ""


def do_migrate(appr, token):
    p = appr.get("payload") or {}
    db = p.get("db")
    name = p.get("migration_name") or ""
    sql = p.get("migration_sql")
    allow_destructive = bool(p.get("allow_destructive"))
    if db not in MIGRATE_DBS:
        return 400, {"ok": False, "error": f"db {db!r} is not a migratable database"}
    if not _MIGNAME_RE.match(name):
        return 400, {"ok": False, "error": f"migration name {name!r} must look like 0004_add_thing.sql"}
    if not sql:
        return 400, {"ok": False, "error": "no migration_sql"}
    if hashlib.sha256(sql.encode()).hexdigest() != p.get("migration_sha256"):
        return 400, {"ok": False, "error": "migration sha256 does not match the approved hash"}
    if _DESTRUCTIVE.search(sql) and not allow_destructive:
        return 400, {"ok": False, "error": "migration contains destructive statements and was not approved as destructive"}
    ver = name[:4]
    try:
        _verify_token(token, "infra.migrate", p, name)
    except urllib.error.HTTPError as e:
        return 403, {"ok": False, "error": f"token rejected: {e.read().decode()[:200]}"}
    if _pg(db, f"SELECT 1 FROM schema_migrations WHERE version='{ver}'").strip() == "1":
        return 400, {"ok": False, "error": f"migration {ver} is already applied to {db}"}
    # apply inside a single transaction (ON_ERROR_STOP + -1): any error rolls the whole file back
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(sql)
        tmp = tf.name
    try:
        dest = f"/tmp/{name}"
        if _pct(["push", str(INFRA_CT), tmp, dest]).returncode != 0:
            return 500, {"ok": False, "error": "could not stage migration into infra-core"}
        r = _pct(["exec", str(INFRA_CT), "--", "sudo", "-u", "postgres", "psql",
                  "-v", "ON_ERROR_STOP=1", "-1", "-d", db, "-f", dest], timeout=120)
        if r.returncode != 0:
            return 500, {"ok": False, "error": f"migration failed (rolled back): {r.stderr.decode()[-400:]}"}
    finally:
        os.unlink(tmp)
    _pg(db, f"INSERT INTO schema_migrations(version, checksum) VALUES ('{ver}','executor-applied') "
            f"ON CONFLICT (version) DO NOTHING")
    # persist the file into the host repo clone so it isn't lost (not auto-committed — see notes)
    try:
        mdir = "/opt/project-pat/db/migrations"
        if os.path.isdir(mdir):
            open(os.path.join(mdir, name), "w").write(sql)
    except Exception:
        pass
    return 200, {"ok": True, "migrated": name, "db": db, "approval_id": appr.get("id")}


# ---------------------------------------------------------------- rung 6: provision a service
def _find_template():
    r = subprocess.run(["bash", "-c",
                        "pveam list local 2>/dev/null | awk '/debian-1[0-9]-standard/{print $1}' | sort | tail -1"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or None


def _validate_provision(p):
    """Return (spec, error). Enforces every hard limit; nothing here trusts the request."""
    name = p.get("name") or ""
    if not _NAME_RE.match(name):
        return None, "service name must be lowercase [a-z][a-z0-9-], 2-31 chars"
    try:
        ctid = int(p.get("ctid")); cores = int(p.get("cores", 1))
        mem = int(p.get("memory_mb", 512)); disk = int(p.get("disk_gb", 8)); port = int(p.get("port"))
    except (TypeError, ValueError):
        return None, "ctid/cores/memory_mb/disk_gb/port must be integers"
    if not PROV_CTID_MIN <= ctid <= PROV_CTID_MAX:
        return None, f"ctid must be in {PROV_CTID_MIN}-{PROV_CTID_MAX} (reserved for provisioned services)"
    if not 1 <= cores <= PROV_CORES_MAX:
        return None, f"cores must be 1-{PROV_CORES_MAX}"
    if not PROV_MEM_MIN <= mem <= PROV_MEM_MAX:
        return None, f"memory_mb must be {PROV_MEM_MIN}-{PROV_MEM_MAX}"
    if not PROV_DISK_MIN <= disk <= PROV_DISK_MAX:
        return None, f"disk_gb must be {PROV_DISK_MIN}-{PROV_DISK_MAX}"
    if not PROV_PORT_MIN <= port <= PROV_PORT_MAX:
        return None, f"port must be {PROV_PORT_MIN}-{PROV_PORT_MAX}"
    deps = p.get("deps") or []
    if not isinstance(deps, list) or len(deps) > PROV_DEPS_MAX or not all(
            isinstance(d, str) and _DEP_RE.match(d) for d in deps):
        return None, "deps must be a short list of plain pip requirement strings"
    files = p.get("files") or []
    if not isinstance(files, list) or not 1 <= len(files) <= PROV_FILES_MAX:
        return None, f"files must be a list of 1-{PROV_FILES_MAX}"
    norm, total = [], 0
    for f in files:
        fp = f.get("path") or ""
        if not fp or fp.startswith("/") or ".." in fp or "\\" in fp:
            return None, f"bad file path {fp!r}"
        cb = f.get("content_b64")
        if not cb:
            return None, f"no content for {fp}"
        b = base64.b64decode(cb)
        if hashlib.sha256(b).hexdigest() != f.get("sha256"):
            return None, f"content sha256 mismatch for {fp}"
        total += len(b)
        norm.append((fp, b))
    if total > PROV_FILES_BYTES_MAX:
        return None, "service files are too large"
    exec_start = p.get("exec_start") or ""
    if any(c in exec_start for c in ";|&`$><\n"):
        return None, "exec_start must not contain shell metacharacters"
    if not re.match(rf"^/opt/{re.escape(name)}-venv/bin/(python3?|uvicorn) ", exec_start):
        return None, "exec_start must run /opt/<name>-venv/bin/python or uvicorn"
    if str(port) not in exec_start:
        return None, "exec_start must bind the approved port"
    healthz = p.get("healthz", "/healthz")
    if not isinstance(healthz, str) or not healthz.startswith("/"):
        return None, "healthz must be a path beginning with /"
    return {"name": name, "ctid": ctid, "cores": cores, "mem": mem, "disk": disk, "port": port,
            "deps": deps, "files": norm, "exec_start": exec_start, "healthz": healthz}, None


def do_provision(appr, token):
    p = appr.get("payload") or {}
    spec, err = _validate_provision(p)
    if err:
        return 400, {"ok": False, "error": err}
    name, ctid, port = spec["name"], spec["ctid"], spec["port"]
    if _pct(["config", str(ctid)]).returncode == 0:
        return 400, {"ok": False, "error": f"CT {ctid} already exists"}
    try:
        _verify_token(token, "infra.provision", p, name)
    except urllib.error.HTTPError as e:
        return 403, {"ok": False, "error": f"token rejected: {e.read().decode()[:200]}"}
    templ = _find_template()
    if not templ:
        return 500, {"ok": False, "error": "no debian template available on host"}

    created = False
    try:
        c = _pct(["create", str(ctid), templ, "--hostname", name, "--cores", str(spec["cores"]),
                  "--memory", str(spec["mem"]), "--swap", "256", "--rootfs", f"{PROV_STORAGE}:{spec['disk']}",
                  "--net0", "name=eth0,bridge=vmbr0,ip=dhcp,firewall=1", "--nameserver", "1.1.1.1 8.8.8.8",
                  "--unprivileged", "1", "--features", "nesting=1", "--onboot", "1", "--start", "1",
                  "--tags", "pat;wade-provisioned", "--description", f"wade-provisioned service: {name}"],
                 timeout=180)
        if c.returncode != 0:
            return 500, {"ok": False, "error": f"pct create failed: {c.stderr.decode()[:300]}"}
        created = True
        for _ in range(30):
            if _pct(["exec", str(ctid), "--", "getent", "hosts", "deb.debian.org"]).returncode == 0:
                break
            time.sleep(2)
        setup = ("export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
                 "apt-get install -y -qq python3 python3-venv curl ca-certificates >/dev/null && "
                 f"python3 -m venv /opt/{name}-venv && /opt/{name}-venv/bin/pip install -q --upgrade pip")
        if _pct(["exec", str(ctid), "--", "bash", "-c", setup], timeout=400).returncode != 0:
            raise RuntimeError("runtime install failed")
        if spec["deps"]:
            dcmd = f"/opt/{name}-venv/bin/pip install -q " + " ".join(shlex.quote(d) for d in spec["deps"])
            if _pct(["exec", str(ctid), "--", "bash", "-c", dcmd], timeout=600).returncode != 0:
                raise RuntimeError("pip dependency install failed")
        _pct(["exec", str(ctid), "--", "mkdir", "-p", f"/opt/{name}"])
        for fp, b in spec["files"]:
            dest = f"/opt/{name}/{fp}"
            d = os.path.dirname(dest)
            if d:
                _pct(["exec", str(ctid), "--", "mkdir", "-p", d])
            ok, e = _write_file(ctid, dest, b)
            if not ok:
                raise RuntimeError(f"failed to write {fp}: {e}")
        # systemd unit — NO EnvironmentFile: a provisioned service gets no secrets/credentials.
        unit = (f"[Unit]\nDescription=wade-provisioned {name}\nAfter=network-online.target\n\n"
                f"[Service]\nWorkingDirectory=/opt/{name}\nExecStart={spec['exec_start']}\n"
                f"Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n")
        okw, e = _write_file(ctid, f"/etc/systemd/system/{name}.service", unit.encode())
        if not okw:
            raise RuntimeError(f"failed to write unit: {e}")
        if _pct(["exec", str(ctid), "--", "bash", "-c",
                 f"systemctl daemon-reload && systemctl enable --now {name}"], timeout=60).returncode != 0:
            raise RuntimeError("service failed to start")
        healthy = False
        for _ in range(10):
            h = _pct(["exec", str(ctid), "--", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                      f"http://127.0.0.1:{port}{spec['healthz']}"], timeout=15)
            if h.returncode == 0 and h.stdout.decode().strip() == "200":
                healthy = True
                break
            time.sleep(2)
        if not healthy:
            raise RuntimeError("service did not become healthy")
        ipp = _pct(["exec", str(ctid), "--", "hostname", "-I"])
        ip = ipp.stdout.decode().split()[0] if ipp.returncode == 0 and ipp.stdout.split() else "?"
        return 200, {"ok": True, "provisioned": name, "ctid": ctid, "url": f"http://{ip}:{port}",
                     "approval_id": appr.get("id")}
    except Exception as e:
        rolled = False
        if created:
            _pct(["stop", str(ctid)], timeout=60)
            rolled = _pct(["destroy", str(ctid)], timeout=120).returncode == 0
        return 500, {"ok": False, "error": f"provision failed: {str(e)[:300]}", "rolled_back": rolled}


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
            code, obj = dispatch(aid, tok)
            self._send(code, obj)
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)[:300]})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
