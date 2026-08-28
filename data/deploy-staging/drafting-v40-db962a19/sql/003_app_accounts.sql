-- Product-layer state for named accounts, saved searches and durable mail delivery.
-- Kept in the existing Postgres service so the single gunicorn worker can be restarted
-- without losing users, ownership or completion notifications.

CREATE TABLE IF NOT EXISTS app_users (
  id bigserial PRIMARY KEY,
  email text NOT NULL,
  full_name text NOT NULL,
  password_hash text NOT NULL,
  is_admin boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT true,
  email_on_completion boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz,
  UNIQUE (email)
);
CREATE UNIQUE INDEX IF NOT EXISTS app_users_email_lower_uq ON app_users (lower(email));

CREATE TABLE IF NOT EXISTS app_saved_searches (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  slug text NOT NULL,
  query text NOT NULL DEFAULT '',
  title text,
  mode text NOT NULL DEFAULT 'novelty',
  search_focus text NOT NULL DEFAULT 'all_text',
  subject text,
  status text NOT NULL DEFAULT 'running',
  saved boolean NOT NULL DEFAULT true,
  notify_email boolean NOT NULL DEFAULT true,
  notification_status text NOT NULL DEFAULT 'not_requested',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  last_viewed_at timestamptz,
  UNIQUE (user_id, slug)
);
CREATE INDEX IF NOT EXISTS app_saved_searches_user_updated_idx
  ON app_saved_searches (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS app_saved_searches_slug_idx ON app_saved_searches (slug);

CREATE TABLE IF NOT EXISTS app_report_flags (
  user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  slug text NOT NULL,
  publication_number text NOT NULL,
  flag text NOT NULL DEFAULT '',
  note text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, slug, publication_number)
);
CREATE INDEX IF NOT EXISTS app_report_flags_search_idx ON app_report_flags (user_id, slug);

CREATE TABLE IF NOT EXISTS app_mail_outbox (
  id bigserial PRIMARY KEY,
  dedupe_key text NOT NULL UNIQUE,
  user_id bigint REFERENCES app_users(id) ON DELETE SET NULL,
  search_slug text,
  to_email text NOT NULL,
  kind text NOT NULL,
  subject text NOT NULL,
  body_text text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  attempts integer NOT NULL DEFAULT 0,
  last_error text,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS app_mail_outbox_pending_idx
  ON app_mail_outbox (status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS app_password_reset_tokens (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  used_at timestamptz
);
CREATE INDEX IF NOT EXISTS app_password_reset_user_idx
  ON app_password_reset_tokens (user_id, created_at DESC);
