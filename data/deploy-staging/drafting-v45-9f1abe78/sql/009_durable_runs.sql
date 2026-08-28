-- Durable search execution (workstream A of the v3 rebuild).
--
-- WHY THIS EXISTS
-- A run used to be a Python thread inside the gunicorn worker with its progress in a process
-- global dict (`webapp._JOBS`). A deploy therefore destroyed a 60 minute search; it happened
-- twice in production (adhoc-ed62f27d3c2a died to the 2026-08-17 22:38 restart). `app_run_queue`
-- (src/run_queue.py) made the REQUEST survive a restart, but not the WORK: a re-queued run
-- started again from zero.
--
-- These tables hold the work itself. A worker claims a run with FOR UPDATE SKIP LOCKED, holds a
-- lease it heartbeats, and writes a checkpoint after every pipeline stage; a worker that dies
-- has its lease reaped and the next worker resumes at the last completed stage.
--
-- NEW TABLES ONLY. Nothing here alters publications, chunks, classifications, citations,
-- families or parties: those are the live corpus and are read only to everything but the offline
-- ingestion path (see docs/corpus_write_policy.md).
--
-- FOUNDATION ONLY. Installing this schema does not cut the web route or SSE stream over, enable a
-- worker, or make every recorded stage resumable. Those integration steps must land together.
--
-- Apply:  psql -h 10.128.0.53 -p 5433 -U patents -d patents -v ON_ERROR_STOP=1 -f sql/009_durable_runs.sql
-- Idempotent: every statement is IF NOT EXISTS.

-- ---------------------------------------------------------------------------------------------
-- 1. search_runs: one row per search. The claim target, the lease holder, the state machine.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_runs (
    run_id              text PRIMARY KEY,
    slug                text NOT NULL,                    -- report identity / cache key
    search_mode         text NOT NULL DEFAULT 'CONCEPT_SEARCH',  -- CONCEPT_SEARCH | CLAIM_ATTACK
    mode                text NOT NULL DEFAULT 'novelty',  -- legal mode (novelty | infringement | ...)
    depth               text NOT NULL DEFAULT 'deep',     -- quick | deep
    lane                text NOT NULL DEFAULT 'deep',     -- gate lane the run spends from
    input               jsonb NOT NULL DEFAULT '{}'::jsonb,   -- query, subject, doc_token, focus
    config              jsonb NOT NULL DEFAULT '{}'::jsonb,   -- budgets, flags, model tiers
    config_fingerprint  text NOT NULL DEFAULT '',         -- sha256 of (input + config + code)
    status              text NOT NULL DEFAULT 'queued',   -- queued running done failed cancelled
    stage               text NOT NULL DEFAULT '',         -- last stage ENTERED
    resume_stage        text NOT NULL DEFAULT '',         -- last stage COMPLETED (where a retry starts)
    attempts            int  NOT NULL DEFAULT 0,
    max_attempts        int  NOT NULL DEFAULT 3,
    priority            int  NOT NULL DEFAULT 100,        -- lower runs first
    worker_id           text,
    heartbeat_at        timestamptz,
    lease_expires_at    timestamptz,
    progress            jsonb NOT NULL DEFAULT '{}'::jsonb,  -- latest progress event (SSE tails this)
    event_seq           bigint NOT NULL DEFAULT 0,        -- bumps on every progress write
    error               text,
    enqueued_at         timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

--  The claim query reads ONLY queued rows. A partial index keeps that read off the finished
--  history entirely, so the claim cost does not grow with the run archive.
CREATE INDEX IF NOT EXISTS ix_search_runs_claim
    ON search_runs (priority, enqueued_at) WHERE status = 'queued';
--  The reaper reads only leased rows.
CREATE INDEX IF NOT EXISTS ix_search_runs_lease
    ON search_runs (lease_expires_at) WHERE status = 'running';
--  The web app looks a run up by slug (newest first) on every status poll.
CREATE INDEX IF NOT EXISTS ix_search_runs_slug ON search_runs (slug, enqueued_at DESC);
-- The SELECT-before-INSERT in enqueue is only an optimization. This partial unique index is the
-- authority when two web requests observe no live row at the same instant.
CREATE UNIQUE INDEX IF NOT EXISTS ux_search_runs_live_slug
    ON search_runs (slug) WHERE status IN ('queued', 'running');

-- ---------------------------------------------------------------------------------------------
-- 2. search_stages: one row per pipeline stage per run. This IS the resume ledger.
--    `payload` is the checkpoint a retry reloads instead of recomputing.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_stages (
    id           bigserial PRIMARY KEY,
    run_id       text NOT NULL REFERENCES search_runs(run_id) ON DELETE CASCADE,
    stage        text NOT NULL,
    attempt      int  NOT NULL DEFAULT 1,
    seq          int  NOT NULL DEFAULT 0,        -- pipeline order, for display
    status       text NOT NULL DEFAULT 'running',-- running | done | failed | skipped | resumed
    n_in         int,
    n_out        int,
    payload      jsonb,                          -- the checkpoint
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error        text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    duration_ms  bigint,
    UNIQUE (run_id, stage, attempt)
);
CREATE INDEX IF NOT EXISTS ix_search_stages_run ON search_stages (run_id, id);
--  Resume asks exactly one question: which stages of this run are already done?
CREATE INDEX IF NOT EXISTS ix_search_stages_done
    ON search_stages (run_id, stage, id DESC) WHERE status = 'done';

-- ---------------------------------------------------------------------------------------------
-- 3. search_queries: every query issued, its channel, its parameters.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_queries (
    id           bigserial PRIMARY KEY,
    run_id       text NOT NULL REFERENCES search_runs(run_id) ON DELETE CASCADE,
    stage        text NOT NULL DEFAULT '',
    channel      text NOT NULL,                  -- dense | bm25 | cpc | citation | family | global | external | docchunks | image
    round        int  NOT NULL DEFAULT 0,
    element_id   text,
    query_text   text NOT NULL DEFAULT '',
    params       jsonb NOT NULL DEFAULT '{}'::jsonb,
    n_hits       int,
    duration_ms  bigint,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_search_queries_run ON search_queries (run_id, id);

-- ---------------------------------------------------------------------------------------------
-- 4. retrieval_hits: RAW per-channel hits, BEFORE fusion. The evidence for "was this retrieved
--    and dropped, or never retrieved?", which used to be answerable only by guesswork.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retrieval_hits (
    id                 bigserial PRIMARY KEY,
    run_id             text NOT NULL REFERENCES search_runs(run_id) ON DELETE CASCADE,
    query_id           bigint REFERENCES search_queries(id) ON DELETE CASCADE,
    channel            text NOT NULL,
    rank               int,
    publication_number text NOT NULL,
    family_id          text,
    score              double precision,
    shard              text NOT NULL DEFAULT '',   -- '' = the hot local corpus
    meta               jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_retrieval_hits_run ON retrieval_hits (run_id, channel, rank);

-- ---------------------------------------------------------------------------------------------
-- 5. search_candidates: the fused, screened and read candidate set, with per-stage scores.
--    One row per (run, publication) with exactly one terminal stage.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_candidates (
    id                 bigserial PRIMARY KEY,
    run_id             text NOT NULL REFERENCES search_runs(run_id) ON DELETE CASCADE,
    publication_number text NOT NULL,
    family_id          text,
    channels           text[] NOT NULL DEFAULT '{}',
    fused_rank         int,
    fused_score        double precision,
    screen_score       double precision,
    screen_verdict     text,
    read_score         double precision,
    read_status        text NOT NULL DEFAULT 'unread',  -- unread | read | rescued | failed
    final_rank         int,
    final_score        double precision,
    stage              text NOT NULL DEFAULT 'fused',   -- terminal stage this candidate reached
    detail             jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, publication_number)
);
CREATE INDEX IF NOT EXISTS ix_search_candidates_run ON search_candidates (run_id, final_rank);

-- ---------------------------------------------------------------------------------------------
-- 6. provider_usage: per provider, per tier, per run. Calls, tokens, latency, errors, cost.
--    The old receipt (run_stats) differenced a PROCESS-WIDE token counter, so two concurrent
--    searches each attributed the other's spend to themselves. A row keyed by run cannot.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_usage (
    id                bigserial PRIMARY KEY,
    run_id            text REFERENCES search_runs(run_id) ON DELETE CASCADE,
    provider          text NOT NULL,                 -- vertex | anthropic | serpapi | epo_ops | ...
    tier              text NOT NULL DEFAULT '',      -- FAST | READ | STRONG | EMBED | ''
    model             text NOT NULL DEFAULT '',
    stage             text NOT NULL DEFAULT '',
    calls             int    NOT NULL DEFAULT 0,
    prompt_tokens     bigint NOT NULL DEFAULT 0,
    completion_tokens bigint NOT NULL DEFAULT 0,
    cached_tokens     bigint NOT NULL DEFAULT 0,
    latency_ms_total  bigint NOT NULL DEFAULT 0,
    errors            int    NOT NULL DEFAULT 0,
    cost_usd          numeric(12,6) NOT NULL DEFAULT 0,
    first_at          timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, provider, tier, model, stage)
);
CREATE INDEX IF NOT EXISTS ix_provider_usage_run ON provider_usage (run_id);

-- ---------------------------------------------------------------------------------------------
-- 7. shard_leases: which shard VM a run has woken and holds, taken when, expiring when.
--    Same lease discipline as the run itself: a heartbeat keeps it, the reaper takes it back.
--    A shard may serve several runs at once, so the lease is per (run, shard); a shard with no
--    held lease has nobody keeping it awake.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shard_leases (
    id           bigserial PRIMARY KEY,
    run_id       text NOT NULL REFERENCES search_runs(run_id) ON DELETE CASCADE,
    shard        text NOT NULL,                  -- shard id (a dormant domain VM)
    host         text NOT NULL DEFAULT '',       -- where it answered, once known
    state        text NOT NULL DEFAULT 'held',   -- held | released | expired
    acquired_at  timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    released_at  timestamptz,
    meta         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shard_leases_held
    ON shard_leases (run_id, shard) WHERE state = 'held';
CREATE INDEX IF NOT EXISTS ix_shard_leases_expiry
    ON shard_leases (expires_at) WHERE state = 'held';

-- ---------------------------------------------------------------------------------------------
-- 8. corpus_ingest_queue: publications a run wants in the next PERMANENT corpus release.
--    This is the ONLY thing a search may leave behind that points at the corpus. The demand
--    fetched text itself goes to the scratch store (sources_docstore), never into chunks.
--    See docs/corpus_write_policy.md for the seam the demand-fetch owner wires into.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus_ingest_queue (
    id                 bigserial PRIMARY KEY,
    publication_number text NOT NULL UNIQUE,
    requested_by_run   text REFERENCES search_runs(run_id) ON DELETE SET NULL,
    reason             text NOT NULL DEFAULT '',   -- why a search wanted it
    source             text NOT NULL DEFAULT '',   -- who fetched the scratch copy
    scratch_ref        text NOT NULL DEFAULT '',   -- key into the scratch store
    payload            jsonb NOT NULL DEFAULT '{}'::jsonb,
    state              text NOT NULL DEFAULT 'pending',  -- pending | claimed | ingested | rejected
    priority           int  NOT NULL DEFAULT 100,
    request_count      int  NOT NULL DEFAULT 1,
    first_requested_at timestamptz NOT NULL DEFAULT now(),
    last_requested_at  timestamptz NOT NULL DEFAULT now(),
    corpus_release     text NOT NULL DEFAULT '',
    ingested_at        timestamptz,
    note               text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_corpus_ingest_pending
    ON corpus_ingest_queue (priority, request_count DESC, first_requested_at)
    WHERE state = 'pending';
