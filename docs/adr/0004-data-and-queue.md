# ADR-0004: PostgreSQL + pgvector as the single durable store; no separate queue broker

Status: accepted (2026-10-25)

- One Postgres 17 instance, separate *databases* and *roles* per trust domain (`pat`, `wade`, `sentinel`).
  Roles are least-privilege: `wade_app` cannot `SELECT` from `sentinel.*`.
- pgvector (HNSW index, cosine) for memory and knowledge retrieval; embedding dimension stored per row
  so an embedding-model change can be migrated instead of silently mixed.
- Conversations are **append-only**. Memory corrections create a new `memory_versions` row referencing the
  old one; the original conversation text is never rewritten (spec §6).
- Memories carry `source_conversation_id`, `source_message_id`, `extracted_by` (model+version), `created_at`,
  and `claim_status` ∈ {considered, decided, approved, purchased, installed, completed, retracted}.
- Job queue: a `jobs` table with `FOR UPDATE SKIP LOCKED`, heartbeat `locked_until`, `attempt` counter capped
  at 3 before state `needs_reassessment` (spec §12), and `LISTEN/NOTIFY` to wake workers. Failed jobs are
  never deleted. Rationale: one fewer service to back up, secure and monitor; a single host does not need a
  broker. If a second worker host ever appears, swap the backend behind `patshared.queue.Queue`.
- Migrations: plain numbered SQL files applied by `db/migrate.py` (records applied versions + checksum in
  `schema_migrations`). No ORM migration tool needed for SQL we author ourselves; Alembic can be adopted
  later for the app layer.
