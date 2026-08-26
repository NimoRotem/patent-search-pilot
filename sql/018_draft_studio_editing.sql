-- Draft Studio: a chosen drafting model, hand editing, and section-scoped agent edits.
--
-- Three things the studio could not record before this migration.
--
-- 1. WHICH MODEL DRAFTS. The tier was a process-wide environment variable, so every project on the
--    host shared it and nobody could try a cheaper model on one draft. It belongs to the project.
--
-- 2. WHAT A TURN IS SCOPED TO. A request that must touch only the Field of the Disclosure is a
--    different, far cheaper unit of work than a full revision, and the worker has to be able to
--    tell them apart after a restart, so the scope is a column and not a convention in the prompt.
--
-- 3. WHERE A VERSION CAME FROM. A version the user typed and a version the agent produced are both
--    filing text, but only one of them was checked by the automatic gates. History has to say
--    which, and the studio has to be able to continue an editing session rather than opening a new
--    version on every debounced keystroke.
--
-- Every statement is replayable: this runs on a live database that already holds these tables.
ALTER TABLE app_drafting_projects
  ADD COLUMN IF NOT EXISTS draft_model text NOT NULL DEFAULT '';

ALTER TABLE app_draft_turns
  ADD COLUMN IF NOT EXISTS section_key text NOT NULL DEFAULT '';

ALTER TABLE app_draft_versions
  ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'agent';

ALTER TABLE app_draft_versions
  ADD COLUMN IF NOT EXISTS edited_sections jsonb NOT NULL DEFAULT '[]'::jsonb;

-- The studio asks "is the newest version still the one this person is typing into?" on every
-- autosave, which is the hottest read on this table.
CREATE INDEX IF NOT EXISTS ix_draft_versions_project_origin
  ON app_draft_versions (project_id, version_no DESC, origin);
