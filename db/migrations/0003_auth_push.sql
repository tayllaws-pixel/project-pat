-- Pat: 2FA (TOTP) on users + web-push subscriptions.
-- Also applied idempotently at Pat startup, so this file is for record/fresh installs.
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
