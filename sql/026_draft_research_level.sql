-- A search started from a draft remembers WHICH RESEARCH LEVEL asked for it.
--
-- The studio used to offer three separate ways to search from a draft, each with its own table
-- and its own vocabulary, and a reader could not tell from a finished result which of them had
-- produced it. There is one control now, with an effort setting, and the setting is a property of
-- the search: a ranked list from the cheapest level and a charted reading from the dearest are
-- different claims about the same draft, and a row that does not say which it is will be read as
-- whichever the reader hopes.
--
-- Everything else about these rows is unchanged, because a research run IS an ordinary search:
-- it has a slug, it appears in the user's history, it opens as a full report, and it renders
-- through the same cards. This adds what the studio needs on top of that and nothing more.
--
-- `reading` is the claim measurement, and it is populated ONLY by the deepest level, which is the
-- only one that builds a chart. Empty on every other level on purpose: storing a zero there would
-- read as "nothing came close" when the truth is "nothing was measured".

ALTER TABLE app_draft_searches
  ADD COLUMN IF NOT EXISTS level text NOT NULL DEFAULT 'find';
ALTER TABLE app_draft_searches
  ADD COLUMN IF NOT EXISTS query_note text NOT NULL DEFAULT '';
ALTER TABLE app_draft_searches
  ADD COLUMN IF NOT EXISTS n_results integer NOT NULL DEFAULT 0;
ALTER TABLE app_draft_searches
  ADD COLUMN IF NOT EXISTS reading jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE app_draft_searches
  ADD COLUMN IF NOT EXISTS redrafted_turn_id bigint;
