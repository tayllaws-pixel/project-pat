# Database
PostgreSQL 17 + pgvector in CT 100. Three databases on one instance: `pat`, `wade`, `sentinel` (ADR-0004).
Roles (created by the provisioning step, passwords from `/etc/project-pat/secrets.env`):
- `pat_app` — owner of `pat`; **no** UPDATE/DELETE on `messages`.
- `wade_app` — owner of `wade`; no access to `pat` or `sentinel`.
- `sentinel_app` — owner of `sentinel`; **no** UPDATE/DELETE on `audit`; read-only on `wade.jobs` (to pause/cancel it writes through Wade's API, which checks Sentinel's signed instruction).
- `pat_reader` — read-only on `wade` reporting tables for the infrastructure map and "what did Wade finish today".
Apply: `python3 db/migrate.py --dsn postgresql://pat_app:...@infra-core/pat --db pat` (idempotent, checksummed).
