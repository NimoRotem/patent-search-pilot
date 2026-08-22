-- Isolated, always-hot vector corpus fed only by completed niche embedding stages.
-- This migration is never applied to the production patent database.
ALTER TABLE niche_corpus.niche_embedding_stage
    ADD COLUMN IF NOT EXISTS published_at timestamptz;

CREATE TABLE IF NOT EXISTS niche_corpus.niche_embedding_releases (
    corpus_release text PRIMARY KEY,
    model text NOT NULL,
    dimension integer NOT NULL CHECK (dimension > 0),
    task_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (corpus_release, model, dimension, task_type)
);
INSERT INTO niche_corpus.niche_embedding_releases
    (corpus_release,model,dimension,task_type)
SELECT DISTINCT ON (corpus_release)
       corpus_release,model,dimension,task_type
  FROM niche_corpus.niche_embedding_stage
 ORDER BY corpus_release,created_at,chunk_id
ON CONFLICT (corpus_release) DO NOTHING;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='niche_embedding_stage_release_fk'
           AND conrelid='niche_corpus.niche_embedding_stage'::regclass
    ) THEN
        ALTER TABLE niche_corpus.niche_embedding_stage
            ADD CONSTRAINT niche_embedding_stage_release_fk
            FOREIGN KEY (corpus_release, model, dimension, task_type)
            REFERENCES niche_corpus.niche_embedding_releases
                (corpus_release, model, dimension, task_type);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS niche_embedding_stage_publish_idx
    ON niche_corpus.niche_embedding_stage (chunk_id, corpus_release)
    WHERE active AND status = 'complete' AND published_at IS NULL;

CREATE TABLE IF NOT EXISTS niche_corpus.niche_vector_documents (
    chunk_id text NOT NULL,
    corpus_release text NOT NULL,
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
    embedding_key text NOT NULL,
    model text NOT NULL,
    dimension integer NOT NULL,
    task_type text NOT NULL,
    embedding vector(768) NOT NULL,
    embedded_at timestamptz NOT NULL,
    tantivy_indexed_at timestamptz,
    tantivy_index_generation text,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, corpus_release)
);
ALTER TABLE niche_corpus.niche_vector_documents
    ADD COLUMN IF NOT EXISTS model text;
ALTER TABLE niche_corpus.niche_vector_documents
    ADD COLUMN IF NOT EXISTS dimension integer;
ALTER TABLE niche_corpus.niche_vector_documents
    ADD COLUMN IF NOT EXISTS task_type text;
ALTER TABLE niche_corpus.niche_vector_documents
    ADD COLUMN IF NOT EXISTS tantivy_index_generation text;
ALTER TABLE niche_corpus.niche_vector_documents
    ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
UPDATE niche_corpus.niche_vector_documents AS vectors
   SET model=releases.model,
       dimension=releases.dimension,
       task_type=releases.task_type
  FROM niche_corpus.niche_embedding_releases AS releases
 WHERE vectors.corpus_release=releases.corpus_release
   AND (vectors.model IS NULL OR vectors.dimension IS NULL OR vectors.task_type IS NULL);
ALTER TABLE niche_corpus.niche_vector_documents
    ALTER COLUMN model SET NOT NULL;
ALTER TABLE niche_corpus.niche_vector_documents
    ALTER COLUMN dimension SET NOT NULL;
ALTER TABLE niche_corpus.niche_vector_documents
    ALTER COLUMN task_type SET NOT NULL;

CREATE INDEX IF NOT EXISTS niche_vector_documents_publication_idx
    ON niche_corpus.niche_vector_documents
    (publication_id, chunk_kind, chunk_id);
CREATE INDEX IF NOT EXISTS niche_vector_documents_family_idx
    ON niche_corpus.niche_vector_documents
    (family_id, chunk_kind, chunk_id);
CREATE INDEX IF NOT EXISTS niche_vector_documents_tantivy_generation_idx
    ON niche_corpus.niche_vector_documents
    (tantivy_index_generation, chunk_id, corpus_release)
    WHERE active;
CREATE INDEX IF NOT EXISTS niche_vector_documents_active_hnsw_idx
    ON niche_corpus.niche_vector_documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128)
    WHERE active;
