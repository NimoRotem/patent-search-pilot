-- RETIRED 2026-08-31, kept because it has already run.
--
-- The quick prior-art pass this table recorded was one of three separate ways to search from a
-- draft, and the three of them read as three names for one thing. They were replaced by a single
-- Research control with an effort setting, whose every level goes through the product's own
-- search pipeline and is therefore an ordinary row in app_draft_searches. Nothing writes
-- app_draft_quick_art any more.
--
-- The file stays in the migration list because it is replayable and because the second half of it
-- is still load-bearing: it widened the origin CHECK on app_drafting_references, and narrowing a
-- CHECK that live rows might already satisfy is how a boot-time migration starts failing. The
-- table itself is left in place rather than dropped: it is empty, dropping it buys nothing, and a
-- DROP in a migration that runs on every boot is a bad habit to start.

-- The quick prior-art pass: one row per "find the art this draft has to be written around".
--
-- 020 records a re-search ROUND, which runs the whole search product and produces a number that is
-- comparable across rounds of one draft. This is the other shape of the same question: minutes
-- instead of tens of minutes, dense retrieval over the local corpus only, and no measurement at
-- all. The two tables are separate because merging them would mean one `closest_coverage` column
-- that is a measurement in half the rows and an impression in the other half, and nothing on a
-- page could then honestly say which.
--
-- WHAT IS WORTH KEEPING per pass is the trail: how many queries actually ran, how many distinct
-- publications came back, how many the screen chose, how many of those the corpus could actually
-- be read for, and which of the application's own claim elements NOTHING that was read disclosed.
-- That last one is the whole output. It is what the drafting turn is told to build the
-- independent claims on, and if it is empty the pass says so rather than implying the art is
-- clear.
--
-- `picks` holds one object per attached reference: its number, title, date, why the screen chose
-- it, what the read found, and how many characters of it were actually read. The references
-- themselves live in app_drafting_references like every other reference, so nothing here is the
-- only copy of anything the draft depends on.

CREATE TABLE IF NOT EXISTS app_draft_quick_art (
  id            bigserial   PRIMARY KEY,
  project_id    bigint      NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  pass_no       integer     NOT NULL CHECK (pass_no > 0),
  version_no    integer,
  -- retrieving | selecting | reading | attaching | drafting | complete | failed
  status        text        NOT NULL DEFAULT 'retrieving',
  n_queries     integer     NOT NULL DEFAULT 0,
  n_candidates  integer     NOT NULL DEFAULT 0,
  n_selected    integer     NOT NULL DEFAULT 0,
  n_read        integer     NOT NULL DEFAULT 0,
  n_elements    integer     NOT NULL DEFAULT 0,
  n_uncovered   integer     NOT NULL DEFAULT 0,
  picks         jsonb       NOT NULL DEFAULT '[]'::jsonb,
  uncovered     jsonb       NOT NULL DEFAULT '[]'::jsonb,
  turn_id       bigint,
  note          text        NOT NULL DEFAULT '',
  seconds       double precision,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, pass_no)
);

CREATE INDEX IF NOT EXISTS ix_draft_quick_art_project
  ON app_draft_quick_art (project_id, pass_no DESC);

-- A reference this pass attached was RANKED and READ, but by the fast route: dense retrieval, one
-- screening call, one reading call against the claims. That is a different warrant from `report`,
-- which means the whole search pipeline charted it, and the drafting prompt tells the agent how
-- far each origin may be trusted. So it gets its own value rather than borrowing one that claims
-- more than was done.
ALTER TABLE app_drafting_references DROP CONSTRAINT IF EXISTS app_drafting_references_origin_check;
DO $$ BEGIN
  ALTER TABLE app_drafting_references ADD CONSTRAINT app_drafting_references_origin_check
    CHECK (origin IN ('report', 'upload', 'manual', 'agent', 'quick'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
