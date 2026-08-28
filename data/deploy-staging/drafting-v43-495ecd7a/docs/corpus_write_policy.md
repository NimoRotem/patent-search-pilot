# Corpus writes are prohibited in the durable search worker

Status: foundation implemented, not deployed and not cut over. Workstream A (durable execution)
owns the prohibition and `corpus_ingest_queue`. The existing web route still dispatches searches
through the legacy in-process queue, so it is not protected by this guard yet. The prohibition
becomes the production invariant only after the route, status stream and Supervisor worker are
cut over together. The demand-fetch path itself is another workstream; the seam it wires into is
at the bottom of this file.

## The defect

`enrich._persist_full_text` is called from `deep_rank._enrich_missing_text`, which is called from
the reading stage of every deep search. It inserts into `claims`, `paragraphs` and `chunks`,
updates `publications.abstract`, inserts `legal_events` and `field_provenance`, and with
`reembed=True` writes embeddings into `chunks.embedding`, which is an insert into a 94 GB HNSW
graph while live searches are querying it. That has blocked live searches behind index
maintenance.

`ops.py` and `incremental_ingest.py` write the same tables and are legitimate. The difference is
not what is written, it is who is writing. So the rule cannot be a convention in a code review; it
has to be a property of the connection.

## What is enforced

`src/corpus_guard.py`. A process either is or is not allowed to write the corpus.

| Process | Armed | Effect |
|---|---|---|
| `runner.worker` (the search worker) | yes, `corpus_guard.arm()` in `main()` | every connection refuses corpus writes |
| `webapp` (gunicorn) | no | still runs the legacy dispatcher until the durable route cutover |
| `ingest_bq`, `ingest_pg`, `chunker`, `embed`, `ops`, `incremental_ingest`, `ops/*.py` | no | write exactly as before |
| anything with `PATENT_CORPUS_INGEST=1` | no | the explicit disarm for a batch job started from an armed parent |

When armed, `db.connect()` and `db.cursor()` hand back connections whose `cursor_factory` is a
guarded cursor. `execute`, `executemany`, `stream` and `copy` all run the statement through
`corpus_guard.check()` first, so the refusal happens in this process, before Postgres is asked.
A cursor cannot be obtained from a guarded connection any other way, which is what makes it
structural rather than advisory.

`db.corpus_cursor()` additionally opens a `READ ONLY` transaction, which Postgres enforces itself.
That is the belt to the guard's braces: it holds even for a caller that reached psycopg directly.

### Protected tables

`publications`, `chunks`, `classifications`, `citations`, `families`, `parties` (the six named in
the rebuild brief), plus the tables the same enrichment code path writes in the same breath:
`claims`, `claim_dependencies`, `paragraphs`, `figures`, `figure_images`, `legal_events`,
`field_provenance`, `applications`, `sources`, `seedpub`, `bench_emb_1024`, `bench_emb_3072`.

`families` does not exist in the pilot schema (family identity is `publications.family_id`). It is
listed so a table that appears later cannot appear unprotected.

### What is refused

Any `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `COPY ... FROM`, `TRUNCATE`, `ALTER TABLE`,
`DROP TABLE`, `CREATE INDEX ... ON`, `REINDEX`, `CLUSTER`, `VACUUM`, `LOCK TABLE`, `GRANT` or
`REVOKE` naming a protected table. Also `SET default_transaction_read_only`,
`SET session_replication_role`, `SET ROLE` and `SET SESSION AUTHORIZATION`, which would be the way
to undo the prohibition from inside SQL.

Fail closed: a statement that contains a write verb whose target table the matcher cannot read is
refused, not allowed. An unparsed write is exactly the case a permissive default lets through.

`SELECT ... FOR UPDATE` is treated as a read (it is a row lock inside a SELECT); the writes it
guards are refused on their own.

### The one escape hatch

```python
with corpus_guard.allow_corpus_writes("nightly ingest of release 2026-09"):
    ...
# or, with a cursor in hand:
with db.ingest_cursor("nightly ingest of release 2026-09") as cur:
    ...
```

Thread scoped, `reason` required. `grep -rn allow_corpus_writes src/ ops/` enumerates every
exception in the tree. Nothing in the search path may use it.

### The stronger option, not applied

A non-superuser role is available and is stronger, because it does not depend on this process:

```sql
CREATE ROLE patents_search LOGIN PASSWORD '<from the advisor>';
GRANT CONNECT ON DATABASE patents TO patents_search;
GRANT USAGE ON SCHEMA public TO patents_search;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO patents_search;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON
    publications, chunks, classifications, citations, parties, claims, paragraphs,
    figures, legal_events, field_provenance, applications, sources
    FROM patents_search;
GRANT INSERT, UPDATE, DELETE ON search_runs, search_stages, search_queries, retrieval_hits,
    search_candidates, provider_usage, shard_leases, corpus_ingest_queue, sources_docstore
    TO patents_search;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO patents_search;
```

NOT APPLIED. The app connects as `patents`, which is a superuser and therefore bypasses every
grant, so switching would need a new credential in the advisor and a change to every deployment's
`.env`. Recorded here so the decision is a decision and not an oversight.

## Where demand fetched material goes instead

Two places, neither of them the live vector tables.

1. **The scratch store**: `sources_docstore` (`src/sources/docstore.py`, `sql/008`). Already exists
   and already does merge-never-overwrite of compressed claims and description text keyed by
   publication number. It is not a retrieval table, nothing indexes it for vector search, and
   writing it never touches the HNSW graph.
2. **`corpus_ingest_queue`** (`sql/009`): the request that this publication be added to the next
   PERMANENT corpus release. A repeat request bumps `request_count`, which is the demand signal
   the release process ranks by.

## THE SEAM for the demand-fetch workstream

Nothing below is used by workstream A. It exists so the demand-fetch path has one place to land.

```python
import runstore
from sources import docstore

# 1. put the fetched text in the scratch store (never in chunks)
docstore._put_sync(pub, {"source": "epo:ops", "claims": claims_text,
                         "description": description_text, "abstract": abstract,
                         "title": title})

# 2. ask for it to be in the next permanent release
runstore.queue_for_ingest(
    pub,
    run_id=run_id,          # optional; the run that wanted it
    reason="read set: corpus holds no claims and no paragraphs",
    source="epo:ops",
    scratch_ref=f"sources_docstore:{pub}",
    payload={"claims_chars": len(claims_text), "desc_chars": len(description_text)},
    priority=100)           # lower runs first
```

Signature:

```python
runstore.queue_for_ingest(publication_number, *, run_id=None, reason="", source="",
                          scratch_ref="", payload=None, priority=100)
    -> {"id": int, "request_count": int, "state": "pending"|"claimed"|"ingested"|"rejected"}
```

The offline release side:

```python
runstore.pending_ingest(limit=500)          -> [row, ...]   ordered by priority, demand, age
runstore.claim_ingest(limit=100)            -> [row, ...]   FOR UPDATE SKIP LOCKED, state->'claimed'
runstore.mark_ingested(pubs, corpus_release="2026-09", note="")
```

The reader still needs the text DURING the run. That is the part workstream A did not build: the
in-memory hand-off from the scratch store to `deep_analysis.full_text`. Under the durable worker,
`deep_rank._enrich_missing_text` currently catches the refusal per reference and the reference is
listed rather than read, the same result as an unreachable source. The legacy in-process web path
is not armed and therefore retains its previous write behaviour until cutover. Making the scratch
copy readable is the demand-fetch workstream's job; `docstore._get_sync(pub)` is where it already
lives.
