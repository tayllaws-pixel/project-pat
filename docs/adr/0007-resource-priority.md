# ADR-0007: Resource priority — foreground first

Status: accepted (2026-10-25)

Mechanisms, cheapest first:
1. **Proxmox caps** (`cores`, `cpulimit`, `cpuunits`, `memory`) per guest. Pat/inference get `cpuunits=2048`,
   wade/forge `cpuunits=512`.
2. **Inference gateway queue** in CT 101: a small FastAPI shim in front of Ollama with two priority lanes;
   background requests wait while any foreground request is in flight, and background models are evicted on
   VRAM pressure.
3. **Sentinel throttle**: when Pat reports an active interactive session (chat or voice), Sentinel sets
   `forge` to `cpulimit=2` via the Proxmox API and raises it back after 5 min idle. Changes are audited.
4. **User override**: Pat exposes "priority profiles" (balanced / my-session-first / max-background) that
   write to Sentinel; Sentinel applies them. No AI agent can change profiles.
