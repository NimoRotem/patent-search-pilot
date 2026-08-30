-- Reuse an independent source-fidelity result only for byte-equivalent review inputs.
-- The hash includes the review version, inventor sources, filing brief, text, numerals and
-- figure specifications, so any substantive or policy change requires a fresh review.
CREATE TABLE IF NOT EXISTS app_draft_source_review_cache (
  source_hash char(64) PRIMARY KEY,
  report jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
