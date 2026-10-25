# ADR-0001: GPU sharing via host driver + LXC device passthrough

Status: accepted (2026-10-25) · Supersedes: none

## Context
One RTX 4070 Ti SUPER (16 GB). Consumer GeForce cards support neither MIG nor licensed vGPU. Several
present and future workloads want a GPU: LLM inference (now), embeddings (now), speech (phase 7),
image generation (phase 7), Frigate object detection (phase 9).

## Options
1. **PCI passthrough to one VM.** Clean isolation, no host driver. But the card is exclusively owned
   by that VM; host loses `nvidia-smi`; sharing with a second workload requires moving everything
   into that VM (nesting Docker inside) which defeats the per-service isolation goal.
2. **Host driver + LXC device sharing** (`dev0: /dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm`,
   `/dev/nvidia-uvm-tools`, `/dev/nvidia-modeset`). Any number of containers can use the GPU;
   CUDA time-slices between processes; host keeps monitoring. Costs: driver lives on the host and
   must be rebuilt (DKMS) on kernel upgrades; container userspace driver must match host version
   exactly; unprivileged containers need the device nodes mapped with correct gid.
3. vGPU/MIG — not available on this hardware.

## Decision
Option 2. Only CT 101 (`inference`) gets the devices initially. Frigate and Jellyfin will use the
Intel Arrow Lake iGPU (`/dev/dri/renderD128`) so they never compete with AI inference.

## Consequences
- `infra/proxmox/20-nvidia-host.sh` installs the `.run` driver with `--dkms`, blacklists nouveau,
  loads `nvidia nvidia_uvm nvidia_modeset` at boot, and pins the running kernel until the upgrade
  runbook is followed.
- `infra/proxmox/30-lxc-gpu.sh` adds the device entries to a CT and installs the matching userspace
  driver (`--no-kernel-modules`) inside it.
- VRAM is not partitionable; scheduling is done by model-load policy (ADR-0007).
- Secure Boot must be disabled (ADR-0002) or DKMS modules must be MOK-signed; we choose disabled.
