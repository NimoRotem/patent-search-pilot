-- Acquisition cache for the in-process federated sources (src/sources/docstore.py).
-- Port of App A's SQLite docstore onto the pilot's Postgres: every document any
-- source ever fetches is kept (zlib-compressed text, merge-never-overwrite), so
-- a second search in the same technical field costs a fraction of the first.
--
-- The package also creates this table itself on first use (CREATE TABLE IF NOT
-- EXISTS); this file records the schema in the migration set.
CREATE TABLE IF NOT EXISTS sources_docstore (
    publication_number  text PRIMARY KEY,
    title               text,
    abstract            text,
    claims_z            bytea,          -- zlib-compressed claims text
    description_z       bytea,          -- zlib-compressed description text
    meta                jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
