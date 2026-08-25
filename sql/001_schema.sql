-- Pilot schema , normalized, provenance-aware (spec §3).
-- Not a single patents table; claims are rows (not JSONB); kind_code kept separate.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sources (
  id serial PRIMARY KEY,
  name text NOT NULL,
  version text,
  fetched_at timestamptz DEFAULT now(),
  UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS applications (
  id bigserial PRIMARY KEY,
  application_number text UNIQUE,
  country text,
  filing_date date,
  earliest_priority_date date
);

CREATE TABLE IF NOT EXISTS publications (
  id bigserial PRIMARY KEY,
  application_id bigint REFERENCES applications(id),
  publication_number text,
  kind_code text,                                   -- SEPARATE columns, not folded into key
  country text,
  publication_date date,
  filing_date date,                                 -- denormalized convenience
  earliest_priority_date date,                      -- denormalized convenience for date engine
  simple_family_id text,
  extended_family_id text,
  title text,
  abstract text,
  abstract_lang text,
  tier text CHECK (tier IN ('core','expanded')),
  facsimile_path text,                              -- authoritative legal evidence (local FS)
  UNIQUE (publication_number, kind_code)
);
CREATE INDEX IF NOT EXISTS ix_pub_simple_family ON publications (simple_family_id);
CREATE INDEX IF NOT EXISTS ix_pub_ext_family ON publications (extended_family_id);
CREATE INDEX IF NOT EXISTS ix_pub_date ON publications (publication_date);
CREATE INDEX IF NOT EXISTS ix_pub_prio ON publications (earliest_priority_date);
CREATE INDEX IF NOT EXISTS ix_pub_tier ON publications (tier);
CREATE INDEX IF NOT EXISTS ix_pub_number ON publications (publication_number);

CREATE TABLE IF NOT EXISTS claims (
  id bigserial PRIMARY KEY,
  publication_id bigint REFERENCES publications(id) ON DELETE CASCADE,
  claim_no int,
  is_independent bool,
  lang text,
  text text,                    -- own text
  resolved_text text            -- text WITH inherited parent limitations
);
CREATE INDEX IF NOT EXISTS ix_claims_pub ON claims (publication_id);

CREATE TABLE IF NOT EXISTS claim_dependencies (
  claim_id bigint REFERENCES claims(id) ON DELETE CASCADE,
  depends_on_claim_id bigint REFERENCES claims(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_claimdep_claim ON claim_dependencies (claim_id);

CREATE TABLE IF NOT EXISTS paragraphs (
  id bigserial PRIMARY KEY,
  publication_id bigint REFERENCES publications(id) ON DELETE CASCADE,
  para_no text, heading text, page_no int,
  lang text,
  text text                                         -- keep coordinates (para/heading/page)
);
CREATE INDEX IF NOT EXISTS ix_para_pub ON paragraphs (publication_id);

CREATE TABLE IF NOT EXISTS figures (
  id bigserial PRIMARY KEY,
  publication_id bigint REFERENCES publications(id) ON DELETE CASCADE,
  figure_no text, caption text, reference_numbers text[]
);
CREATE INDEX IF NOT EXISTS ix_fig_pub ON figures (publication_id);

CREATE TABLE IF NOT EXISTS classifications (
  publication_id bigint REFERENCES publications(id) ON DELETE CASCADE,
  scheme text, symbol text, version_date date,      -- CPC/IPC with version
  is_first bool DEFAULT false
);
CREATE INDEX IF NOT EXISTS ix_class_pub ON classifications (publication_id);
CREATE INDEX IF NOT EXISTS ix_class_symbol ON classifications (symbol);

CREATE TABLE IF NOT EXISTS citations (
  src_pub text, dst_pub text,                        -- publication_number strings (may be off-corpus)
  category text,        -- X / Y / A / P / E ...
  origin text,          -- examiner | applicant | search-report
  search_phase text, occurrences int DEFAULT 1,
  PRIMARY KEY (src_pub, dst_pub, origin)
);
CREATE INDEX IF NOT EXISTS ix_cit_src ON citations (src_pub);
CREATE INDEX IF NOT EXISTS ix_cit_dst ON citations (dst_pub);

CREATE TABLE IF NOT EXISTS parties (
  publication_id bigint REFERENCES publications(id) ON DELETE CASCADE,
  role text,            -- applicant | inventor | assignee
  raw_name text, normalized_name text
);
CREATE INDEX IF NOT EXISTS ix_parties_pub ON parties (publication_id);
CREATE INDEX IF NOT EXISTS ix_parties_norm ON parties (normalized_name);

CREATE TABLE IF NOT EXISTS legal_events (          -- light in pilot; store raw
  publication_id bigint REFERENCES publications(id) ON DELETE CASCADE,
  event_code text, event_date date, raw jsonb
);
CREATE INDEX IF NOT EXISTS ix_legal_pub ON legal_events (publication_id);

CREATE TABLE IF NOT EXISTS field_provenance (      -- generic provenance ledger
  id bigserial PRIMARY KEY,
  entity text, entity_id bigint, field text,
  source_id int REFERENCES sources(id),
  original_value text, normalized_value text,
  ocr_status text, ocr_confidence real, ingested_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_prov_entity ON field_provenance (entity, entity_id);

CREATE TABLE IF NOT EXISTS chunks (
  id bigserial PRIMARY KEY,
  publication_id bigint REFERENCES publications(id) ON DELETE CASCADE,
  kind text,           -- whole|abstract|claim_own|claim_resolved|paragraph|figure_caption
  ref_id bigint,       -- claim_id / paragraph_id / figure_id
  coord jsonb,         -- {claim_no, para_no, page_no} for grounded citations
  lang text,
  text text, token_count int,
  embedding vector(768),
  -- language-neutral lexical column; RRF fuses by rank so exact BM25 not required
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(text,''))) STORED
);
CREATE INDEX IF NOT EXISTS ix_chunks_pub ON chunks (publication_id);
CREATE INDEX IF NOT EXISTS ix_chunks_kind ON chunks (kind);
CREATE INDEX IF NOT EXISTS ix_chunks_null_emb ON chunks (id) WHERE embedding IS NULL;  -- resumable embed

-- Small multi-dimension benchmark stores (spec §7/§8: 768 vs 1024 vs 3072).
CREATE TABLE IF NOT EXISTS bench_emb_1024 (
  chunk_id bigint PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  embedding vector(1024)
);
CREATE TABLE IF NOT EXISTS bench_emb_3072 (
  chunk_id bigint PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  embedding vector(3072)
);

-- Patent-drawing image-similarity index. This is separate from text chunks and stays at the
-- image model's native 768 dimensions. Store the model per row so changing it is an explicit
-- data migration rather than a silent mismatch.
CREATE TABLE IF NOT EXISTS figure_images (
  id                 bigserial PRIMARY KEY,
  publication_id     bigint REFERENCES publications(id) ON DELETE CASCADE,
  publication_number text,
  fig_index          int,
  file_name          text,
  sha256             text,
  model              text NOT NULL,
  embedding          vector(768),
  created_at         timestamptz DEFAULT now(),
  UNIQUE (publication_number, file_name, model)
);
CREATE INDEX IF NOT EXISTS ix_figimg_pub   ON figure_images (publication_id);
CREATE INDEX IF NOT EXISTS ix_figimg_pubno ON figure_images (publication_number);
CREATE INDEX IF NOT EXISTS ix_figimg_model ON figure_images (model);
-- Build HNSW while the table is empty on a fresh install. Existing deployments preserve it.
CREATE INDEX IF NOT EXISTS ix_figimg_hnsw
  ON figure_images USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Convenience view: family-level collapse for reporting (spec §3 last line).
CREATE OR REPLACE VIEW family_of AS
SELECT id AS publication_id,
       COALESCE(NULLIF(simple_family_id,''), publication_number) AS family_key
FROM publications;
