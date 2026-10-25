# Project PAT — Development Checklist

Stable IDs are `PAT-nnn` and are never renumbered. If the previous ~90-item roadmap is supplied,
add it to `docs/CHECKLIST-MIGRATION.md` mapping old IDs → new IDs; do not reuse old numbers here.

Status categories (spec §28): `planned` → `designed` → `implemented` → `tested` → `deployed` → `verified`.
`tested` means automated tests pass *in the dev environment*; `deployed` means installed on the real
server; `verified` means the acceptance test in spec §34 passed on the real server with recorded evidence.
Nothing below is marked beyond `tested` until it has run on Taylor's hardware.

Evidence column links to a test file, a CI run, or a file under `evidence/`.

## Phase 1 — Fresh Proxmox foundation

| ID | Task | Pri | Deps | Status | Owner | Completion criteria | Evidence |
|---|---|---|---|---|---|---|---|
| PAT-001 | Requirements review, constraints, ADRs | P0 | – | designed | Claude | ADR-0001..0009 written; ARCHITECTURE.md | docs/adr/ |
| PAT-002 | BIOS/firmware checklist for Z890-ACE SE | P0 | – | designed | Claude | VT-x, VT-d, Above-4G, ReBAR, SecureBoot off, XMP, iGPU enabled, fan curves | docs/PHASE1-PROXMOX.md §1 |
| PAT-003 | Proxmox VE 9.x install procedure (ZFS) | P0 | 002 | designed | Claude+Taylor | Host boots, SSH key login works, ZFS rpool healthy | docs/PHASE1-PROXMOX.md §2 |
| PAT-004 | Pre-wipe inventory & explicit confirmation gate | P0 | – | implemented | Claude | `patctl preflight` lists what a reinstall destroys; install refuses without `--i-understand` | infra/preflight, tests/test_preflight.py |
| PAT-005 | Post-install hardening script | P0 | 003 | implemented | Claude | repos fixed, updates, unattended security updates, sshd hardened w/o dropping session, fail2ban, pve firewall default-deny inbound except LAN/tailnet | infra/proxmox/10-post-install.sh |
| PAT-006 | Tailscale on host | P0 | 005 | implemented | Claude | host reachable over tailnet; 8006 not public | infra/proxmox/15-tailscale.sh |
| PAT-007 | NVIDIA host driver (DKMS) + kernel hold | P0 | 005 | implemented | Claude | `nvidia-smi` on host shows 4070 Ti SUPER; survives reboot | infra/proxmox/20-nvidia-host.sh |
| PAT-008 | Network bridges vmbr0/vmbr1 + LAB NAT + firewall | P0 | 005 | implemented | Claude | forge can reach internet, cannot reach LAN | infra/proxmox/40-network.sh |
| PAT-009 | ZFS ARC cap, local snapshot timers | P1 | 003 | implemented | Claude | ARC ≤ 8 GB; hourly snapshots present | infra/proxmox/60-zfs-snapshots.sh |
| PAT-010 | Host monitoring exporters (node, nvidia, smartctl, lm-sensors) | P1 | 007 | planned | Claude | metrics scrapeable; unavailable sensors reported as absent | – |
| PAT-011 | Read-only preflight utility | P0 | – | tested | Claude | JSON+Markdown report of CPU/RAM/disk/GPU/PVE/network; no writes | infra/preflight, tests/test_preflight.py |
| PAT-012 | `patctl` installer framework (plan/dry-run/install/verify/status/resume/repair/rollback/uninstall) | P0 | 011 | tested | Claude | idempotent step engine with persisted state; destructive steps gated | infra/installer, tests/test_installer.py |
| PAT-013 | Phase 1 acceptance: CPU/RAM/storage/network/GPU/remote/recovery/security validated on real host | P0 | 003–009 | planned | Taylor+Claude | `patctl verify --phase 1` green on the server | – |

## Phase 2 — Core AI infrastructure

| ID | Task | Pri | Deps | Status | Owner | Completion criteria | Evidence |
|---|---|---|---|---|---|---|---|
| PAT-020 | Guest definitions (TOML) + creation steps | P0 | 012 | implemented | Claude | CT 100–106, VM 110 created idempotently | infra/guests/, installer steps |
| PAT-021 | infra-core: Postgres 17 + pgvector, roles per domain | P0 | 020 | designed | Claude | roles cannot cross-read; backups dump cleanly | db/ |
| PAT-022 | DB schema v1 (conversations, memory, projects, tasks, jobs, approvals, audit) | P0 | 021 | implemented | Claude | migration applies on empty DB; re-run is a no-op | db/migrations/0001_init.sql, db/migrate.py |
| PAT-023 | inference: Ollama in CT 101 with GPU, model manifest | P0 | 007,020 | designed | Claude | GPU-accelerated completion measured; VRAM policy applied | services/inference/ |
| PAT-024 | Inference priority shim (foreground/background lanes) | P1 | 023 | planned | Claude | background request waits while foreground in flight (test) | – |
| PAT-025 | Shared schemas: events, tasks, approvals, status reports | P0 | – | tested | Claude | pydantic models + JSON schema export | services/shared, tests/test_schemas.py |
| PAT-026 | Sentinel: e-stop (file+DB), approval issuance/verification, audit chain | P0 | 022,025 | tested | Claude | tokens expire, single-use, signature-checked; chain verify detects tampering | services/sentinel, tests/test_sentinel.py |
| PAT-027 | Sentinel: scoped Proxmox API token + guest power control | P0 | 026 | designed | Claude | token can only manage pool `autonomous`; stop of CT 103/VM 110 works | – |
| PAT-028 | Identity/auth for Pat (single admin, passkey or password+TOTP, revocable sessions) | P0 | 022 | planned | Claude | – | – |
| PAT-029 | Basic monitoring stack in CT 105 | P1 | 010,020 | planned | Claude | dashboards for host+guests; alerts for temp/disk/OOM | – |
| PAT-030 | Notifications via ntfy (temporary) | P1 | 020 | planned | Claude | iPhone receives test alert | – |
| PAT-031 | Secrets generation & distribution by installer | P0 | 012 | designed | Claude | no secret in repo; files 0600 | – |
| PAT-034 | Model benchmark harness on real GPU | P1 | 023 | planned | Claude | tokens/s, VRAM, latency recorded per candidate | – |

## Phase 3 — Pat foundation
PAT-040 Pat API skeleton (FastAPI) · PAT-041 chat via router · PAT-042 persistent history + search ·
PAT-043 automatic memory extraction with provenance · PAT-044 memory review/correct/delete UI ·
PAT-045 projects + association · PAT-046 approval inbox · PAT-047 web app (PWA, iPhone-first) ·
PAT-048 infrastructure map v1 (real data) · PAT-049 file attachments · PAT-050 Image Prompt Creator.
All `planned`.

## Phase 4 — Wade autonomous development
PAT-060 job queue + worker w/ checkpoints · PAT-061 forge VM provisioning + podman · PAT-062 plan→code→test loop ·
PAT-063 independent validator · PAT-064 deploy controller (Sentinel) + rollback · PAT-065 three-consecutive-tasks
acceptance test with evidence · PAT-066 bounded retries / reassessment after 3 failures · PAT-067 external AI
escalation with budget meter (disabled by default). All `planned`.

## Phase 5–10
PAT-070 supervisor framework + synthetic supervisor · PAT-071 native-app gateway decision · PAT-072 voice ·
PAT-080 Shuri · PAT-081 Monk · PAT-082 Eugene · PAT-083 HAL · PAT-090 AdGuard · PAT-091 Jellyfin (iGPU) ·
PAT-092 Frigate · PAT-093 Pterodactyl · PAT-094 Home Assistant · PAT-100 family accounts. All `planned`.

## Progress summary (computed by `make progress`)
See `docs/progress.json` — generated, do not edit by hand.
