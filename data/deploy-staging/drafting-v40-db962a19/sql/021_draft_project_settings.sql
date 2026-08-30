-- Advanced settings for one drafting project, in one column.
--
-- jsonb rather than a column per knob on purpose: the set of settings is expected to grow as more
-- of the pipeline becomes configurable, and every one of those would otherwise be a migration on a
-- table three other programs also write to. `draft_settings.resolve` reads through it, so a
-- project saved before a field existed still gets today's default rather than a NULL.
--
-- The existing `draft_model` column stays where it is: it predates this and is read by the model
-- picker, which now writes both.
ALTER TABLE app_drafting_projects
  ADD COLUMN IF NOT EXISTS settings jsonb NOT NULL DEFAULT '{}'::jsonb;
