-- Once-per-run side effects (workstream A of the v3 rebuild).
--
-- WHY THIS EXISTS
-- A durable run is RETRIED. `search_runs.attempts` goes to 2 and 3 when a worker is SIGKILLed,
-- when a lease expires, when a provider outage fails a stage. Two things in the pipeline are
-- per RUN and not per ATTEMPT: charging the run against a budget, and telling the person who
-- asked that their search is ready. Both were performed at the end of `webapp._generate`, which
-- runs once per attempt, and the only thing standing between three attempts and three debits was
-- a value in the memory of a process that no longer exists by the time the retry runs.
--
-- So the fact lives in a row with a primary key on (run_id, kind). The second claim loses, in
-- Postgres, and nobody has to remember anything. `runstore.settle` writes the row in the SAME
-- transaction that makes the run terminal, so there is no window in which a run is done and its
-- charge is not recorded.
--
-- NEW TABLE ONLY. Nothing here alters publications, chunks, classifications, citations, families
-- or parties: those are the live corpus and are read only to everything but the offline ingestion
-- path (see docs/corpus_write_policy.md).
--
-- Apply:  psql -h 10.128.0.53 -p 5433 -U patents -d patents -v ON_ERROR_STOP=1 -f sql/013_run_side_effects.sql
-- Idempotent: every statement is IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS run_side_effects (
    run_id     text NOT NULL REFERENCES search_runs(run_id) ON DELETE CASCADE,
    kind       text NOT NULL,          -- charge | notify_complete | ...
    attempt    int  NOT NULL DEFAULT 1,-- which attempt owned it, for the audit trail
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- THE WHOLE POINT. One row per (run, side effect), for ever, whatever the attempt count.
    PRIMARY KEY (run_id, kind)
);

--  "what has this run already done" is the only question asked of it, and it is asked while a
--  transaction holds the run row, so it must be served from the primary key.
CREATE INDEX IF NOT EXISTS ix_run_side_effects_kind ON run_side_effects (kind, created_at);
