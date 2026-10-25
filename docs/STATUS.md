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
