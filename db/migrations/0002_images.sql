-- Project PAT migration 0002 — local image generation + editing (spec §23).
-- Stores every generated/edited image's metadata and its prompt (prompt preservation), with a
-- self-referential source_image_id so edits/variations form a lineage (version history).
-- PNG bytes live on the Pat container's disk; this table holds the record + file path.

-- ======================================================================= pat
-- @db pat
CREATE TABLE IF NOT EXISTS images (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid NOT NULL REFERENCES users(id),
  conversation_id  uuid REFERENCES conversations(id),
  mode             text NOT NULL DEFAULT 'generate'
                     CHECK (mode IN ('generate','img2img','inpaint')),
  prompt           text NOT NULL,          -- the prompt actually sent to the model (preserved)
  source_prompt    text,                   -- the user's original idea, if it was enhanced
  negative_prompt  text,
  seed             bigint,
  steps            int,
  guidance         real,
  width            int,
  height           int,
  model            text,
  source_image_id  uuid REFERENCES images(id),   -- edit/variation lineage -> version history
  file_path        text NOT NULL,
  bytes            int,
  gen_seconds      real,
  created_at       timestamptz NOT NULL DEFAULT now(),
  deleted_at       timestamptz
);

CREATE INDEX IF NOT EXISTS images_user_created ON images (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS images_source       ON images (source_image_id);

-- Explicit grant (migrations run as postgres; default privileges also cover this, belt-and-braces).
GRANT SELECT, INSERT, UPDATE, DELETE ON images TO pat_app;
