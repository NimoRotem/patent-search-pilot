-- Re-search rounds: one row per "search the current draft, then draft away from what it found".
--
-- The point of the table is the MEASUREMENT. A round records which version of the draft was
-- searched, which search answered, how close the nearest single reference came to the independent
-- claims, and which drafting turn was raised to move away from it. Without that, running the loop
-- twice tells nobody whether it worked; with it, the rounds are a series and the series either
-- falls or it does not.
--
-- `closest_coverage` is a fraction of the independent claims' elements, so it is comparable across
-- rounds of the SAME draft and meaningless across different ones. Nullable on purpose: a round
-- that charted no reference has no reading, and storing 0 there would read as "nothing came close"
-- when the truth is "nothing was measured".
CREATE TABLE IF NOT EXISTS app_draft_research_rounds (
  id                bigserial PRIMARY KEY,
  project_id        bigint      NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  round_no          integer     NOT NULL CHECK (round_no > 0),
  version_no        integer,
  slug              text        NOT NULL DEFAULT '',
  status            text        NOT NULL DEFAULT 'searching',
  imported_count    integer     NOT NULL DEFAULT 0,
  closest_coverage  double precision,
  mean_top3         double precision,
  combination       double precision,
  n_elements        integer     NOT NULL DEFAULT 0,
  n_charted         integer     NOT NULL DEFAULT 0,
  closest_pub       text        NOT NULL DEFAULT '',
  closest_title     text        NOT NULL DEFAULT '',
  reading           jsonb       NOT NULL DEFAULT '{}'::jsonb,
  turn_id           bigint,
  note              text        NOT NULL DEFAULT '',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, round_no)
);

CREATE INDEX IF NOT EXISTS ix_draft_research_rounds_project
  ON app_draft_research_rounds (project_id, round_no DESC);
