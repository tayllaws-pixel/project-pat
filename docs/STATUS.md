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
