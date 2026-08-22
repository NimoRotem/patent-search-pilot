-- Streaming parse-and-embed of newly acquired full text (ops/parsed_embed.py).
--
-- Workstream C fetches a batch; this stage parses, chunks and embeds that batch while C fetches
-- the next. Nothing here is a retrieval table and nothing here is indexed for vector search: the
-- vectors land in `chunks_stage_v3`, which `ops/desc_backfill.py` already creates, and which
-- workstream F turns into a release. Writing the live `chunks` table is an insert into a 94 GB
-- HNSW graph while production is querying it, which is why no path in this pipeline can reach it.
--
-- The worker also creates every one of these with CREATE TABLE IF NOT EXISTS on startup, the same
-- way `sources_docstore` and `chunks_stage_v3` do, so it can run before workstream H decides to
-- apply migrations. This file is the record of the schema, not its only applier.

-- Publications that are NOT in `publications` still need a stable id for `chunks_stage_v3`, and
-- `publications` is prohibited to write. The surrogate is used NEGATED, so a staged row for a
-- publication the corpus does not hold can never be mistaken for, or joined to, a real one.
CREATE TABLE IF NOT EXISTS parsed_stage_pub (
    id                  bigserial PRIMARY KEY,
    publication_number  text NOT NULL UNIQUE,
    first_seen          timestamptz NOT NULL DEFAULT now()
);

-- One row per parsed document. The PRIMARY KEY is the idempotence: a document already staged is
-- never parsed, embedded or paid for twice, and a document that was REJECTED keeps its reason
-- instead of being retried for ever against the same defect.
CREATE TABLE IF NOT EXISTS parsed_doc_ledger (
    source_key          text PRIMARY KEY,           -- gcs:bucket/name | docstore:PUBNUM
    publication_number  text NOT NULL,
    publication_id      bigint NOT NULL,            -- negative = parsed_stage_pub surrogate
    content_sha         text NOT NULL,
    state               text NOT NULL,              -- staged | rejected
    code                text,                       -- rejection code, see src/parsed_norm.py
    reason              text,
    detail              jsonb,
    n_claims            int NOT NULL DEFAULT 0,
    n_paragraphs        int NOT NULL DEFAULT 0,
    n_chunks            int NOT NULL DEFAULT 0,
    corpus_release      text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_parsed_doc_ledger_pub ON parsed_doc_ledger (publication_number);
CREATE INDEX IF NOT EXISTS ix_parsed_doc_ledger_state ON parsed_doc_ledger (state, code);

-- The durable work queue. A chunk is written here BEFORE it is embedded and deleted only in the
-- same transaction that writes its vector into `chunks_stage_v3`, so a kill at any instant leaves
-- the work either queued or staged and never lost. `item_key` carries a digest of the text, so a
-- re-parse of unchanged text is free and a re-parse of improved text is new work rather than a
-- silent no-op.
CREATE TABLE IF NOT EXISTS parsed_embed_item (
    id                  bigserial PRIMARY KEY,
    item_key            text NOT NULL UNIQUE,
    source_key          text NOT NULL,
    publication_id      bigint NOT NULL,
    shard               int NOT NULL DEFAULT 0,
    kind                text NOT NULL,
    coord               jsonb,
    lang                text,
    text                text NOT NULL,
    state               text NOT NULL DEFAULT 'queued',   -- queued | submitted | failed
    batch_id            bigint,
    attempts            int NOT NULL DEFAULT 0,
    last_error          text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_parsed_embed_item_work ON parsed_embed_item (state, shard, id);
CREATE INDEX IF NOT EXISTS ix_parsed_embed_item_batch ON parsed_embed_item (batch_id);

-- One row per Gemini Batch prediction job, so a worker that is killed between submitting and
-- collecting finds the job in the database rather than paying for it again.
CREATE TABLE IF NOT EXISTS parsed_embed_batch (
    id                  bigserial PRIMARY KEY,
    job_name            text UNIQUE,
    state               text NOT NULL,              -- submitted | collected | failed
    job_state           text,                       -- the Vertex JOB_STATE_*
    n_items             int NOT NULL DEFAULT 0,
    n_ok                int NOT NULL DEFAULT 0,
    n_failed            int NOT NULL DEFAULT 0,
    input_uri           text,
    output_prefix       text,
    output_dir          text,
    shard               int NOT NULL DEFAULT 0,
    last_error          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- The heartbeat, one row per shard, the same shape `chunks_stage_v3_progress` uses for the
-- description backfill. A SEPARATE table on purpose: sharing one keyed by shard number would let
-- two unrelated jobs collide on a row the day either of them is run with a different shard count.
CREATE TABLE IF NOT EXISTS parsed_embed_progress (
    shard               int PRIMARY KEY,
    shards              int NOT NULL,
    pass_name           text NOT NULL,
    watermarks          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {source_name: cursor}
    docs_seen           bigint NOT NULL DEFAULT 0,
    docs_staged         bigint NOT NULL DEFAULT 0,
    docs_rejected       bigint NOT NULL DEFAULT 0,
    docs_skipped        bigint NOT NULL DEFAULT 0,
    rows_done           bigint NOT NULL DEFAULT 0,
    chars_done          bigint NOT NULL DEFAULT 0,
    api_calls           bigint NOT NULL DEFAULT 0,
    batch_jobs          bigint NOT NULL DEFAULT 0,
    started_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
