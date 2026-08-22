-- Streaming parse, canonical chunks and Gemini Batch staging for the isolated niche database.
-- This migration is never applied to the production patent database.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS niche_corpus.pipeline_identity (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    database_name text NOT NULL,
    fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_input_watermarks (
    source text PRIMARY KEY,
    cursor text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_parsed_sources (
    parsed_source_id bigserial PRIMARY KEY,
    publication_id text NOT NULL
        REFERENCES niche_corpus.niche_publications(publication_id) ON DELETE CASCADE,
    source_uri text NOT NULL,
    source_generation text NOT NULL DEFAULT '',
    parsed_content_hash text NOT NULL,
    parsed jsonb NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_uri, source_generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS niche_parsed_sources_active_uri_idx
    ON niche_corpus.niche_parsed_sources (source_uri)
    WHERE active;
CREATE INDEX IF NOT EXISTS niche_parsed_sources_publication_idx
    ON niche_corpus.niche_parsed_sources (publication_id, active, parsed_source_id);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_parse_jobs (
    job_id bigserial PRIMARY KEY,
    publication_id text NOT NULL,
    source_kind text NOT NULL CHECK (source_kind IN ('local', 'gcs')),
    source_uri text NOT NULL,
    source_generation text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'completed', 'failed')),
    worker_id text,
    lease_until timestamptz,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (source_uri, source_generation)
);
CREATE INDEX IF NOT EXISTS niche_parse_jobs_claim_idx
    ON niche_corpus.niche_parse_jobs (next_attempt_at, job_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS niche_parse_jobs_lease_idx
    ON niche_corpus.niche_parse_jobs (lease_until)
    WHERE status = 'leased';
CREATE INDEX IF NOT EXISTS niche_parse_jobs_publication_idx
    ON niche_corpus.niche_parse_jobs (publication_id, job_id);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_chunks (
    chunk_id text PRIMARY KEY,
    publication_id text NOT NULL,
    family_id text NOT NULL,
    chunk_kind text NOT NULL CHECK (
        chunk_kind IN ('abstract', 'claim_own', 'claim_resolved', 'description', 'figure_caption')
    ),
    claim_number integer,
    language text,
    text text NOT NULL,
    source_location text NOT NULL,
    content_hash text NOT NULL,
    source_uri text NOT NULL,
    source_generation text NOT NULL DEFAULT '',
    active boolean NOT NULL DEFAULT true,
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE niche_corpus.niche_chunks
    ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE niche_corpus.niche_chunks
    ADD COLUMN IF NOT EXISTS retired_at timestamptz;
CREATE INDEX IF NOT EXISTS niche_chunks_publication_idx
    ON niche_corpus.niche_chunks (publication_id, chunk_kind, chunk_id);
CREATE INDEX IF NOT EXISTS niche_chunks_family_idx
    ON niche_corpus.niche_chunks (family_id, chunk_kind, chunk_id);
CREATE INDEX IF NOT EXISTS niche_chunks_content_idx
    ON niche_corpus.niche_chunks (content_hash);

CREATE TABLE IF NOT EXISTS niche_corpus.embedding_budget (
    budget_key text PRIMARY KEY,
    limit_usd numeric(14,6) NOT NULL CHECK (limit_usd > 0),
    reserved_usd numeric(14,6) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
    spent_usd numeric(14,6) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (reserved_usd + spent_usd <= limit_usd)
);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_embedding_batches (
    batch_id bigserial PRIMARY KEY,
    submission_key text NOT NULL UNIQUE,
    status text NOT NULL CHECK (
        status IN ('prepared', 'submitting', 'submitted', 'succeeded', 'failed', 'ambiguous')
    ),
    model text NOT NULL,
    dimension integer NOT NULL CHECK (dimension > 0),
    task_type text NOT NULL,
    corpus_release text NOT NULL,
    request_digest text NOT NULL,
    input_uri text NOT NULL,
    output_prefix text NOT NULL,
    display_name text NOT NULL,
    provider_job_name text UNIQUE,
    provider_state text,
    n_items integer NOT NULL CHECK (n_items > 0),
    estimated_tokens bigint NOT NULL CHECK (estimated_tokens > 0),
    reserved_usd numeric(14,6) NOT NULL CHECK (reserved_usd >= 0),
    budget_key text NOT NULL,
    price_usd_per_million_tokens numeric(14,6) NOT NULL
        CHECK (price_usd_per_million_tokens > 0),
    gcp_project text NOT NULL,
    gcp_location text NOT NULL,
    actual_tokens bigint NOT NULL DEFAULT 0 CHECK (actual_tokens >= 0),
    actual_usd numeric(14,6) NOT NULL DEFAULT 0 CHECK (actual_usd >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
ALTER TABLE niche_corpus.niche_embedding_batches
    ADD COLUMN IF NOT EXISTS budget_key text;
ALTER TABLE niche_corpus.niche_embedding_batches
    ADD COLUMN IF NOT EXISTS price_usd_per_million_tokens numeric(14,6);
ALTER TABLE niche_corpus.niche_embedding_batches
    ADD COLUMN IF NOT EXISTS gcp_project text;
ALTER TABLE niche_corpus.niche_embedding_batches
    ADD COLUMN IF NOT EXISTS gcp_location text;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM niche_corpus.niche_embedding_batches
         WHERE budget_key IS NULL
            OR price_usd_per_million_tokens IS NULL
            OR gcp_project IS NULL
            OR gcp_location IS NULL
    ) THEN
        RAISE EXCEPTION
            'backfill niche embedding batch accounting configuration before migration';
    END IF;
END
$$;
ALTER TABLE niche_corpus.niche_embedding_batches
    ALTER COLUMN budget_key SET NOT NULL,
    ALTER COLUMN price_usd_per_million_tokens SET NOT NULL,
    ALTER COLUMN gcp_project SET NOT NULL,
    ALTER COLUMN gcp_location SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='niche_embedding_batches_price_positive'
           AND conrelid='niche_corpus.niche_embedding_batches'::regclass
    ) THEN
        ALTER TABLE niche_corpus.niche_embedding_batches
            ADD CONSTRAINT niche_embedding_batches_price_positive
            CHECK (price_usd_per_million_tokens > 0);
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS niche_embedding_batches_state_idx
    ON niche_corpus.niche_embedding_batches (status, batch_id);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_embedding_cache (
    embedding_key text PRIMARY KEY,
    content_hash text NOT NULL,
    model text NOT NULL,
    dimension integer NOT NULL CHECK (dimension > 0),
    task_type text NOT NULL,
    text text NOT NULL,
    token_estimate bigint NOT NULL CHECK (token_estimate > 0),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'submitted', 'complete', 'failed')),
    batch_id bigint REFERENCES niche_corpus.niche_embedding_batches(batch_id),
    vector vector(768),
    actual_tokens bigint NOT NULL DEFAULT 0 CHECK (actual_tokens >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS niche_embedding_cache_pending_idx
    ON niche_corpus.niche_embedding_cache (embedding_key)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS niche_embedding_cache_batch_idx
    ON niche_corpus.niche_embedding_cache (batch_id)
    WHERE batch_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS niche_corpus.niche_embedding_batch_items (
    batch_id bigint NOT NULL REFERENCES niche_corpus.niche_embedding_batches(batch_id),
    item_index integer NOT NULL CHECK (item_index >= 0),
    embedding_key text NOT NULL REFERENCES niche_corpus.niche_embedding_cache(embedding_key),
    PRIMARY KEY (batch_id, item_index),
    UNIQUE (batch_id, embedding_key)
);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_embedding_stage (
    chunk_id text NOT NULL REFERENCES niche_corpus.niche_chunks(chunk_id),
    embedding_key text NOT NULL REFERENCES niche_corpus.niche_embedding_cache(embedding_key),
    model text NOT NULL,
    dimension integer NOT NULL CHECK (dimension > 0),
    task_type text NOT NULL,
    corpus_release text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'complete', 'failed')),
    active boolean NOT NULL DEFAULT true,
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, model, dimension, task_type, corpus_release)
);
ALTER TABLE niche_corpus.niche_embedding_stage
    ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE niche_corpus.niche_embedding_stage
    ADD COLUMN IF NOT EXISTS retired_at timestamptz;
CREATE INDEX IF NOT EXISTS niche_embedding_stage_status_idx
    ON niche_corpus.niche_embedding_stage (status, chunk_id);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_tantivy_deletions (
    document_key text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
