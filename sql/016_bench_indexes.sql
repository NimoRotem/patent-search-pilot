-- Bench HNSW indexes, split out of 002 so a benchmark fixture cannot fail the corpus build.
--
-- WHY THIS FILE EXISTS. `bench_emb_3072.embedding` is `vector(3072)`, and pgvector 0.8.5 caps an
-- HNSW index at 2000 dimensions:
--     ERROR: column cannot have more than 2000 dimensions for hnsw index
-- So `ix_bench3072_hnsw` can never be created, on any host, at any scale. While it sat in 002 the
-- whole file raised, which meant `ix_chunks_hnsw` and `ix_chunks_tsv`, the two indexes the live
-- search depends on, were never built on a fresh install either.
--
-- The 1024 index is real and is built here. The 3072 one is deliberately NOT attempted: an
-- index that the extension refuses is not a pending task, and writing it out as a comment is
-- honest where a commented-out CREATE that somebody re-enables is not. Sequential scan over the
-- bench subset is what that fixture has always actually used.
--
-- If pgvector ever raises the cap, add the index in a NEW migration rather than editing this one:
-- an applied migration's checksum is recorded in the ledger and editing it makes every host
-- disagree about what ran.
CREATE INDEX IF NOT EXISTS ix_bench1024_hnsw
  ON bench_emb_1024 USING hnsw (embedding vector_cosine_ops);
