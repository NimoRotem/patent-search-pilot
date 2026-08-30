-- What one drafting project has actually put through the models, kept where it can be shown.
--
-- 022 recorded the spend of a headless TURN on the turn's own row, which was the right fix for the
-- eight-hour turn nobody could see. It does not answer the question a person actually asks, which
-- is "what has this application cost me so far", and it cannot: the interactive drafting agent is
-- not a turn, the drawing inspections are not a turn, and the filing reviewer is not a turn. On one
-- real project the interactive agent alone had put 470 million cache-read tokens through Opus and
-- nothing anywhere added it up.
--
-- ONE ROW PER SOURCE OF USAGE, holding running totals rather than one row per model call. A Claude
-- Code session writes its own transcript with an exact usage block on every assistant message, so
-- the row for a transcript remembers how many bytes of it have been counted and adds only what is
-- new. That makes a refresh cheap enough to run on the terminal's own poll, which is what makes
-- the number on the page move while the agent is working.
--
-- `usd` IS A METERED-EQUIVALENT, not money charged. The drafting agent runs on a subscription; the
-- figure is what these tokens would cost at published API rates, which is the only comparable
-- number and is the one worth watching. Anything that displays it has to say so.

CREATE TABLE IF NOT EXISTS app_draft_usage (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  -- terminal | turn | review | figures | filing_qa | research
  source text NOT NULL,
  -- The transcript this row counts, or '' for usage reported directly by the code that spent it.
  path text NOT NULL DEFAULT '',
  model text NOT NULL DEFAULT '',
  bytes_read bigint NOT NULL DEFAULT 0,
  calls bigint NOT NULL DEFAULT 0,
  tokens_input bigint NOT NULL DEFAULT 0,
  tokens_output bigint NOT NULL DEFAULT 0,
  tokens_cache_read bigint NOT NULL DEFAULT 0,
  tokens_cache_write bigint NOT NULL DEFAULT 0,
  usd numeric(12,4) NOT NULL DEFAULT 0,
  first_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- One row per transcript file, and one per (source, model) for directly reported usage.
CREATE UNIQUE INDEX IF NOT EXISTS app_draft_usage_key
  ON app_draft_usage (project_id, source, path, model);
CREATE INDEX IF NOT EXISTS app_draft_usage_project
  ON app_draft_usage (project_id, updated_at DESC);
