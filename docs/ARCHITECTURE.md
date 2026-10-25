# Project PAT — System Architecture (v0.1)

Status: **designed**, not deployed. Nothing in this document is a claim that software is installed.
Each decision here has an ADR in `docs/adr/`; the checklist in `docs/CHECKLIST.md` tracks what is actually built, tested and verified.

## 1. Critical dependencies and constraints (Step 1 of the master spec)

| # | Constraint | Consequence for the design |
|---|---|---|
| C1 | One RTX 4070 Ti SUPER, 16 GB VRAM, no MIG/vGPU partitioning on consumer silicon | A single **inference service** owns the GPU for LLM work. Everything else talks to it over HTTP. VRAM budgeting is done by *model-load policy*, not by hardware quotas. |
| C2 | 64 GB RAM total, 24/7 operation | Hard memory caps on every guest; ZFS ARC capped at 8 GB; sum of guest caps ≤ ~48 GB so the host never swaps. |
| C3 | Single 2 TB NVMe (no redundancy) | ZFS on the one disk for snapshots/checksums/compression, but **no** data safety until off-box backups run. Weekly backups to the desktop + local ZFS snapshots (hourly, 48 kept) + daily vzdump of infra-core. A second SSD is the first recommended hardware purchase. |
| C4 | Intel Core Ultra 7 265K (Arrow Lake: 8 P-cores + 12 E-cores, 20 threads, **integrated Xe GPU**) | The iGPU (VA-API/QSV) handles Jellyfin transcoding and Frigate video decode later, keeping the NVIDIA card free for AI. Guests get CPU *limits* and *shares*, not pinning, so the kernel can still use E-cores for background work. |
| C5 | AI-generated code must run but must not be able to touch the host, the LAN, or production data | Wade's build/test sandbox is a **VM** on an isolated NAT bridge, not an LXC. The deploy path goes through a controller Wade cannot modify. |
| C6 | Safety controls must survive Wade being broken or adversarial | **Sentinel** runs in its own container with its own DB credentials and the *only* Proxmox API token that can start/stop guests. Wade's container has no Proxmox token at all. |
| C7 | Proxmox UI must never be public; admin is usually from an iPhone over SSH | Tailscale on the host (phase 1) and a reverse-proxy gateway for Pat (phase 3). Public ports: none by default. All host scripts are copy-paste blocks that never drop the SSH session. |
| C8 | Local-first; $5/month external AI budget, disabled until configured | External providers are a Sentinel-metered capability, off by default, with a hard monthly cap enforced server-side. |
| C9 | Proxmox VE 9.x (Debian 13) ships kernel 6.14+; NVIDIA kernel modules are out-of-tree | Driver installed on the host via NVIDIA's `.run` with DKMS; kernel upgrades are pinned/held and performed by a documented procedure, never unattended. Secure Boot off (ADR-0002). |
| C10 | Everything must be resumable and idempotent from a phone | `patctl` installer keeps a state file; each step declares `check()` and `apply()`; destructive steps require an explicit `--i-understand` token per step. |

## 2. Guest layout (initial, Phases 1–4)

```
Proxmox VE host  (ZFS rpool, kernel 6.x, NVIDIA driver, Tailscale, node_exporter)
│
├─ vmbr0  LAN bridge (192.168.x.0/24 — DHCP reservations on the Netgear RS180)
│   ├─ CT 100  infra-core   PostgreSQL 17 + pgvector, object store dir   8 GB RAM, 4 vCPU
│   ├─ CT 101  inference    Ollama (GPU via LXC device share)            16 GB RAM, 8 vCPU, GPU
│   ├─ CT 102  pat          Pat API (FastAPI) + web app (static)         6 GB RAM, 4 vCPU
│   ├─ CT 103  wade         Wade supervisor + workers (no host access)   6 GB RAM, 4 vCPU
│   ├─ CT 104  sentinel     Safety plane: approvals, e-stop, deploy ctl  2 GB RAM, 2 vCPU
│   ├─ CT 105  observe      Prometheus + Grafana + Alertmanager          3 GB RAM, 2 vCPU
│   └─ CT 106  gateway      Caddy reverse proxy + ntfy (phase 3)         1 GB RAM, 1 vCPU
│
└─ vmbr1  LAB bridge (10.77.0.0/24, NAT to internet only, firewalled from LAN)
    └─ VM 110  forge        Wade's isolated build/test VM (Debian + podman) 12 GB RAM, 6 vCPU
                            dynamic: throttled to 2 vCPU / 4 GB when Pat is busy

Reserved for later phases:
  CT 120 adguard   CT 121 jellyfin (iGPU)   VM 122 homeassistant (HAOS)
  CT 123 frigate (iGPU decode + USB/PCIe accelerator)   VM 124 pterodactyl
```

Rationale for every boundary is in ADR-0003. Totals: 54 GB of caps, but `forge` and `inference` are the only
guests expected to approach their cap, and Sentinel throttles `forge` under interactive load (ADR-0007).

## 3. GPU strategy (ADR-0001)

**Chosen:** NVIDIA driver on the host, GPU exposed to unprivileged LXC containers via device passthrough
(`dev0: /dev/nvidia0 …`), with the *userspace* driver of the identical version inside the container.

- Only CT 101 gets the GPU initially. Frigate/Jellyfin use the Intel iGPU, so the NVIDIA card is not contended.
- A second AI container (e.g. image generation) can share the same GPU later; CUDA time-slices between processes.
  VRAM is the real limit, so the **model-load policy** in the inference service is the scheduler:
  `OLLAMA_MAX_LOADED_MODELS`, keep-alive, and a Sentinel-enforced "foreground first" rule (ADR-0007).
- PCI passthrough to a VM was rejected: it locks the whole card to one VM, costs the host visibility of
  `nvidia-smi`, and makes future sharing impossible without re-architecture.

## 4. Control planes and trust boundaries

```
 Taylor (admin)
   │ Pat UI / PWA  (Tailscale now, gateway later)
   ▼
 ┌──────── Pat (CT 102) ────────┐      ┌──────── Sentinel (CT 104) ─────────┐
 │ chat, memory, projects,      │─────▶│ approval inbox backend             │
 │ approval inbox UI, map       │      │ e-stop flag (authoritative)        │
 └──────────────┬───────────────┘      │ deploy controller                  │
                │ tasks/events          │ Proxmox API token (scoped: pool    │
                ▼                       │   "autonomous", VM.PowerMgmt only) │
 ┌──────── Wade (CT 103) ───────┐      │ budget meter for external AI       │
 │ planner, workers, job queue  │◀────▶│ capability allowlist / tool exec   │
 │ NO proxmox token, NO sudo    │      └──────────────┬─────────────────────┘
 └──────────────┬───────────────┘                     │ stop/throttle
                │ ssh (key, forced cmd)               ▼
                ▼                       ┌──────── forge VM (110) ──────────┐
                                        │ podman build + test, LAB bridge  │
                                        │ no LAN route, no prod secrets    │
                                        └──────────────────────────────────┘
```

Rules that are enforced by software, not prompts:

1. Wade's DB role can read/write only `wade_*` and shared task tables; it cannot read `approvals`, `audit`, or `sentinel_*`.
2. Every privileged action is a row in `approvals` created by Sentinel; the executor verifies an Ed25519-signed,
   single-use, expiring token (ADR-0005) before acting.
3. The e-stop is a file **and** a DB flag owned by Sentinel; Wade's worker loop polls it before claiming any job and
   Sentinel additionally stops CT 103/VM 110 via the Proxmox API. Resume requires an admin-authenticated call.
4. Deploys are performed by Sentinel's deploy controller from a tagged git revision that passed validation in forge;
   Wade can only *request* a deploy.
5. Audit rows are hash-chained (each row stores the SHA-256 of the previous row) so deletion or edits are detectable.

## 5. Data architecture (ADR-0004)

One PostgreSQL 17 instance (CT 100) with `pgvector`, separate databases per trust domain:

- `pat` — conversations (append-only messages), memories (with provenance + `claim_status` enum:
  considered / decided / approved / purchased / installed / completed), projects, tasks, documents.
- `wade` — job queue (`FOR UPDATE SKIP LOCKED`), checkpoints, build results.
- `sentinel` — approvals, approval tokens, audit chain, e-stop state, budgets.
- `observe` is Prometheus TSDB, not Postgres.

No Redis/NATS initially. Postgres `LISTEN/NOTIFY` + a jobs table covers the queue requirements (durable, survives
restarts, failed jobs never disappear). Revisit when >1 worker node exists.

## 6. Inference and model routing (ADR-0006)

- Backend: **Ollama** in CT 101 for phase 2 (single binary, GGUF model management, OpenAI-compatible API).
  llama.cpp server and vLLM are evaluated in the benchmark task (PAT-034) before anything is committed long-term.
- Router lives in `services/shared/patshared/router.py` and is config-driven (`services/inference/models.toml`):
  task class → candidate models → chosen by measured benchmark, not parameter count.
- Candidate starting set (to be *measured*, not assumed): Qwen3-8B (chat/reasoning), Qwen2.5-Coder-14B or Qwen3-14B
  (coding), Qwen3-4B (classification), `nomic-embed-text` (embeddings), a small VLM for images. 16 GB VRAM fits one
  14B Q4 model *or* 8B + 4B + embeddings concurrently; never both 14B and 8B at once.

## 7. Remote access (ADR-0008)

Phase 1: Tailscale on the Proxmox host; SSH and the PVE UI (8006) are reachable only over the tailnet and the LAN.
Phase 3: Pat is served by Caddy in CT 106 on the tailnet with TLS. Phase 7+: an authenticated gateway (Tailscale
Funnel *or* Cloudflare Tunnel in front of Caddy with the app's own auth and device-bound sessions) so the native app
does not need the Tailscale client. The Proxmox UI is never behind that gateway.

## 8. Backups (ADR-0009)

- Local: ZFS snapshots of all guest datasets (hourly ×48, daily ×14) — the "additional recovery mechanism".
- Weekly: `restic` repository on the desktop over SSH (or SMB), encrypted with a key kept in Sentinel's secrets;
  a systemd timer retries every 2 h until the desktop is reachable; restore is tested monthly by restoring into a
  scratch dataset and running the functional test suite against it.
- Future: a dedicated Proxmox Backup Server box.

## 9. What is NOT in scope for the first installer run

Family accounts, Shuri/Monk/Eugene/HAL, Jellyfin, Pterodactyl, Frigate, AdGuard, image/video generation,
the native iOS app, and the Animation Studio. The layout reserves IDs and bridges for them so adding them is
additive.
