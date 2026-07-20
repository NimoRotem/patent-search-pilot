-- Patent-drawing image-similarity index. SEPARATE from the text `chunks` table.
-- One row per (publication, figure file), pgvector at the image model's native dim (768,
-- dinov2-base). `model` is stored per row so corpus and query can never silently drift and
-- a model change is a data migration, not a silent mismatch.
--
-- RAM NOTE: the HNSW index is created HERE, on the EMPTY table, on purpose. An empty-table
-- create is instantaneous and needs no maintenance_work_mem; rows then index INCREMENTALLY
-- as they are inserted, so the backfill never triggers a big index build that would compete
-- with the ~6 GB text HNSW already resident on this 15 GB box. If you ever must REBUILD it,
-- do it off-peak with a modest budget, e.g.:
--     SET maintenance_work_mem = '256MB'; REINDEX INDEX ix_figimg_hnsw;
-- and confirm free RAM first — a full rebuild at 107k rows is still small (<0.5 GB) but the
-- text-HNSW rebuild is not, so never REINDEX both at once.

CREATE TABLE IF NOT EXISTS figure_images (
    id                 bigserial PRIMARY KEY,
    publication_id     bigint REFERENCES publications(id) ON DELETE CASCADE,
    publication_number text,                 -- denormalised: return hits without a join
    fig_index          int,                  -- ordinal of the figure within the publication
    file_name          text,                 -- data/figures/<publication_number>/<file_name>
    sha256             text,                  -- image content hash (idempotency / dedup)
    model              text NOT NULL,         -- IMG_MODEL that produced `embedding`
    embedding          vector(768),
    created_at         timestamptz DEFAULT now(),
    UNIQUE (publication_number, file_name, model)
);

CREATE INDEX IF NOT EXISTS ix_figimg_pub   ON figure_images (publication_id);
CREATE INDEX IF NOT EXISTS ix_figimg_pubno ON figure_images (publication_number);
CREATE INDEX IF NOT EXISTS ix_figimg_model ON figure_images (model);

-- cosine HNSW, same ops/params as the text index; built empty (see RAM NOTE above).
CREATE INDEX IF NOT EXISTS ix_figimg_hnsw
    ON figure_images USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
