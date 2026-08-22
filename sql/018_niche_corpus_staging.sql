-- Independent niche corpus staging schema.
-- This migration does not write to publications, claims, paragraphs, chunks, or live indexes.
CREATE SCHEMA IF NOT EXISTS niche_corpus;

CREATE TABLE IF NOT EXISTS niche_corpus.niche_publications (
    publication_id text PRIMARY KEY,
    publication_number text NOT NULL UNIQUE,
    family_id text,
    authority text,
    kind_code text,

    title text,
    abstract text,
    language text,

    cpc_codes text[] NOT NULL DEFAULT '{}',
    ipc_codes text[] NOT NULL DEFAULT '{}',

    publication_date date,
    filing_date date,
    earliest_priority_date date,

    has_title boolean NOT NULL DEFAULT false,
    has_abstract boolean NOT NULL DEFAULT false,
    has_claims boolean NOT NULL DEFAULT false,
    has_complete_claims boolean NOT NULL DEFAULT false,
    has_description boolean NOT NULL DEFAULT false,
    has_complete_description boolean NOT NULL DEFAULT false,
    has_figures boolean NOT NULL DEFAULT false,
    has_citations boolean NOT NULL DEFAULT false,

    preferred_source text,
    raw_object_uri text,
    parsed_object_uri text,
    chunk_object_uri text,

    fetch_status text NOT NULL DEFAULT 'pending'
        CHECK (fetch_status IN ('pending', 'leased', 'partial', 'completed', 'failed')),
    fetch_attempts integer NOT NULL DEFAULT 0 CHECK (fetch_attempts >= 0),
    last_provider text,
    last_error text,

    priority smallint NOT NULL DEFAULT 4 CHECK (priority BETWEEN 1 AND 4),
    discovery_signals text[] NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS niche_publications_family_idx
    ON niche_corpus.niche_publications (family_id, publication_id);
CREATE INDEX IF NOT EXISTS niche_publications_status_idx
    ON niche_corpus.niche_publications (fetch_status, priority, publication_id);
CREATE INDEX IF NOT EXISTS niche_publications_authority_idx
    ON niche_corpus.niche_publications (authority, publication_id);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_fetch_attempts (
    attempt_id bigserial PRIMARY KEY,
    publication_id text NOT NULL
        REFERENCES niche_corpus.niche_publications(publication_id) ON DELETE CASCADE,
    provider text NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL,
    http_status integer,
    latency_ms integer NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    credits_used integer NOT NULL DEFAULT 0 CHECK (credits_used >= 0),
    bytes_received bigint NOT NULL DEFAULT 0 CHECK (bytes_received >= 0),
    error_class text,
    error_message text
);

CREATE INDEX IF NOT EXISTS niche_fetch_attempts_pub_idx
    ON niche_corpus.niche_fetch_attempts (publication_id, attempted_at DESC);
CREATE INDEX IF NOT EXISTS niche_fetch_attempts_provider_idx
    ON niche_corpus.niche_fetch_attempts (provider, attempted_at DESC);

CREATE TABLE IF NOT EXISTS niche_corpus.niche_source_objects (
    source_object_id bigserial PRIMARY KEY,
    publication_id text NOT NULL
        REFERENCES niche_corpus.niche_publications(publication_id) ON DELETE CASCADE,
    provider text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    content_hash text NOT NULL,
    media_type text NOT NULL,
    source_url text,
    raw_object_uri text NOT NULL,
    metadata_object_uri text,
    http_status integer,
    size_bytes bigint NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    UNIQUE (publication_id, provider, content_hash)
);

CREATE INDEX IF NOT EXISTS niche_source_objects_pub_idx
    ON niche_corpus.niche_source_objects (publication_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS niche_corpus.corpus_fetch_jobs (
    job_id bigserial PRIMARY KEY,
    publication_id text NOT NULL UNIQUE
        REFERENCES niche_corpus.niche_publications(publication_id) ON DELETE CASCADE,
    priority smallint NOT NULL DEFAULT 4 CHECK (priority BETWEEN 1 AND 4),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'completed', 'failed')),
    worker_id text,
    lease_until timestamptz,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz,
    last_error text
);

CREATE INDEX IF NOT EXISTS corpus_fetch_jobs_claim_idx
    ON niche_corpus.corpus_fetch_jobs (status, next_attempt_at, priority, created_at)
    WHERE status IN ('pending', 'leased');
CREATE INDEX IF NOT EXISTS corpus_fetch_jobs_lease_idx
    ON niche_corpus.corpus_fetch_jobs (lease_until)
    WHERE status = 'leased';

CREATE TABLE IF NOT EXISTS niche_corpus.niche_discovery_watermarks (
    source text NOT NULL,
    scope_key text NOT NULL,
    last_value bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, scope_key)
);
