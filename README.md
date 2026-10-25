# Project PAT

Locally hosted personal AI ecosystem on a single Proxmox VE host: **Pat** (assistant), **Wade**
(AI systems supervisor with a controlled autonomous development pipeline), **Sentinel** (independent
safety/approval plane), shared inference and data services, and room for future departments.

**State of this repository:** Phase 1–2 foundations. Read `docs/CHECKLIST.md` for what is
designed / implemented / tested / deployed / verified. Nothing has been deployed to the real server yet.

## Layout
```
docs/            SPEC.md (master requirements), ARCHITECTURE.md, adr/, PHASE1-PROXMOX.md, CHECKLIST.md
infra/proxmox/   Host scripts, numbered, idempotent, copy-paste safe over SSH
infra/guests/    Guest definitions (TOML) — CT/VM IDs, caps, networks, devices
infra/preflight/ Read-only hardware/PVE inventory + what-a-reinstall-would-destroy report
infra/installer/ `patctl` — plan / dry-run / install / verify / status / resume / repair / rollback / uninstall
services/shared/ patshared — schemas (events, tasks, approvals), router config loader, queue interface
services/sentinel/ Sentinel core: e-stop, approval tokens, hash-chained audit
services/inference/ Ollama service config, model manifest, priority shim (later)
db/              SQL migrations + migrate.py
tests/           unittest/pytest suites (stdlib runner: `make test`)
```

## Quick start (on the Proxmox host, as root, after Phase 1 §2 of docs/PHASE1-PROXMOX.md)
```bash
apt-get install -y git python3 >/dev/null
git clone <this repo> /opt/project-pat && cd /opt/project-pat
python3 -m infra.preflight            # read-only report → /var/lib/project-pat/preflight.json
python3 -m infra.installer plan       # shows every step and whether it is already satisfied
python3 -m infra.installer install --phase 1
```
`patctl` never performs a destructive step without `--i-understand <step-id>`; `plan` shows which steps are destructive.

## Development
```bash
make test        # stdlib unittest runner (pytest also works)
make lint        # ruff if available, shellcheck if available; otherwise bash -n
make progress    # regenerates docs/progress.json from CHECKLIST.md
```
No secrets are stored in this repository. The installer generates them into `/etc/project-pat/` (0600).
