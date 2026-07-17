-- Heavy indexes — build AFTER bulk load/embed (spec §4). Idempotent.
-- Dense: pgvector HNSW (sufficient at 1-3M vectors per spec §1).
SET maintenance_work_mem = '1GB';
CREATE INDEX IF NOT EXISTS ix_chunks_hnsw
  ON chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Lexical/BM25 stand-in: GIN over generated tsvector (spec §4). ParadeDB pg_search is a
-- drop-in upgrade for true BM25; RRF fuses by rank so ts_rank_cd suffices for the pilot.
CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON chunks USING gin (tsv);

-- Bench HNSW (small subset only).
CREATE INDEX IF NOT EXISTS ix_bench1024_hnsw ON bench_emb_1024 USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_bench3072_hnsw ON bench_emb_3072 USING hnsw (embedding vector_cosine_ops);

ANALYZE chunks;
