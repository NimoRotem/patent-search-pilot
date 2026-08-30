-- Richer account profile defaults used by the drafting workspace. Safe to re-run.
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS organization text NOT NULL DEFAULT '';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS default_applicant text NOT NULL DEFAULT '';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS default_inventors text NOT NULL DEFAULT '';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS preferred_jurisdiction text NOT NULL DEFAULT 'US';
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS session_version integer NOT NULL DEFAULT 1;
