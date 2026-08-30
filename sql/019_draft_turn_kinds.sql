-- The turn-kind check constraint had fallen behind the code that writes turns.
--
-- Two kinds are enqueued by src/ and neither was allowed, so both failed with a CheckViolation at
-- INSERT rather than anywhere a reader would look:
--
--   section_edit   a request scoped to one section of the application. The agent reads everything
--                  and returns a patch, which the app applies; no drawing is touched.
--   gate_resume    draft_studio_service._continue_terminal_filing_repair enqueues this to carry a
--                  checkpointed filing candidate into a fresh turn after a provider disconnect.
--                  It is caught by a bare `except` there, so the automatic continuation was
--                  silently doing nothing on this database.
--
-- THE CONSTRAINT IS RENAMED, and that is not cosmetic. migrate.py decides whether a migration has
-- already run by asking the database whether the objects it creates exist. Redefining a constraint
-- under its old name would answer "yes" before this file had ever been applied, so the new rule
-- gets a name that can only exist once this has run.
--
-- Replayable: both drops are conditional and the add always restates the full list.
ALTER TABLE app_draft_turns DROP CONSTRAINT IF EXISTS app_draft_turns_kind_check;
ALTER TABLE app_draft_turns DROP CONSTRAINT IF EXISTS app_draft_turns_kind_allowed;
ALTER TABLE app_draft_turns ADD CONSTRAINT app_draft_turns_kind_allowed
  CHECK (kind = ANY (ARRAY['initial'::text, 'revise'::text, 'question'::text, 'qa_fix'::text,
                           'section_edit'::text, 'gate_resume'::text]));
