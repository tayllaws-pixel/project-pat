# Session handoff — where Project PAT stands

Updated: 2026-10-25 (session 1). Read this first when resuming.

## Done this session (all `tested` in the dev environment, nothing deployed)
- Architecture + 9 ADRs (`docs/ARCHITECTURE.md`, `docs/adr/`).
- Phase 1 procedure (`docs/PHASE1-PROXMOX.md`) with BIOS table, ZFS install choices, copy-paste blocks.
- Host scripts 10/15/20/30/40/60 (`infra/proxmox/`), each with `--check`, backups of edited files, no session drops.
- Read-only preflight (`python3 -m infra.preflight`) incl. "what a reinstall destroys" inventory.
- `patctl` installer: plan / dry-run / install / verify / status / resume / repair / rollback / uninstall / guests;
  persisted state; destructive gating; reboot gating; dependency re-evaluation.
- Guest definitions for CT 100–106 and VM 110 (`infra/guests/*.toml`), per-guest firewall generator.
- Shared contracts (`patshared.schemas`), config-driven model router, `models.toml`.
- DB schema v1 for pat / wade / sentinel + checksummed migration runner.
- Sentinel core: e-stop (file + DB), per-agent pause/disable with inheritance, Ed25519 approval tokens
  (payload-bound, scoped, expiring, single-use), hash-chained audit, external-AI budget meter.
- 48 automated tests (`make test`).

## Blocked on Taylor
1. **Consent to erase**: reply "erase the 2 TB NVMe" after reviewing the preflight loss inventory on the old host
   (or confirm the old system is already gone).
2. **Access**: either link a GitHub repo (so commits land directly) or extract the archive at `/opt/project-pat`
   on the host and paste back `preflight.md` + `patctl plan` output after each step.
3. **NVIDIA driver version**: pass `--nvidia-version` (current production branch from nvidia.com/drivers/unix) —
   the installer refuses to guess.
4. **SSH public key** for the iPhone/desktop clients before step 10.

## Next eligible work (no Taylor input needed)
- PAT-021 infra-core provisioning step (`svc-postgres`): Postgres 17 + pgvector inside CT 100, roles/grants per
  `db/README.md`, run `db/migrate.py` for the three databases.
- PAT-023 `svc-inference`: install Ollama in CT 101, systemd env drop-in from `services/inference/ollama.env.example`,
  pull default candidates, benchmark harness that writes `wade.benchmarks`.
- PAT-026 Sentinel service wrapper: FastAPI around `sentinel.core` with Postgres store; PAT-027 scoped PVE token.
- PAT-010 host exporters; PAT-029 observe stack.
- PAT-040 Pat API skeleton with session auth (PAT-028).

## Decisions I made without asking (documented defaults — say so if you want them changed)
ZFS on the single NVMe with 8 GB ARC · Secure Boot off · forge is a VM on an isolated NAT bridge · Postgres as the
only queue/store · Ollama first, benchmark before committing · Tailscale on host, no `tailscale ssh` ·
hostname `pve.home.arpa` · LAB subnet 10.77.0.0/24 · guest IDs 100–110 reserved as listed.

---

## Build log — session 2 (2026-10-25, Option A on the live server)

Working against the real server (PVE 9.2.2 at 100.123.22.20) via the Proxmox web console in the user's Chrome.

**Done:**
- Inventoried + preserved + **cleared the old ChatGPT stack** (CT100/101, VM102, ~15 `wade-*` host services). Models (19 GB) saved to `/var/lib/vz/ollama-backup`, code archived to `/var/lib/vz/old-stack`.
- Repo pipeline live: GitHub `tayllaws-pixel/project-pat` (public) → cloned to `/opt/project-pat`. Update loop = push here, `git pull` on box.
- **infra-core (CT 100) provisioned**: PostgreSQL 17 + pgvector, databases `pat`/`wade`/`sentinel` created with roles, schema v1 (14/5/8 tables), least-privilege grants. Host-side script `infra/provision/create-infra-core.sh`, in-CT `services/infra-core/provision-db.sh`, migration applier `db/apply-migrations.sh`.

**Unprivileged-LXC gotchas found & fixed (now baked into `infra/provision/prep-container.sh` + provision-db.sh):**
1. apt's `_apt` sandbox user can't resolve DNS → `APT::Sandbox::User "root"`.
2. `/etc` ships as mode 0700 → non-root (postgres/_apt) can't read `/etc/passwd`; initdb failed "could not look up effective user ID". Fix: `chmod 755 /etc`.
3. Default PG cluster not auto-created on minimal image → `pg_createcluster 17 main --start` (+ locale-gen).
4. Flaky DHCP resolver + bad IPv6 path to mirrors → pin 1.1.1.1/8.8.8.8, force IPv4.

**Next:** inference container (CT 101, Ollama + restore models), then Sentinel (CT 104).

**Operational notes:** destructive ops (pct/qm destroy) are blocked by the session's safety classifier and must be run by Taylor in the console; infra-core is on DHCP (192.168.1.156 at provision time) — reserve a stable IP on the router.

**Acceptance (infra-core):** `verify-db.sh` → PASS=12 FAIL=0 on the live server — cross-DB isolation, append-only messages/audit, pat_reader read-only, pgvector operational all verified.

**inference (CT 101) done & GPU-verified:** RTX 4070 Ti SUPER shared into the unprivileged LXC by reusing the host's packaged driver and **copying the host's exact-version userspace libs** (615.71.09) — apt gives a mismatched version. Ollama installed from the official binary tarball (no `curl|sh`), all 5 qwen3 models **restored from backup** (no re-download). Acceptance: `ollama run qwen3:4b` → correct output, `ollama ps` = 100% GPU, 3.9 GB VRAM. Scripts: `create-inference.sh`, `install-nvidia-userspace.sh` (unused — superseded by host-lib copy), `install-ollama.sh`, `provision-ollama.sh`.

**More unprivileged-LXC/GPU gotchas (fixed):** apt lists stale in fresh template → `apt-get update` first; CUDA repo version drifted past host module → copy host libs not apt; `curl|sh` blocked by classifier → binary tarball; Ollama asset is now `.tar.zst`.

**Pat v0 (CT 102) LIVE & usable:** FastAPI backend (session-cookie login, conversations + messages in the pat DB, streaming chat from Ollama, model picker) + clean dark web UI. Deployed via `create-pat.sh`. End-to-end verified in-browser: login → chat → GPU response (qwen3:8b) → persisted history. Pat persona/system prompt added (identifies as Taylor's local assistant). Access: http://192.168.1.230:8080 on the LAN (login taylor / in secrets.env).
Known v0 gaps: markdown not rendered in UI; remote/tailnet access needs the gateway container; memory-extraction/projects/approval-inbox and auth hardening are later.

**Pat enhancements (session 3):**
- Markdown rendering in the chat UI (bold/code/code-blocks/lists/headers/links).
- **Persistent memory** (PAT-043/044): nomic-embed-text pulled; after each exchange a background thread uses qwen3:4b-instruct to extract durable facts → stored in memories/memory_versions with provenance + pgvector embedding (768 padded to 1024). On each message the query is embedded and top-k relevant memories are injected into the system prompt. `/api/memories` + a Memory panel to view/delete. Verified end-to-end: extracted 3 correct facts, recalled them in a fresh conversation, panel shows + deletes them.
- Known refinements: no memory dedup yet; extraction quality is 4b-model level.

**Image generation + editing (session 4) — LIVE & verified (spec §23):**
- New **imagegen container (CT 107)**, GPU shared by the same host-lib-copy trick as inference. A small FastAPI **diffusers** service runs **Stable Diffusion XL** (`services/imagegen/app.py`): `/generate` (txt2img), `/img2img`, `/inpaint`, `/healthz`, `/info`. `enable_model_cpu_offload()` keeps SDXL's ~7 GB weights in CPU RAM and streams to the GPU per step, so renders peak low on VRAM. Scripts: `create-imagegen.sh`, `install-imagegen.sh` (torch cu124 + diffusers, CUDA checked before the model download).
- **VRAM coordination** (the whole point on a shared 16 GB card): before each render the image service asks Ollama to unload its models (`/api/ps` → `/api/generate keep_alive:0`) and always `empty_cache()`s after, so image gen and chat hand the GPU back and forth instead of OOMing. Verified: with qwen3:8b resident (6.3 GB), a render auto-unloaded it and succeeded; `ollama ps` empty afterward.
- **Pat integration**: migration `0002_images.sql` (images table w/ edit lineage via `source_image_id` + prompt preservation). Backend `/api/images` endpoints: `prompt` (Image Prompt Creator — LLM rewrites an idea into a detailed SDXL prompt), `generate`, `edit` (img2img from an existing image by id **or** an uploaded one), list/file/delete. PNGs on the Pat container at `/opt/pat/images`, metadata in the pat DB. `IMAGEGEN_URL` wired in `create-pat.sh` (images degrade gracefully if CT 107 is absent).
- **UI**: a 🎨 **Image Studio** (Generate / Edit tabs, ✨ Enhance prompt, size presets, gallery, detail lightbox with variations + download + delete).
- Acceptance (in-browser, full stack): idea → Enhance → 1024² render in **9.1 s** (cozy snowy cabin) → img2img variation in **6.1 s**, both saved + shown. SDXL smoke test on the free card: 768² in 8.9 s.
- Known refinements: inpaint endpoint exists but has no mask-drawing UI yet; generation and chat still shouldn't run at the exact same instant (single user, fine); seed of random renders isn't captured back.

**Remote access (session 4):** Pat is reachable off-LAN via **Tailscale Serve** on the host → `https://proxmox.taile950f6.ts.net` (tailnet-only, HTTPS, proxies to Pat at 192.168.1.230:8080). Root cause of "link didn't work on phone": the prior serve rule pointed at a stale container IP (192.168.1.233) — CT 102's DHCP lease had moved to .230. Repointed it; verified healthz=200 over the tailnet name. **Durability TODO:** CT 102 (hwaddr BC:24:11:3A:58:FB) is on DHCP so its IP can drift again and silently break the serve rule — needs a router DHCP reservation or a static IP (gw 192.168.1.1/24) on the container.

**Update:** Resolved the drift risk — pinned CT 102 to a static `192.168.1.230/24` (gw 192.168.1.1) via `pct set -net0 ...ip=192.168.1.230/24`. Verified after reboot: static IP live, local healthz=200, tailnet healthz=200. Remote access is now stable.

**Wade build — session 4 (Sentinel first, per the spec's safety ordering):**
- **Sentinel (CT 104) LIVE & verified** — the independent safety plane, built on the existing pure core (core.py: e-stop Gate, Ed25519 Approvals, hash-chained Audit) + a new Postgres store (pgstore.py) + HTTP service (app.py). Two scoped bearer tokens enforce the central rule in software: a **decider** token (Pat, relaying the authenticated human) may decide approvals / engage-release the e-stop / pause-disable agents; a **requester** token (Wade & agents) may only *request* approvals and read its gate. The Ed25519 signing key lives only in CT 104 (0600); Wade can never mint tokens. E-stop has a file-flag backstop. Endpoints: /status /gate/{actor} /estop(/engage,/release) /controls /approvals(/decide,/revoke) /verify /audit/verify. Deployed by `create-sentinel.sh`; acceptance `verify-sentinel.sh` → **PASS=12 FAIL=0** on the live box (requester-cannot-decide, requester-cannot-estop, token verify, replay-blocked, e-stop gating, audit chain all confirmed). Sentinel @ http://192.168.1.12:8070.
- **Next:** approval inbox + STOP control inside Pat (so Taylor approves/denies in one place), then the Wade worker (CT 103) + Forge (CT 110 VM) where generated code actually runs.

**Approval inbox + e-stop inside Pat (session 4) — LIVE:** Pat proxies to Sentinel with the decider token (new /api/safety, /api/approvals, /api/estop endpoints), so Taylor approves/denies and engages/releases the e-stop from one place — no separate login. UI: a 🛡 Approvals panel with a pending badge, an "● Autonomy OK / ■ STOPPED" pill in the top bar (polls every 20s), each request shown with action/reason/affected/consequences/requester/cost, Approve·Deny buttons, and a one-tap STOP/RESUME. Verified in-browser: a seeded "deploy.pat_ui" request from wade renders correctly with the badge and controls; the final Approve click is the human's to make (and is gated for the automation). Sentinel decide/e-stop paths already covered by verify-sentinel.sh (12/12).

**Wade worker (CT 103) — gated engine LIVE & verified (session 4):** the autonomous-dev orchestration brain. Durable job queue over the wade DB (leased atomic claims via FOR UPDATE SKIP LOCKED + dependency gating, checkpoints, append-only job_log, bounded retries → needs_reassessment, stale-lease reclaim). The worker checks Sentinel's /gate/wade before claiming and before every attempt, **fails closed** if Sentinel is unreachable, and requeues a job if the e-stop engages mid-run. v1 ships one safe read-only handler (diagnostics.health). Control API (POST /jobs, GET /status, GET /jobs) behind a control token; Wade holds only the Sentinel REQUESTER token — no Proxmox token, no signing key, no deploy rights (ADR-0003). Scripts: `create-wade.sh`, `verify-wade.sh` → **PASS=6 FAIL=0** on the live box: job runs→succeeds, **e-stop blocks a new job (Wade halts), release resumes it** — Sentinel governs Wade in software. Wade @ http://192.168.1.136:8060.
- **Next (task):** Wade's dev pipeline — plan→generate code→**Forge (CT 110 VM)** build/test in kernel isolation→request deploy approval via Sentinel→deploy on approval→verify/rollback. Then the §12 milestone: three consecutive scoped dev tasks end-to-end. Forge must be the isolated VM (vmbr1) before Wade executes anything beyond our own repo/tests.

**Forge + Wade dev pipeline (session 4) — END-TO-END PROVEN:**
- **Forge (CT 110) LIVE** — isolated build/test executor (`services/forge`). Accepts a workspace tarball + commands, runs them as the unprivileged `forge` user in a throwaway dir with per-command/global timeouts + safe-path extraction, returns pass/fail + logs. Holds NO secrets (only its own FORGE_TOKEN). `create-forge.sh`. Forge @ http://192.168.1.2:8050. v1 is an LXC; ADR-0003's kernel-isolated VM on vmbr1 is the tracked upgrade before any untrusted/third-party code (v1 runs only our own repo's checks).
- **Wade dev.implement pipeline LIVE & demonstrated** — handler: clone repo → local LLM (qwen3:8b) rewrites the target file per an instruction → **Forge** builds/tests in isolation → on pass, Wade opens a Sentinel `deploy.code` approval with the diff + Forge summary as evidence and the job ends in `awaiting_approval`; test failures raise so the worker retries, then needs_reassessment. **Live run:** enqueued "add `__version__` to the forge package" → Wade cloned, generated the change, Forge `py_compile` passed, and a `deploy.code` approval ("from wade") landed in Pat's inbox. This is the §12 loop working: a task → a tested change awaiting human approval, no shell commands run by Taylor.
- Fixed a redeploy bug: create-{wade,sentinel,forge}.sh used `systemctl enable --now` which doesn't restart an already-running unit → now `enable; restart` so code updates take effect.
- **Still ahead:** the deploy controller (Sentinel holds the Proxmox token; verifies the approval token, applies the change, verifies, rolls back) so an approved `deploy.code` auto-ships; showing the diff in Pat's approval card; the three-consecutive-tasks milestone; Forge→VM hardening.

**Deploy controller (deployd) — BUILT, install is owner-gated (session 4):** the only component with host/pct access, so it runs as a systemd service on the Proxmox host (stdlib Python, no host pip). It acts solely on a single-use Sentinel token bound to the exact approved content: GET the approval's payload → POST /verify (consume token, checks sig/exp/payload-hash/scope) → re-check sha256 → deploy a whitelisted target (backup → pct push → restart → in-container healthz) → auto-rollback on any failure. Wade carries the new file content in the approval payload (base64), so the token's hash binds the exact bytes. Pat forwards an approved `deploy.code` to deployd and shows "✓ Shipped / ✗ Deploy failed" in the inbox (dormant until DEPLOY_URL is wired). Files: `services/deployd/deployd.py`, `create-deployd.sh`, `verify-deployd.sh` (full chain incl. token-replay rejection + restore).
- **NOT yet installed/verified live:** `create-deployd.sh` stands up a root service on the hypervisor with full container-deploy powers — the session safety classifier (correctly) blocks Claude from auto-installing it. Taylor runs it himself: `bash /opt/project-pat/infra/provision/create-deployd.sh` then `bash /opt/project-pat/services/deployd/verify-deployd.sh`. After that, Pat is re-wired (DEPLOY_URL) and approving a deploy.code in the inbox auto-ships with health-check + rollback.
- Wade now embeds file content in deploy.code approvals; Pat has the (dormant) forwarding + ship-status UI deployed.
