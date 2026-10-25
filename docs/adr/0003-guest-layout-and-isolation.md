# ADR-0003: Guest layout — which services get a container, a VM, or share one

Status: accepted (2026-10-25)

## Principle
Isolate by *trust level and failure domain*, not by org chart.

| Guest | Type | Why this boundary |
|---|---|---|
| 100 infra-core | LXC unpriv | Postgres is the one thing everything needs; its own guest so it can be snapshotted/backed up and resource-capped independently. Not merged with Pat so a Pat crash/redeploy never touches the DB. |
| 101 inference | LXC unpriv + GPU | GPU device share only works for LXC (ADR-0001). Separate so model reloads/OOMs cannot kill Pat's API. |
| 102 pat | LXC unpriv | Internet-facing (via gateway) app surface; redeployed often. |
| 103 wade | LXC unpriv | Runs *trusted* orchestration code only; it never executes generated code. No Proxmox token, no sudo, outbound restricted by host firewall to infra-core, inference, sentinel, forge, and package mirrors. |
| 104 sentinel | LXC unpriv | Must be stoppable/startable independently of Wade and must hold the only scoped Proxmox API token. |
| 105 observe | LXC unpriv | Monitoring keeps working when everything else is on fire; separate storage growth. |
| 106 gateway | LXC unpriv | TLS termination + ntfy. Single ingress point to audit. |
| 110 forge | **VM** | Executes AI-generated code and untrusted dependencies. Needs kernel isolation and its own network namespace on an isolated bridge (`vmbr1`, NAT only, LAN firewalled). Nested containers (podman) inside a VM is acceptable; nested VMs are not used. |

Rejected: one big Docker-compose VM (no per-service resource control, Proxmox can't throttle inside it);
one container per microservice inside Pat (unneeded operational overhead on a single host).

## Resource caps (initial)
See `infra/guests/*.toml`. Sum of `memory` = 54 GB on 64 GB; host reserve 10 GB incl. ARC 8 GB.
`forge` is the elastic one: Sentinel lowers its `cpulimit` and ballooning target under interactive load.
