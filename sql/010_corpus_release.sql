-- Immutable corpus releases (workstream F of the v3 rebuild).
--
-- WHY THIS EXISTS
-- The corpus is mutable, unversioned and live. `enrich._persist_full_text` runs inside the reading
-- stage of a deep search and inserts into `chunks`, which is an insert into a 101 GB HNSW graph
-- while production is querying it, and that has blocked live searches behind index maintenance.
-- Nothing records what the corpus contained when a search ran, so a recall number cannot be
-- attributed to a corpus and a bad ingest cannot be undone.
--
-- V3 replaces that with releases. Text is fetched into scratch (`sources_docstore`), requested
-- into `corpus_ingest_queue`, and then a release is BUILT OFFLINE and ACTIVATED ATOMICALLY.
-- A serving index is never mutated: it is replaced, or it is not.
--
-- THE SEVEN TABLES
--   corpus_niche_definition  what "the niche" means, as CPC symbols, so the hot corpus is a
--                            definition under version control rather than a WHERE clause
--   corpus_release           one buildable, sealed, activatable release for ONE shard key
--   corpus_release_shard     which CPC domains a release holds, and their counts. This is the
--                            domain -> release lookup `shard_router.route()` output needs
--   corpus_release_member    the families in a release, their home domain and what it cost
--   corpus_release_active    THE SWITCH. One row per shard key. Activation is one transaction
--   chunks_release           the release's chunk payload, LIST-partitioned by release_id so each
--                            release is its own partition with its own HNSW and is never updated
--   corpus_fetch_ledger      what the demand loop fetched, and which release consumed it
--
-- NEW TABLES ONLY. Nothing here alters publications, chunks, classifications, citations or
-- parties. See docs/corpus_write_policy.md and docs/corpus_release.md.
--
-- Apply through src/migrate.py, never with a bare psql -f. Idempotent: every statement is
-- IF NOT EXISTS, CREATE OR REPLACE, or guarded by a DO block.

-- ---------------------------------------------------------------------------------------------
-- 0. The family key. ONE definition, because two would place a family on two shards.
--
--    MEASURED 2026-08-22: 21,862 publications carry simple_family_id = '-1', which is DOCDB for
--    "no simple family is known", not a family. The `family_of` view in 001 only guards the empty
--    string, so today those 21,862 unrelated disclosures share one family key: retrieval dedupes
--    by family key, so they collapse to a single result, and a home shard decided per family
--    would strand all of them on one shard. A sentinel is not a family: such a publication is its
--    own family, which is what a document with no known relatives is.
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus_family_key(simple_family_id text, publication_number text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN lower(coalesce(btrim(simple_family_id), ''))
             IN ('', '-1', '0', 'none', 'null', 'unknown', 'nan')
        THEN coalesce(publication_number, '')
        ELSE btrim(simple_family_id)
    END
$$;

-- ---------------------------------------------------------------------------------------------
-- 1. corpus_niche_definition: what the hot corpus is FOR.
--    The eight seed subgroups live in five CPC subclasses. Shards split at subclass granularity
--    (shard_router.domain_of, four characters) but the niche is defined at subgroup granularity,
--    because B65G as a whole is 12.7% of all classification rows and is not the niche.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus_niche_definition (
    id          bigserial PRIMARY KEY,
    niche       text NOT NULL DEFAULT 'vacuum_handling',
    scheme      text NOT NULL DEFAULT 'CPC',
    symbol      text NOT NULL,                    -- 'B66C1/0225', stored without spaces
    domain      text NOT NULL,                    -- shard_router.domain_of(symbol), four chars
    role        text NOT NULL DEFAULT 'seed',     -- seed | neighbour | excluded
    title       text NOT NULL DEFAULT '',
    note        text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (niche, scheme, symbol)
);
CREATE INDEX IF NOT EXISTS ix_corpus_niche_domain ON corpus_niche_definition (domain);

-- ---------------------------------------------------------------------------------------------
-- 2. corpus_release: one row per release. `hot_v1`, `domain_01_v1`, ... `domain_08_v1`.
--
--    content_hash is a sha256 over the CANONICAL manifest body, and the body deliberately
--    excludes built_at and builder: content addressing that changes when the clock changes tells
--    you nothing about the content. Two builders given the same input produce the same hash, and
--    that is the property a shard checks when it verifies what it is serving.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus_release (
    release_id      text PRIMARY KEY,                  -- 'domain_01_v1'
    shard_key       text NOT NULL,                     -- 'domain_01'
    version         int  NOT NULL,
    kind            text NOT NULL DEFAULT 'domain',    -- hot | domain | niche
    state           text NOT NULL DEFAULT 'building',  -- building|built|verified|active|superseded|failed
    content_hash    text NOT NULL DEFAULT '',
    manifest        jsonb NOT NULL DEFAULT '{}'::jsonb,
    built_from      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- source watermarks, one per input
    counts          jsonb NOT NULL DEFAULT '{}'::jsonb,
    index_params    jsonb NOT NULL DEFAULT '{}'::jsonb,
    stats           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- completeness, compared before activation
    snapshot_path   text NOT NULL DEFAULT '',
    snapshot_sha256 text NOT NULL DEFAULT '',
    snapshot_bytes  bigint NOT NULL DEFAULT 0,
    builder         text NOT NULL DEFAULT '',
    note            text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    built_at        timestamptz,
    sealed_at       timestamptz,                       -- once set, the row is immutable
    UNIQUE (shard_key, version)
);
CREATE INDEX IF NOT EXISTS ix_corpus_release_shardkey ON corpus_release (shard_key, version DESC);
CREATE INDEX IF NOT EXISTS ix_corpus_release_state ON corpus_release (state);
CREATE INDEX IF NOT EXISTS ix_corpus_release_hash ON corpus_release (content_hash);

-- ---------------------------------------------------------------------------------------------
-- 3. corpus_release_shard: the domains a release holds.
--    `shard_router.route()` returns domains; this table is how a domain becomes a shard to wake.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus_release_shard (
    release_id     text NOT NULL REFERENCES corpus_release(release_id) ON DELETE CASCADE,
    domain         text NOT NULL,               -- CPC subclass, or 'unclassified'
    n_families     bigint NOT NULL DEFAULT 0,
    n_publications bigint NOT NULL DEFAULT 0,
    n_chunks       bigint NOT NULL DEFAULT 0,
    index_bytes    bigint NOT NULL DEFAULT 0,
    disk_bytes     bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (release_id, domain)
);
CREATE INDEX IF NOT EXISTS ix_corpus_release_shard_domain ON corpus_release_shard (domain);

-- ---------------------------------------------------------------------------------------------
-- 4. corpus_release_member: the FAMILIES in a release.
--    The unit is the family, not the publication: a family split across two shards is a family
--    that gets deduped against itself, because fusion collapses by family key.
--    `secondary_domains` is the measurable cost of keeping a family whole: those are the domains
--    this family mentions but does not live in, and `corpus.stats` turns the column into a
--    reachability number instead of an assumption.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus_release_member (
    release_id        text NOT NULL REFERENCES corpus_release(release_id) ON DELETE CASCADE,
    family_key        text NOT NULL,
    home_domain       text NOT NULL,
    secondary_domains text[] NOT NULL DEFAULT '{}',
    n_publications    int NOT NULL DEFAULT 0,
    n_chunks          int NOT NULL DEFAULT 0,
    source            text NOT NULL DEFAULT 'corpus',   -- corpus | stage | demand
    PRIMARY KEY (release_id, family_key)
);
CREATE INDEX IF NOT EXISTS ix_corpus_release_member_home
    ON corpus_release_member (release_id, home_domain);

-- ---------------------------------------------------------------------------------------------
-- 5. corpus_release_active: THE SWITCH.
--    One row per shard key, so activation is an UPSERT and a query reads the whole fleet's
--    generation in one statement, which is one MVCC snapshot. Activation of several shard keys
--    happens in ONE transaction, so a reader sees all of it or none of it and a query can never
--    fan out across two generations.
--    `previous_release_id` is what rollback returns to, and rollback swaps the pair, so rolling
--    back a rollback returns to where you were.
-- ---------------------------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS corpus_release_generation_seq;

CREATE TABLE IF NOT EXISTS corpus_release_active (
    shard_key           text PRIMARY KEY,
    release_id          text NOT NULL REFERENCES corpus_release(release_id),
    previous_release_id text REFERENCES corpus_release(release_id),
    generation          bigint NOT NULL,
    activated_at        timestamptz NOT NULL DEFAULT now(),
    activated_by        text NOT NULL DEFAULT '',
    reason              text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_corpus_release_active_gen ON corpus_release_active (generation);

-- ---------------------------------------------------------------------------------------------
-- 6. chunks_release: the release payload.
--
--    LIST-partitioned by release_id. A release is then literally a partition: built once,
--    indexed once, never updated, and retired by detaching rather than by a 40M row DELETE that
--    would rewrite the HNSW graph. That is what makes "immutable" a physical property.
--
--    No `tsv` column and no GIN index: v3's lexical channel is Tantivy, which the release
--    snapshot carries beside the database. The generated tsvector costs a measured 160 B/chunk of
--    index and returns zero gold families on the standing benchmark.
--
--    Every vector carries its model, dimension and chunker version, which `chunks` does not, so a
--    model change is a new release rather than a rewrite of 27.6M rows in place.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks_release (
    release_id         text   NOT NULL,
    chunk_id           bigint NOT NULL,        -- stable within the release
    family_key         text   NOT NULL,
    publication_id     bigint NOT NULL,
    publication_number text   NOT NULL DEFAULT '',
    home_domain        text   NOT NULL,
    kind               text   NOT NULL,
    ref_id             bigint,
    coord              jsonb,
    lang               text,
    text               text   NOT NULL,
    token_count        int,
    embedding          vector(768) NOT NULL,
    embed_model        text   NOT NULL,
    embed_dim          int    NOT NULL,
    task_type          text   NOT NULL DEFAULT '',
    chunker_version    text   NOT NULL DEFAULT '',
    source             text   NOT NULL DEFAULT 'corpus',
    PRIMARY KEY (release_id, chunk_id)
) PARTITION BY LIST (release_id);

-- ---------------------------------------------------------------------------------------------
-- 7. corpus_fetch_ledger: the demand loop's receipt.
--    `corpus_ingest_queue` (sql/009) holds the REQUEST and its request_count, which is the demand
--    signal a release ranks by. This is what happened to that request: what was fetched, from
--    where, how much text came back, and which release it landed in. Without it, a publication
--    that was requested four times and silently produced no text looks the same as one that was
--    never asked for.
--    No foreign key to corpus_ingest_queue: 009 and 010 are applied separately and a release can
--    be built on a scratch database that has no queue at all.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus_fetch_ledger (
    id                 bigserial PRIMARY KEY,
    publication_number text NOT NULL,
    release_id         text NOT NULL DEFAULT '',    -- '' until a release consumes it
    queue_id           bigint,
    request_count      int  NOT NULL DEFAULT 0,
    priority           int  NOT NULL DEFAULT 100,
    source             text NOT NULL DEFAULT '',
    scratch_ref        text NOT NULL DEFAULT '',
    state              text NOT NULL DEFAULT 'fetched',  -- fetched|staged|included|rejected|missing
    claims_chars       int  NOT NULL DEFAULT 0,
    desc_chars         int  NOT NULL DEFAULT 0,
    n_chunks           int  NOT NULL DEFAULT 0,
    reason             text NOT NULL DEFAULT '',
    note               text NOT NULL DEFAULT '',
    fetched_at         timestamptz NOT NULL DEFAULT now(),
    included_at        timestamptz,
    UNIQUE (publication_number, release_id)
);
CREATE INDEX IF NOT EXISTS ix_corpus_fetch_ledger_release ON corpus_fetch_ledger (release_id, state);
CREATE INDEX IF NOT EXISTS ix_corpus_fetch_ledger_demand
    ON corpus_fetch_ledger (request_count DESC, priority) WHERE release_id = '';

-- ---------------------------------------------------------------------------------------------
-- 8. Immutability, enforced by the database rather than by discipline.
--
--    A sealed release may change exactly one column: `state`, because activation and rollback
--    move a release between built / active / superseded. Everything else raises. The membership,
--    the domain counts and the chunk payload of a sealed release cannot be updated or deleted at
--    all: retiring a release means dropping its partition, which is a DDL decision somebody takes
--    on purpose, not an UPDATE that quietly rewrites what a past search was run against.
--
--    CREATE OR REPLACE TRIGGER, not CREATE TRIGGER: a bare CREATE TRIGGER has no IF NOT EXISTS
--    form and is exactly what makes 007_figure_compiler.sql unreplayable (see src/migrate.py).
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus_release_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.sealed_at IS NOT NULL THEN
            RAISE EXCEPTION 'corpus_release % is sealed and cannot be deleted', OLD.release_id
                USING ERRCODE = 'raise_exception';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.sealed_at IS NOT NULL
       AND to_jsonb(NEW) - 'state' IS DISTINCT FROM to_jsonb(OLD) - 'state' THEN
        RAISE EXCEPTION 'corpus_release % is sealed: only state may change', OLD.release_id
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_corpus_release_immutable
    BEFORE UPDATE OR DELETE ON corpus_release
    FOR EACH ROW EXECUTE FUNCTION corpus_release_immutable();

CREATE OR REPLACE FUNCTION corpus_release_child_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    rid text;
    sealed timestamptz;
BEGIN
    rid := CASE WHEN TG_OP = 'DELETE' THEN OLD.release_id ELSE NEW.release_id END;
    SELECT r.sealed_at INTO sealed FROM corpus_release r WHERE r.release_id = rid;
    IF sealed IS NOT NULL THEN
        RAISE EXCEPTION 'row in % belongs to sealed release %: % is refused',
            TG_TABLE_NAME, rid, TG_OP USING ERRCODE = 'raise_exception';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

-- UPDATE and DELETE only. INSERT is what the build does, and a row trigger on 40M inserts is a
-- cost paid on the hot path of every build for no protection: an insert into a sealed release is
-- refused by the builder, which seals only after the last row is in.
CREATE OR REPLACE TRIGGER trg_corpus_release_member_immutable
    BEFORE UPDATE OR DELETE ON corpus_release_member
    FOR EACH ROW EXECUTE FUNCTION corpus_release_child_immutable();

CREATE OR REPLACE TRIGGER trg_corpus_release_shard_immutable
    BEFORE UPDATE OR DELETE ON corpus_release_shard
    FOR EACH ROW EXECUTE FUNCTION corpus_release_child_immutable();

CREATE OR REPLACE TRIGGER trg_chunks_release_immutable
    BEFORE UPDATE OR DELETE ON chunks_release
    FOR EACH ROW EXECUTE FUNCTION corpus_release_child_immutable();
