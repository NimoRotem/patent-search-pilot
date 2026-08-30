-- Durable, account-owned US patent drafting projects and generation jobs.
--
-- Draft versions are immutable.  Editing disclosure/reference inputs increments the project
-- revision, and a worker may only publish a result generated for the current revision.  This
-- prevents an old background request from replacing a draft after the user changes its inputs.

CREATE TABLE IF NOT EXISTS app_drafting_projects (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  search_slug text NOT NULL,
  title text NOT NULL,
  disclosure_text text NOT NULL,
  inventor_notes text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'queued', 'generating', 'ready', 'archived')),
  revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
  latest_version_no integer NOT NULL DEFAULT 0 CHECK (latest_version_no >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_drafting_projects_owner_updated_idx
  ON app_drafting_projects (user_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS app_drafting_projects_search_idx
  ON app_drafting_projects (user_id, search_slug, updated_at DESC);

CREATE TABLE IF NOT EXISTS app_drafting_references (
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  publication_number text NOT NULL,
  report_rank integer NOT NULL CHECK (report_rank BETWEEN 1 AND 10000),
  title text NOT NULL DEFAULT '',
  source_url text,
  relevance_summary text NOT NULL DEFAULT '',
  snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  selected_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, publication_number)
);
CREATE INDEX IF NOT EXISTS app_drafting_references_rank_idx
  ON app_drafting_references (project_id, report_rank, publication_number);

CREATE TABLE IF NOT EXISTS app_drafting_jobs (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  requested_by_user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  retry_of_job_id bigint REFERENCES app_drafting_jobs(id) ON DELETE SET NULL,
  project_revision integer NOT NULL CHECK (project_revision > 0),
  request_instructions text NOT NULL DEFAULT '',
  system_prompt text NOT NULL,
  user_prompt text NOT NULL,
  prompt_sha256 char(64) NOT NULL,
  allowed_references jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'complete', 'failed', 'cancelled', 'superseded')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  idempotency_key text,
  claimed_by text,
  lease_token_hash char(64),
  lease_expires_at timestamptz,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  last_error text,
  model_name text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS app_drafting_jobs_idempotency_uq
  ON app_drafting_jobs (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS app_drafting_jobs_one_active_uq
  ON app_drafting_jobs (project_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS app_drafting_jobs_queue_idx
  ON app_drafting_jobs (status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS app_drafting_jobs_project_idx
  ON app_drafting_jobs (project_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS app_draft_versions (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  job_id bigint UNIQUE REFERENCES app_drafting_jobs(id) ON DELETE SET NULL,
  version_no integer NOT NULL CHECK (version_no > 0),
  base_version_no integer CHECK (base_version_no IS NULL OR base_version_no > 0),
  project_revision integer NOT NULL CHECK (project_revision > 0),
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'approved', 'archived')),
  sections jsonb NOT NULL,
  markdown text NOT NULL,
  citations jsonb NOT NULL DEFAULT '[]'::jsonb,
  notification_status text NOT NULL DEFAULT 'not_requested'
    CHECK (notification_status IN ('not_requested', 'pending', 'queued')),
  prompt_sha256 char(64),
  model_name text,
  created_by_user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, version_no)
);
CREATE INDEX IF NOT EXISTS app_draft_versions_project_idx
  ON app_draft_versions (project_id, version_no DESC);
CREATE INDEX IF NOT EXISTS app_draft_versions_status_idx
  ON app_draft_versions (project_id, status, version_no DESC);
ALTER TABLE app_draft_versions ADD COLUMN IF NOT EXISTS notification_status text
  NOT NULL DEFAULT 'not_requested';
CREATE INDEX IF NOT EXISTS app_draft_versions_notification_idx
  ON app_draft_versions (notification_status, created_at, id);
