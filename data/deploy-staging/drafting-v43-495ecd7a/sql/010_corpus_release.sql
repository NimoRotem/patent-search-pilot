-- 010: the niche corpus release tables.
--
-- Version 010 is assigned to the corpus workstream by docs/migrations.md. That table also lists
-- `corpus_release`, `corpus_release_active`, `corpus_release_member`, `corpus_release_shard`,
-- `chunks_release` and `corpus_fetch_ledger` as belonging here. They are NOT in this file yet:
-- they come from the release workstream, and when they land they are appended to THIS file rather
-- than given a new number, because two numeric aliases for one version is a hard error in
-- src/migrate.py and three workstreams have already collided on a `009_*.sql` once.
--
-- NOT APPLIED to the live database by this workstream. Application is workstream H's decision,
-- taken once, deliberately (see the brief). Everything here is additive and creates no index on a
-- live corpus table.
--
-- The MANIFEST OF RECORD is newline delimited JSON under data/manifests/<release_id>/, described
-- in docs/niche_manifest_contract.md. These tables are a mirror for consumers that would rather
-- join in SQL than read a file; ops/niche_enumerate.py fills them only when asked with
-- `--emit db`. Nothing in the search path reads them, and nothing here touches publications,
-- chunks, classifications, citations, claims or paragraphs.

-- The frozen boundary a release was cut from. One row per (name, version); the spec is the exact
-- bytes of config/niche_boundary.json, so a release can always be explained without the repo.
CREATE TABLE IF NOT EXISTS corpus_niche_definition (
  name            text NOT NULL,
  version         int  NOT NULL,
  spec            jsonb NOT NULL,
  spec_sha256     text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  note            text,
  PRIMARY KEY (name, version)
);

-- One row per manifest release. `state` mirrors index.json: a reader may consume a release whose
-- state is still 'in_progress', and rows only ever appear for families already written.
CREATE TABLE IF NOT EXISTS corpus_niche_release (
  release_id      text PRIMARY KEY,
  boundary_name   text,
  boundary_version int,
  boundary_sha256 text NOT NULL,
  state           text NOT NULL DEFAULT 'in_progress'
                  CHECK (state IN ('in_progress','complete','abandoned')),
  started_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  families        bigint NOT NULL DEFAULT 0,
  publications    bigint NOT NULL DEFAULT 0,
  summary         jsonb
);

-- One row per family, the same ten fields as a manifest record and in the same order.
CREATE TABLE IF NOT EXISTS corpus_niche_family (
  release_id        text NOT NULL REFERENCES corpus_niche_release(release_id) ON DELETE CASCADE,
  family_id         text NOT NULL,
  publications      text[] NOT NULL DEFAULT '{}',
  cpc               text[] NOT NULL DEFAULT '{}',
  title             text NOT NULL DEFAULT '',
  abstract          text NOT NULL DEFAULT '',
  has_claims        boolean NOT NULL DEFAULT false,
  has_description   boolean NOT NULL DEFAULT false,
  has_complete_text boolean NOT NULL DEFAULT false,
  best_source       text,
  missing_fields    text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (release_id, family_id)
);

-- The consumer's query is "give me the next N families I have not fetched yet, in family_id
-- order", which the primary key already serves. This partial index serves the other one: "which
-- families are actually work", i.e. something is missing and it is not already in a sibling.
CREATE INDEX IF NOT EXISTS ix_niche_family_work
  ON corpus_niche_family (release_id, family_id)
  WHERE best_source IS NOT NULL AND best_source <> 'local:family_member';

-- Publication numbers the niche reaches by citation that this corpus holds no row for at all.
-- This is the only part of the niche that CANNOT be answered locally, so it is kept apart from
-- the manifest rather than mixed into it as a family with no publications.
CREATE TABLE IF NOT EXISTS corpus_niche_external (
  release_id         text NOT NULL REFERENCES corpus_niche_release(release_id) ON DELETE CASCADE,
  publication_number text NOT NULL,
  reason             text NOT NULL DEFAULT 'xy_citation_neighbour',
  PRIMARY KEY (release_id, publication_number)
);
