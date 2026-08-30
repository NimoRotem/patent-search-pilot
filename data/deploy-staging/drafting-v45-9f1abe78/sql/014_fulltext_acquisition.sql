-- 014: full-text acquisition. The fetcher's own tables. Nothing here is a retrieval table.
--
-- Text we do not hold is art we cannot find. Enrichment used to fetch text only for references
-- already chosen to be read, and the choice was a screen score computed from the text that had
-- not been fetched. This is the fetcher that breaks that circle: it works from a niche manifest
-- rather than from a run's read set, it runs continuously, and everything it acquires lands in
-- sources_docstore + GCS + corpus_ingest_queue, never in publications/chunks/claims/paragraphs.
--
-- Four tables:
--   fulltext_fetch_task     the work pool. Publication number is the PRIMARY KEY, which is the
--                           dedup: a publication can be in the pool exactly once. Also the lease.
--   fulltext_fetch_event    the ledger. One row per provider attempt: outcome, chars, credits, ms.
--   fulltext_budget         the hard money cap, reserved atomically so N workers cannot overspend.
--   fulltext_manifest_cursor  where the incremental manifest reader got to.

CREATE TABLE IF NOT EXISTS fulltext_fetch_task (
    publication_number  text PRIMARY KEY,
    family_id           text NOT NULL DEFAULT '',
    country             text NOT NULL DEFAULT '',
    partition_id        smallint NOT NULL,
    priority            integer NOT NULL DEFAULT 100,
    state               text NOT NULL DEFAULT 'pending',
    attempts            integer NOT NULL DEFAULT 0,
    lease_owner         text,
    lease_expires_at    timestamptz,
    provider            text NOT NULL DEFAULT '',
    claims_chars        integer NOT NULL DEFAULT 0,
    desc_chars          integer NOT NULL DEFAULT 0,
    raw_uri             text NOT NULL DEFAULT '',
    parsed_uri          text NOT NULL DEFAULT '',
    last_error          text NOT NULL DEFAULT '',
    manifest            text NOT NULL DEFAULT '',
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fulltext_fetch_task_state_ck CHECK (
        state IN ('pending', 'leased', 'done', 'missing', 'failed', 'skipped'))
);

-- The claim index. Partial, over pending rows only, so a worker never walks the finished pool.
CREATE INDEX IF NOT EXISTS ix_ftt_claimable
    ON fulltext_fetch_task (partition_id, priority, created_at) WHERE state = 'pending';
-- The reaper index: a dead worker's rows must come back to the pool without a full scan.
CREATE INDEX IF NOT EXISTS ix_ftt_lease
    ON fulltext_fetch_task (lease_expires_at) WHERE state = 'leased';
CREATE INDEX IF NOT EXISTS ix_ftt_family ON fulltext_fetch_task (family_id);
CREATE INDEX IF NOT EXISTS ix_ftt_state ON fulltext_fetch_task (state);

CREATE TABLE IF NOT EXISTS fulltext_fetch_event (
    id                  bigserial PRIMARY KEY,
    at                  timestamptz NOT NULL DEFAULT now(),
    worker              text NOT NULL DEFAULT '',
    partition_id        smallint,
    publication_number  text NOT NULL DEFAULT '',
    provider            text NOT NULL,
    outcome             text NOT NULL,
    claims_chars        integer NOT NULL DEFAULT 0,
    desc_chars          integer NOT NULL DEFAULT 0,
    credits             numeric(14,4) NOT NULL DEFAULT 0,
    usd                 numeric(14,6) NOT NULL DEFAULT 0,
    latency_ms          integer NOT NULL DEFAULT 0,
    detail              text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_ffe_at ON fulltext_fetch_event (at DESC);
CREATE INDEX IF NOT EXISTS ix_ffe_provider ON fulltext_fetch_event (provider, outcome);

-- A cap that is only in a process variable is not a cap: four workers each hold their own copy of
-- it and the account is emptied four times over. Reservation is one atomic UPDATE whose WHERE
-- clause carries the cap, so the spend can never cross it however many workers are running.
CREATE TABLE IF NOT EXISTS fulltext_budget (
    provider    text NOT NULL,
    period      text NOT NULL,
    cap         numeric(14,4) NOT NULL,
    spent       numeric(14,4) NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, period)
);

CREATE TABLE IF NOT EXISTS fulltext_manifest_cursor (
    reader      text PRIMARY KEY,
    cursor      text NOT NULL DEFAULT '',
    seeded      bigint NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
