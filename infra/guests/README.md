# Guest definitions
One TOML per guest. `patctl` turns these into `pct create` / `qm create` commands (idempotent: existing VMIDs are
reconciled, never recreated). Memory values are hard caps in MiB. `network = "lan"` → vmbr0 (DHCP, reserve on router);
`network = "lab"` → vmbr1 with a per-guest firewall that drops traffic to the LAN and tailnet.
Phase-2+ guests exist here so IDs are reserved; `phase` controls when the installer creates them.
