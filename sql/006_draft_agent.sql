-- Conversational, agent-driven drafting.
--
-- Phase one of this product finds the art.  Phase two writes the application, and it is a
-- CONVERSATION rather than a form submission: the user describes the invention (or hands over a
-- draft they already have), the drafting agent writes, the user reacts, and the agent revises.
-- Every one of those exchanges is a TURN.  A turn is durable and leased exactly like the older
-- one-shot generation job, because an agent run takes minutes and a deploy in the middle of one
-- must not lose it or double it.
--
-- What each table is for:
--   app_draft_turns      one agent iteration: the request, the queue/lease state, and afterwards
--                        the agent's own summary of its reasoning and what it changed.
--   app_draft_qa_reports the independent check that runs automatically after every iteration.
--                        Kept separate from the turn so a QA re-run does not rewrite history.
--   app_draft_messages   the rendered conversation.  Denormalised on purpose: the feed must stay
--                        readable even for turns that produced no version and no QA report.
--   app_draft_documents  material the user uploads mid-conversation — further prior art, a spec
--                        they already wrote, lab notes.  The extracted text lives here so the
--                        workspace can be rebuilt on any box from Postgres alone.

-- ---------------------------------------------------------------------------------------------
-- Projects: a drafting project no longer requires a search.
--
-- The original design made `search_slug` NOT NULL because drafting was reachable only from a
-- finished report.  A user who has an invention and no search yet is the more common case, and a
-- user who already has a draft they want improved may never run a search at all.  Prior art
-- remains OPTIONAL input, not a precondition.
-- ---------------------------------------------------------------------------------------------
ALTER TABLE app_drafting_projects ALTER COLUMN search_slug DROP NOT NULL;
ALTER TABLE app_drafting_projects ADD COLUMN IF NOT EXISTS input_kind text NOT NULL DEFAULT 'description';
ALTER TABLE app_drafting_projects ADD COLUMN IF NOT EXISTS agent_session_id text;
ALTER TABLE app_drafting_projects ADD COLUMN IF NOT EXISTS agent_turn_no integer NOT NULL DEFAULT 0;
ALTER TABLE app_drafting_projects ADD COLUMN IF NOT EXISTS applicant text NOT NULL DEFAULT '';
ALTER TABLE app_drafting_projects ADD COLUMN IF NOT EXISTS inventors text NOT NULL DEFAULT '';
DO $$ BEGIN
  ALTER TABLE app_drafting_projects ADD CONSTRAINT app_drafting_projects_input_kind_check
    CHECK (input_kind IN ('description', 'existing_draft'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Where a reference came from decides how far it may be trusted.  A `report` reference was ranked
-- and read by the search pipeline; an `upload` is whatever the user handed us; `manual` is a bare
-- publication number the user typed and we resolved.  The drafting prompt says so explicitly, so
-- the agent does not describe an unread document as though it had read it.
ALTER TABLE app_drafting_references ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'report';
DO $$ BEGIN
  ALTER TABLE app_drafting_references ADD CONSTRAINT app_drafting_references_origin_check
    CHECK (origin IN ('report', 'upload', 'manual', 'agent'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_draft_turns (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  turn_no integer NOT NULL CHECK (turn_no > 0),
  requested_by_user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  project_revision integer NOT NULL CHECK (project_revision > 0),
  kind text NOT NULL DEFAULT 'revise'
    CHECK (kind IN ('initial', 'revise', 'question', 'qa_fix')),
  user_message text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'complete', 'failed', 'cancelled')),
  -- A drafting run is minutes long and silent.  `stage` is the only honest progress signal we
  -- have, so it is written as the worker moves and read straight by the page.
  stage text NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL DEFAULT 2 CHECK (max_attempts BETWEEN 1 AND 6),
  idempotency_key text,
  claimed_by text,
  lease_token_hash char(64),
  lease_expires_at timestamptz,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  agent_session_id text,
  -- The agent's own account of the iteration.  Shown to the user as the summary; kept in full so
  -- a later reader can see WHY a limitation was narrowed, which is the part that decays fastest.
  summary text NOT NULL DEFAULT '',
  reasoning jsonb NOT NULL DEFAULT '[]'::jsonb,
  changes jsonb NOT NULL DEFAULT '[]'::jsonb,
  questions jsonb NOT NULL DEFAULT '[]'::jsonb,
  prior_art_strategy text NOT NULL DEFAULT '',
  answer text NOT NULL DEFAULT '',
  version_no integer CHECK (version_no IS NULL OR version_no > 0),
  transcript_path text,
  cost_usd numeric(10, 4) NOT NULL DEFAULT 0,
  duration_ms integer NOT NULL DEFAULT 0,
  model_name text,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, turn_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS app_draft_turns_one_active_uq
  ON app_draft_turns (project_id) WHERE status IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS app_draft_turns_idempotency_uq
  ON app_draft_turns (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS app_draft_turns_queue_idx
  ON app_draft_turns (status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS app_draft_turns_project_idx
  ON app_draft_turns (project_id, turn_no DESC);

-- A candidate that cleared structural parsing but exhausted an automatic repair cycle is not a
-- published version. Keep it separately so a leased retry continues from that work and its exact
-- review instead of rebuilding the last published version and repeating the same failed attempt.
CREATE TABLE IF NOT EXISTS app_draft_turn_candidates (
  turn_id bigint PRIMARY KEY REFERENCES app_draft_turns(id) ON DELETE CASCADE,
  snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  qa_report jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_draft_qa_reports (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  turn_id bigint REFERENCES app_draft_turns(id) ON DELETE CASCADE,
  version_no integer CHECK (version_no IS NULL OR version_no > 0),
  status text NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'complete', 'failed', 'skipped')),
  -- pass / warn / fail is a triage signal for the reader, not a legal conclusion: it says how many
  -- internal inconsistencies were found, never whether the application is allowable.
  verdict text NOT NULL DEFAULT 'unknown'
    CHECK (verdict IN ('unknown', 'pass', 'warn', 'fail')),
  summary text NOT NULL DEFAULT '',
  -- `checks` are the deterministic ones (numerals, claim dependencies, citation resolution).
  -- `findings` are the reviewing model's, each carrying its own evidence.  Kept apart because a
  -- deterministic FAIL is a fact and a model finding is an opinion, and merging them would let
  -- the second borrow the authority of the first.
  checks jsonb NOT NULL DEFAULT '[]'::jsonb,
  findings jsonb NOT NULL DEFAULT '[]'::jsonb,
  counts jsonb NOT NULL DEFAULT '{}'::jsonb,
  cost_usd numeric(10, 4) NOT NULL DEFAULT 0,
  duration_ms integer NOT NULL DEFAULT 0,
  model_name text,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_draft_qa_project_idx
  ON app_draft_qa_reports (project_id, id DESC);
CREATE INDEX IF NOT EXISTS app_draft_qa_turn_idx ON app_draft_qa_reports (turn_id);

-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_draft_messages (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  turn_id bigint REFERENCES app_draft_turns(id) ON DELETE SET NULL,
  role text NOT NULL CHECK (role IN ('user', 'agent', 'qa', 'system')),
  body text NOT NULL DEFAULT '',
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_draft_messages_project_idx
  ON app_draft_messages (project_id, id);

-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_draft_documents (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  uploaded_by_user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  kind text NOT NULL DEFAULT 'prior_art'
    CHECK (kind IN ('prior_art', 'material', 'source_draft')),
  filename text NOT NULL,
  content_type text NOT NULL DEFAULT '',
  publication_number text,
  title text NOT NULL DEFAULT '',
  note text NOT NULL DEFAULT '',
  -- Extracted text, not the original bytes.  The workspace is rebuildable from Postgres alone,
  -- which is what makes a workspace safe to delete and a project safe to move between hosts.
  body text NOT NULL DEFAULT '',
  char_count integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_draft_documents_project_idx
  ON app_draft_documents (project_id, id);

-- A search launched from the current draft stays attached to that draft. The report itself keeps
-- using the established report cache and account-search row; this table is the small durable link
-- that lets the studio show progress after a reload and import the resulting art in place.
CREATE TABLE IF NOT EXISTS app_draft_searches (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  requested_by_user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  slug text NOT NULL,
  query text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'complete', 'error')),
  imported_count integer NOT NULL DEFAULT 0 CHECK (imported_count >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (project_id, slug)
);
CREATE INDEX IF NOT EXISTS app_draft_searches_project_idx
  ON app_draft_searches (project_id, id DESC);

-- Versions gain the agent's provenance so the history reads as a conversation, not a list, and
-- they carry the two things that used to live only in the workspace directory: the reference
-- numeral table and the figure specifications. Those are PART OF THE DRAFT — a numeral table that
-- disappears when a scratch directory is cleaned takes the meaning of every numeral with it — so
-- they are versioned with the text they belong to, and the workspace becomes a genuine cache.
ALTER TABLE app_draft_versions ADD COLUMN IF NOT EXISTS turn_id bigint;
ALTER TABLE app_draft_versions ADD COLUMN IF NOT EXISTS change_note text NOT NULL DEFAULT '';
ALTER TABLE app_draft_versions ADD COLUMN IF NOT EXISTS numerals jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE app_draft_versions ADD COLUMN IF NOT EXISTS figure_specs jsonb NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS app_draft_versions_turn_idx ON app_draft_versions (turn_id);
