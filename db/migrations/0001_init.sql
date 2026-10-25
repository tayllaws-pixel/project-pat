-- Project PAT schema v1 (PAT-022). Applied by db/migrate.py to each database; the DB name selects which
-- section runs (pat | wade | sentinel). Idempotent: every statement is IF NOT EXISTS / DO $$ guarded.
-- Roles are created by the infra-core provisioning step, not here.

-- ======================================================================= common
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

DO $$ BEGIN
  CREATE TYPE claim_status AS ENUM ('considered','decided','approved','purchased','installed','completed','retracted');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE job_state AS ENUM ('queued','claimed','running','awaiting_approval','awaiting_validation',
                                 'succeeded','failed','needs_reassessment','cancelled','paused');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE priority AS ENUM ('foreground','high','normal','background');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ======================================================================= pat
-- @db pat
CREATE TABLE IF NOT EXISTS users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username      text UNIQUE NOT NULL,
  display_name  text NOT NULL,
  role          text NOT NULL CHECK (role IN ('admin','member','child')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  disabled_at   timestamptz
);

CREATE TABLE IF NOT EXISTS sessions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id),
  device_label  text,
  token_hash    bytea UNIQUE NOT NULL,            -- sha256 of the bearer; raw token never stored
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz,
  expires_at    timestamptz NOT NULL,
  revoked_at    timestamptz
);

CREATE TABLE IF NOT EXISTS projects (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id      uuid NOT NULL REFERENCES users(id),
  slug          text UNIQUE NOT NULL,
  title         text NOT NULL,
  description   text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('proposed','active','paused','archived')),
  priority      int  NOT NULL DEFAULT 50,
  restricted    boolean NOT NULL DEFAULT false,   -- e.g. Lake Alapaha creative content: denied to Wade by default
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  last_activity_at timestamptz
);

CREATE TABLE IF NOT EXISTS conversations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id),
  project_id    uuid REFERENCES projects(id),
  title         text,
  parent_message_id uuid,                          -- branching: conversation forked from a message (spec §9)
  created_at    timestamptz NOT NULL DEFAULT now(),
  archived_at   timestamptz,
  deleted_at    timestamptz                        -- soft delete; hard delete is an admin-approved job
);
CREATE INDEX IF NOT EXISTS conversations_user_idx ON conversations(user_id, created_at DESC);

-- Append-only. No UPDATE/DELETE grants on this table for application roles (set by provisioning).
CREATE TABLE IF NOT EXISTS messages (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  seq           bigint GENERATED ALWAYS AS IDENTITY,
  role          text NOT NULL CHECK (role IN ('user','assistant','system','tool')),
  actor_id      text NOT NULL,                     -- "taylor", "pat", "wade", ...
  content       text NOT NULL,
  content_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  model         text,
  tool_calls    jsonb,
  attachments   jsonb,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conv_idx ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS messages_fts_idx ON messages USING gin(content_tsv);

CREATE TABLE IF NOT EXISTS message_embeddings (
  message_id    uuid PRIMARY KEY REFERENCES messages(id),
  model         text NOT NULL,
  dims          int  NOT NULL,
  embedding     vector(1024) NOT NULL              -- padded/truncated per model; dims records the real size
);
CREATE INDEX IF NOT EXISTS message_embeddings_hnsw ON message_embeddings USING hnsw (embedding vector_cosine_ops);

-- Long-term memory: each correction is a new version; nothing is rewritten (spec §6).
CREATE TABLE IF NOT EXISTS memories (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id),
  project_id    uuid REFERENCES projects(id),
  kind          text NOT NULL CHECK (kind IN ('personal','project','technical','supervisor','decision','task','system')),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_versions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id     uuid NOT NULL REFERENCES memories(id),
  version       int  NOT NULL,
  content       text NOT NULL,
  claim_status  claim_status NOT NULL DEFAULT 'considered',
  confidence    real CHECK (confidence BETWEEN 0 AND 1),
  source_conversation_id uuid REFERENCES conversations(id),
  source_message_id      uuid REFERENCES messages(id),
  extracted_by  text NOT NULL,                     -- model+version or 'human:<username>'
  supersedes    uuid REFERENCES memory_versions(id),
  correction_note text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  deleted_at    timestamptz,                       -- user-initiated deletion of a memory (history row kept)
  UNIQUE (memory_id, version)
);
CREATE INDEX IF NOT EXISTS memory_versions_mem_idx ON memory_versions(memory_id, version DESC);

CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_version_id uuid PRIMARY KEY REFERENCES memory_versions(id),
  model         text NOT NULL,
  dims          int  NOT NULL,
  embedding     vector(1024) NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_embeddings_hnsw ON memory_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS project_links (        -- conversations/documents/tasks associated to a project
  project_id    uuid NOT NULL REFERENCES projects(id),
  item_type     text NOT NULL,                     -- conversation | document | task | job | report
  item_id       uuid NOT NULL,
  linked_by     text NOT NULL,                     -- 'auto:pat' or 'human:taylor'
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, item_type, item_id)
);

CREATE TABLE IF NOT EXISTS documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id      uuid NOT NULL REFERENCES users(id),
  project_id    uuid REFERENCES projects(id),
  title         text NOT NULL,
  mime          text NOT NULL,
  sha256        text NOT NULL,
  bytes         bigint NOT NULL,
  storage_path  text NOT NULL,                     -- under infra-core object dir
  version       int  NOT NULL DEFAULT 1,
  supersedes    uuid REFERENCES documents(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (                 -- human-facing tasks (spec §7, §9), distinct from wade.jobs
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id    uuid REFERENCES projects(id),
  title         text NOT NULL,
  description   text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','blocked','done','cancelled','paused')),
  priority      priority NOT NULL DEFAULT 'normal',
  assigned_to   text,                              -- actor id
  blocker       text,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  evidence      jsonb                              -- links proving completion (spec §28)
);

CREATE TABLE IF NOT EXISTS events (                -- system events relevant to the user; feeds map + notifications
  id            uuid PRIMARY KEY,
  type          text NOT NULL,
  actor_kind    text NOT NULL,
  actor_id      text NOT NULL,
  subject       text,
  severity      text NOT NULL,
  data          jsonb NOT NULL DEFAULT '{}',
  notify        boolean NOT NULL DEFAULT false,
  notified_at   timestamptz,
  at            timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS events_at_idx ON events(at DESC);
CREATE INDEX IF NOT EXISTS events_subject_idx ON events(subject, at DESC);

-- ======================================================================= wade
-- @db wade
CREATE TABLE IF NOT EXISTS jobs (
  id            uuid PRIMARY KEY,
  kind          text NOT NULL,
  title         text NOT NULL,
  project_id    uuid,
  priority      priority NOT NULL DEFAULT 'background',
  state         job_state NOT NULL DEFAULT 'queued',
  payload       jsonb NOT NULL DEFAULT '{}',
  depends_on    uuid[] NOT NULL DEFAULT '{}',
  attempt       int  NOT NULL DEFAULT 0,
  max_attempts  int  NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  claimed_by    text,
  locked_until  timestamptz,                       -- heartbeat lease; a dead worker's job is reclaimable
  last_error    text,
  failure_class text,                              -- test_failure | build_error | timeout | needs_capability | unknown
  result        jsonb,
  evidence      jsonb,                             -- build/test log URIs, commit sha
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(state, priority, created_at) WHERE state IN ('queued','paused');

CREATE TABLE IF NOT EXISTS job_checkpoints (
  id            bigserial PRIMARY KEY,
  job_id        uuid NOT NULL REFERENCES jobs(id),
  attempt       int  NOT NULL,
  stage         text NOT NULL,
  data          jsonb NOT NULL DEFAULT '{}',
  at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS job_checkpoints_job_idx ON job_checkpoints(job_id, id DESC);

CREATE TABLE IF NOT EXISTS job_log (               -- failed jobs never disappear: every transition is kept
  id            bigserial PRIMARY KEY,
  job_id        uuid NOT NULL REFERENCES jobs(id),
  from_state    job_state,
  to_state      job_state NOT NULL,
  note          text,
  at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS benchmarks (
  id            bigserial PRIMARY KEY,
  model         text NOT NULL,
  role          text NOT NULL,
  metric        text NOT NULL,
  value         double precision NOT NULL,
  unit          text,
  vram_mb       int,
  tokens_per_s  real,
  latency_ms_p50 real,
  gpu           text,
  driver        text,
  backend       text,
  measured_at   timestamptz NOT NULL DEFAULT now(),
  raw           jsonb
);

-- ======================================================================= sentinel
-- @db sentinel
CREATE TABLE IF NOT EXISTS approvals (
  id            uuid PRIMARY KEY,
  requester_kind text NOT NULL,
  requester_id  text NOT NULL,
  action        text NOT NULL,
  reason        text NOT NULL,
  affected      text[] NOT NULL DEFAULT '{}',
  consequences  text NOT NULL DEFAULT '',
  resources     jsonb NOT NULL DEFAULT '{}',
  estimated_external_cost_usd numeric(10,4) NOT NULL DEFAULT 0,
  validation_evidence text[] NOT NULL DEFAULT '{}',
  rollback      text NOT NULL DEFAULT '',
  payload       jsonb NOT NULL DEFAULT '{}',
  payload_sha256 text NOT NULL,
  requested_at  timestamptz NOT NULL,
  expires_at    timestamptz NOT NULL,
  decision      text CHECK (decision IN ('approved','denied','expired','revoked')),
  decided_by    text,
  decided_at    timestamptz,
  decision_note text
);
CREATE INDEX IF NOT EXISTS approvals_open_idx ON approvals(requested_at DESC) WHERE decision IS NULL;

CREATE TABLE IF NOT EXISTS approval_tokens (
  nonce         text PRIMARY KEY,
  approval_id   uuid NOT NULL REFERENCES approvals(id),
  issued_at     timestamptz NOT NULL DEFAULT now(),
  exp           timestamptz NOT NULL,
  used_at       timestamptz,
  used_by       text
);

CREATE TABLE IF NOT EXISTS estop (
  singleton     boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  engaged       boolean NOT NULL DEFAULT false,
  engaged_by    text,
  engaged_at    timestamptz,
  reason        text,
  released_by   text,
  released_at   timestamptz
);
INSERT INTO estop (singleton, engaged) VALUES (true, false) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS agent_controls (        -- pause / disable per agent (spec §18 separates these controls)
  actor_id      text PRIMARY KEY,
  paused        boolean NOT NULL DEFAULT false,
  disabled      boolean NOT NULL DEFAULT false,
  set_by        text NOT NULL,
  set_at        timestamptz NOT NULL DEFAULT now(),
  note          text
);

CREATE TABLE IF NOT EXISTS budgets (
  provider      text PRIMARY KEY,
  enabled       boolean NOT NULL DEFAULT false,    -- disabled until Taylor configures it (spec §16)
  monthly_cap_usd numeric(10,2) NOT NULL DEFAULT 5.00,
  spent_month_usd numeric(10,4) NOT NULL DEFAULT 0,
  month         date NOT NULL DEFAULT date_trunc('month', now())::date,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS external_calls (
  id            bigserial PRIMARY KEY,
  provider      text NOT NULL,
  requester     text NOT NULL,
  job_id        uuid,
  issue_key     text,                              -- same unresolved issue → max 3 escalations (spec §16)
  approval_id   uuid,
  redaction_report jsonb,
  cost_usd      numeric(10,4),
  at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit (                 -- hash-chained (ADR-0005). Append-only; no UPDATE/DELETE grants.
  id            bigserial PRIMARY KEY,
  ts            timestamptz NOT NULL DEFAULT now(),
  actor         text NOT NULL,
  action        text NOT NULL,
  details       jsonb NOT NULL DEFAULT '{}',
  prev_hash     text NOT NULL,
  hash          text NOT NULL UNIQUE
);

-- ======================================================================= bookkeeping (all dbs)
-- @db all
CREATE TABLE IF NOT EXISTS schema_migrations (
  version       text PRIMARY KEY,
  checksum      text NOT NULL,
  applied_at    timestamptz NOT NULL DEFAULT now()
);
