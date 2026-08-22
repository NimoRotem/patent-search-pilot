-- Streaming parse-and-embed of newly acquired full text (ops/parsed_embed.py).
--
-- VERSION 017, not 013. `sql/013_run_side_effects.sql` is durable execution's and is the older
-- claim; git merges two files that differ only in name without a murmur and then
-- `migrate.discover()` raises DuplicateVersion and every migrate.py command against the live
-- database stops. 009, 012 and 013 are durable execution's, 010 corpus release, 011 held empty for
-- the eval gold set, 014 acquisition, 015 draft turns, 016 the 002 split, 018 the niche pipeline.
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
    publication_number  text NOT NULL,             -- the CORPUS spelling where the corpus knows it
    -- The number as the SOURCE spelled it. C writes `parsed/{PUBLICATION}/{provider}.json` with
    -- the compact form (`DE10023344C2`) and the corpus stores the hyphenated one
    -- (`DE-10023344-C2`), so the two are genuinely different strings for the same document and
    -- both have to be recorded: one joins to `publications`, the other names the GCS object.
    fetched_number      text NOT NULL DEFAULT '',
    publication_id      bigint NOT NULL,            -- negative = parsed_stage_pub surrogate
    content_sha         text NOT NULL,
    state               text NOT NULL,              -- staged | rejected
    code                text,                       -- rejection code, see src/parsed_norm.py
    reason              text,
    detail              jsonb,
    n_claims            int NOT NULL DEFAULT 0,
    n_paragraphs        int NOT NULL DEFAULT 0,
    n_chunks            int NOT NULL DEFAULT 0,
    -- Provenance. `source` is the provider C fetched from (`serp_self`, `himmpat`,
    -- `corpus:family`). `donor_publication` is set when the record's WORDS belong to a family
    -- sibling: the disclosure is the same document, the text is somebody else's, and flattening
    -- that away puts a staged chunk under a publication whose text nobody has ever read with
    -- nothing on the row to say so.
    source              text,
    donor_publication   text,
    corpus_release      text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_parsed_doc_ledger_pub ON parsed_doc_ledger (publication_number);
CREATE INDEX IF NOT EXISTS ix_parsed_doc_ledger_state ON parsed_doc_ledger (state, code);

-- These three arrived after the table did, on a database where it was already created by an
-- earlier run of the worker's own CREATE TABLE IF NOT EXISTS. A migration that only ships the new
-- CREATE TABLE would be silently wrong on exactly the host that has the data.
ALTER TABLE parsed_doc_ledger ADD COLUMN IF NOT EXISTS fetched_number    text NOT NULL DEFAULT '';
ALTER TABLE parsed_doc_ledger ADD COLUMN IF NOT EXISTS source            text;
ALTER TABLE parsed_doc_ledger ADD COLUMN IF NOT EXISTS donor_publication text;

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

-- Which chunks have already been paid for and staged. `parsed_embed_item` cannot answer that: its
-- row is DELETED in the same transaction that writes the vector, which is what makes the pipeline
-- exactly-once against a kill. The consequence is that the queue forgets, and a publication that
-- arrives a second time (workstream C writes one object per PROVIDER, so a second provider for the
-- same publication is a second document) would be re-enqueued, re-embedded and staged twice.
--
-- `chunks_stage_v3` cannot answer it either: its only unique index is partial, `(kind, ref_id)
-- WHERE ref_id IS NOT NULL`, and every row this pipeline writes has ref_id NULL on purpose, so
-- `ON CONFLICT DO NOTHING` there has no constraint to fire on. Adding a unique index to
-- `chunks_stage_v3` is not an option: it is 8.8M rows and `patents-desc-backfill` is writing it
-- right now.
--
-- So the receipt is kept here, written in that same transaction. `enqueue()` filters against it.
CREATE TABLE IF NOT EXISTS parsed_embed_done (
    item_key            text PRIMARY KEY,
    publication_id      bigint NOT NULL,
    staged_at           timestamptz NOT NULL DEFAULT now()
);

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
