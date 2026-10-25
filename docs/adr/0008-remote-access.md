# ADR-0008: Remote access — Tailscale now, authenticated gateway later; PVE UI never public

Status: accepted (2026-10-25)

- Phase 1: Tailscale on the Proxmox host (`tailscale up --ssh` is **not** used; OpenSSH with keys stays the
  SSH path so nothing changes mid-configuration). PVE UI bound to LAN + tailnet via the host firewall.
- Phase 3: Pat reachable at `https://pat.<tailnet>.ts.net` via Caddy in CT 106 using Tailscale certs.
- Phase 7: For a native app without the Tailscale client, the candidates are (a) Tailscale Funnel in front
  of Caddy, (b) Cloudflare Tunnel, (c) a WireGuard tunnel embedded in the app. (c) is explicitly *not*
  assumed trivial (Network Extension entitlement, battery, App Store review). Decision deferred to PAT-071
  with a written comparison; whichever is chosen, the app authenticates to Pat's own backend with
  device-bound, revocable sessions, and the Proxmox UI is never behind it.
