-- Heavy indexes — build AFTER bulk load/embed (spec §4). Idempotent.
-- Dense: pgvector HNSW (sufficient at 1-3M vectors per spec §1).
SET maintenance_work_mem = '1GB';
CREATE INDEX IF NOT EXISTS ix_chunks_hnsw
  ON chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Lexical/BM25 stand-in: GIN over generated tsvector (spec §4). ParadeDB pg_search is a
-- drop-in upgrade for true BM25; RRF fuses by rank so ts_rank_cd suffices for the pilot.
CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON chunks USING gin (tsv);

-- The bench HNSW indexes used to be here. They are in 016 now, because one of them CANNOT BE
-- BUILT: `bench_emb_3072.embedding` is `vector(3072)` and pgvector 0.8.5 refuses an HNSW index
-- above 2000 dimensions. With it in this file, `run.sh`'s closing `apply --only 002` raised on
-- every fresh install, and the two indexes this file exists for, the ones the live search
-- actually uses, never got built. A benchmark fixture must not be able to fail the corpus.

ANALYZE chunks;
