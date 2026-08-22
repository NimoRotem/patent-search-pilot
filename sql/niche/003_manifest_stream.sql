-- Incremental bridge cursor for the isolated niche manifest.
-- This migration is never applied to the production patent database.
CREATE INDEX IF NOT EXISTS niche_publications_updated_idx
    ON niche_corpus.niche_publications (updated_at, publication_id);
