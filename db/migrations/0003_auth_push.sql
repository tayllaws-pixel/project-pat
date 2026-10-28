-- Pat: 2FA (TOTP) on users + web-push subscriptions. Applied as the postgres owner
-- (pat_app can't ALTER tables it doesn't own). Runs only against the pat database.
-- @db pat
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret  text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS push_subscriptions (
  id          bigserial PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES users(id),
  endpoint    text UNIQUE NOT NULL,
  p256dh      text NOT NULL,
  auth        text NOT NULL,
  user_agent  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  last_ok     timestamptz
);
CREATE INDEX IF NOT EXISTS push_subs_user ON push_subscriptions(user_id);

-- Default privileges (set in provision-db.sh) already cover tables created by postgres,
-- but grant explicitly too so this migration is self-contained.
GRANT SELECT, INSERT, UPDATE, DELETE ON push_subscriptions TO pat_app;
GRANT USAGE, SELECT ON SEQUENCE push_subscriptions_id_seq TO pat_app;
