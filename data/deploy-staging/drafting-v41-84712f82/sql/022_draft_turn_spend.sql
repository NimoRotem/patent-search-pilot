-- What a drafting turn actually spends, recorded WHILE it spends it.
--
-- Until now a running turn showed cost 0.00 and duration 0, because both were written once at
-- completion. One turn on this database ran for eight hours and twenty minutes across 76 agent
-- runs, put roughly 196 MILLION tokens through the models and cost about $343, and none of that
-- was visible anywhere: the row said zero the whole time and the page said "independent review".
--
-- `model_ms` is time inside the models. `duration_ms` stays what it always was. Wall clock is
-- started_at to now, which is a different and larger number again, and the difference between the
-- two is the queue.
--
-- Cache reads are counted separately on purpose. They are most of the volume on a turn that
-- resumes a session every repair round, and folding them into one total hides exactly the shape of
-- the problem.
ALTER TABLE app_draft_turns ADD COLUMN IF NOT EXISTS agent_runs        integer NOT NULL DEFAULT 0;
ALTER TABLE app_draft_turns ADD COLUMN IF NOT EXISTS model_ms          bigint  NOT NULL DEFAULT 0;
ALTER TABLE app_draft_turns ADD COLUMN IF NOT EXISTS spend_usd         numeric(12,4) NOT NULL DEFAULT 0;
ALTER TABLE app_draft_turns ADD COLUMN IF NOT EXISTS tokens_input      bigint  NOT NULL DEFAULT 0;
ALTER TABLE app_draft_turns ADD COLUMN IF NOT EXISTS tokens_output     bigint  NOT NULL DEFAULT 0;
ALTER TABLE app_draft_turns ADD COLUMN IF NOT EXISTS tokens_cache_read bigint  NOT NULL DEFAULT 0;
ALTER TABLE app_draft_turns ADD COLUMN IF NOT EXISTS tokens_cache_write bigint NOT NULL DEFAULT 0;
