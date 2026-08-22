# Niche full-text patent data factory

This is an independent acquisition and indexing workstream for vacuum, suction,
gripping, lifting, handling, conveying, and robotic-manipulation patents. It
does not write to the production publications, claims, paragraphs, chunks,
retrieval indexes, or application code.

The durable control plane is the `niche_corpus` schema in the isolated
`niche_full_v1` PostgreSQL database. Raw and canonical objects are permanent in
Google Cloud Storage. The fetch workers may use the existing acquisition queue
and budget ledger, but those tables are staging infrastructure and never become
retrieval data directly.

## Streaming architecture

Each stage runs continuously and can be restarted independently:

1. `discover` scans bounded, indexed source-ID ranges using read-only
   transactions. It persists manifest rows and per-range watermarks.
2. The manifest bridge keyset-pages new or changed manifest rows and seeds one
   preferred incomplete publication per family into the shared acquisition
   queue.
3. `fetch` leases acquisition work across VMs. It reuses cached objects, follows
   the provider waterfall, records provider cost, and signals the isolated
   parse queue after a durable source is available.
4. `parse` leases raw or local sources, writes canonical JSON, preserves claim
   dependencies and description paragraph boundaries, and stages
   embedding-ready chunks.
5. `embed` submits and reconciles Gemini Batch jobs. It deduplicates identical
   content and enforces one database-global dollar budget.
6. `publish` atomically copies completed chunk occurrences and vectors into the
   isolated `niche_vector_documents` table. Its HNSW index stays online as rows
   arrive.
7. `tantivy_build` incrementally updates the persistent BM25 index. `status`
   writes continuous publication and family completeness artifacts.

Discovery classifications are signals, not inclusion walls. Citation-only,
family-expanded, co-classified, and terminology-matched publications remain in
the manifest even when no CPC or IPC code is present.

## Isolated schema

Apply these migrations in order, only to `niche_full_v1`:

- [001_fetch_queue.sql](../sql/niche/001_fetch_queue.sql): manifest, discovery
  watermarks, provider attempts, source objects, and fetch jobs.
- [002_streaming_embedding.sql](../sql/niche/002_streaming_embedding.sql):
  database identity, parse queue, canonical chunks, shared embedding budget,
  embedding cache, and batch state.
- [003_manifest_stream.sql](../sql/niche/003_manifest_stream.sql): indexed
  manifest bridge cursor.
- [004_search_build.sql](../sql/niche/004_search_build.sql): vector documents,
  HNSW, and incremental Tantivy publication state.

Every writer validates both `NICHE_EXPECTED_DATABASE` and
`NICHE_DATABASE_FINGERPRINT` against the database marker before doing work.
This prevents an accidentally supplied production DSN from becoming a write
target.

## Required configuration

Common staging configuration:

```bash
export NICHE_DATABASE_URL='postgresql://.../niche_full_v1'
export NICHE_EXPECTED_DATABASE='niche_full_v1'
export NICHE_DATABASE_FINGERPRINT='niche-full-v1-20260822'
export NICHE_SOURCE_DATABASE_URL='postgresql://.../patents?options=-cdefault_transaction_read_only%3Don'
export NICHE_CANONICAL_OBJECT_URI='gs://YOUR_BUCKET/niche_full_v1'
export MAX_FETCH_ATTEMPTS_PER_PUBLICATION=5
```

The source role should be read only. Runtime source transactions also execute
`SET TRANSACTION READ ONLY`. Source scans use indexed ID ranges, bounded
windows, persisted watermarks, and a configurable delay.

Gemini Batch configuration:

```bash
export GEMINI_EMBED_MODEL='gemini-embedding-001'
export GEMINI_EMBED_DIMENSION=768
export GEMINI_EMBED_TASK_TYPE='RETRIEVAL_DOCUMENT'
export NICHE_CORPUS_RELEASE='niche_full_v1'
export GEMINI_EMBED_BUDGET_KEY='niche_full_v1'
export MAX_GEMINI_EMBED_USD_TOTAL=400
export GEMINI_EMBED_PRICE_USD_PER_MTOK='...'
export GEMINI_BATCH_BUCKET='YOUR_BUCKET'
export GEMINI_BATCH_PREFIX='niche_full_v1/embed_batch'
export GCP_PROJECT='nimo-gpt'
export GEMINI_BATCH_LOCATION='us-central1'
```

The configured dollar limit must exactly match the pre-provisioned database
budget row. Missing, invalid, non-positive, or mismatched limits fail closed.
The deployed `niche_full_v1` build uses a $400 one-time lifetime ceiling. This
is a safety maximum, not a spend target. Actual and reserved cost are reported
continuously by the status command.
The controller reserves a conservative upper-bound cost transactionally before
calling Vertex. A restart adopts one exact provider-job match by deterministic
name, labels, model, input URI, and output prefix. It never blindly resubmits an
uncertain POST.

`gemini-embedding-001` can return its native 3072-dimensional vector even when
the batch request carries a smaller output dimensionality. The collector
therefore applies Matryoshka prefix truncation to 768 values and L2-normalizes
the result, as required for non-3072 dimensions. Fetch workers never call
Gemini.

## Discovery

Initialize once, then run non-overlapping ranges. Bounds are `(start, end]`, so
an end value belongs only to its lower range:

```bash
python -m src.corpus.niche.discover --init-schema --id-start 0 --id-end 3218196 --max-batches 0
python -m src.corpus.niche.discover --id-start 3218196 --id-end 6436391 --max-batches 0
```

A four-way layout is:

```text
(0, 1609098]
(1609098, 3218196]
(3218196, 4827294]
(4827294, 6436391]
```

Never overlap ranges. Before changing the number of workers, stop at persisted
watermarks and allocate the remaining intervals explicitly. Keep
`NICHE_DISCOVERY_ID_WINDOW`, graph expansion, result pages, concurrency, and
source-read delay bounded. Do not replace indexed keyset scans with `OFFSET`
or repeated full-table scans.

The systemd template accepts an explicit numeric range token, so a durable
remainder worker can be enabled as
`patents-niche-discovery@START-END.service`. Historical range watermarks are
retained for audit; the status report unions their covered intervals, so a
coordinated reshard does not inflate progress.

After the bounded snapshot reaches 100 percent, keep one low-frequency tail
worker active. It resumes after the completed snapshot maximum, checks the
indexed source maximum, and scans only newly appended ID ranges:

```bash
ops/run-niche-discovery.sh tail
```

The default idle interval is 300 seconds and is configurable with
`NICHE_DISCOVERY_TAIL_INTERVAL_SECONDS`.

## Manifest bridge and fetch workers

The continuous bridge reads manifest changes by
`(updated_at, publication_id)` and writes only to the existing acquisition
queue and budget-ledger tables. It skips complete families and deduplicates
family targets.

Two current acquisition workers use one non-overlapping partition layout:

```bash
NICHE_FACTORY_ISOLATED=1 FETCH_WORKERS=8 FULLTEXT_IN_FLIGHT=8 FULLTEXT_GCS_BUCKET=nimo-patents-fulltext FULLTEXT_FIRECRAWL_BUDGET=50000 FULLTEXT_SERPAPI_BUDGET=1500 FULLTEXT_SCRAPINGBEE_BUDGET=300000 FULLTEXT_HIMMPAT_BUDGET=150 .venv/bin/python -u ops/fulltext_acquire.py run --shard 0 --of 2

NICHE_FACTORY_ISOLATED=1 FETCH_WORKERS=8 FULLTEXT_IN_FLIGHT=8 FULLTEXT_GCS_BUCKET=nimo-patents-fulltext FULLTEXT_FIRECRAWL_BUDGET=50000 FULLTEXT_SERPAPI_BUDGET=1500 FULLTEXT_SCRAPINGBEE_BUDGET=300000 FULLTEXT_HIMMPAT_BUDGET=150 .venv/bin/python -u ops/fulltext_acquire.py run --shard 1 --of 2
```

Do not add `--shard 2 --of 3` while the old `--of 2` workers are running.
Expanding to a third VM requires one coordinated, graceful cutover to
`0/3`, `1/3`, and `2/3`. The exact third-worker command after that cutover is:

```bash
NICHE_FACTORY_ISOLATED=1 FETCH_WORKERS=8 FULLTEXT_IN_FLIGHT=8 FULLTEXT_GCS_BUCKET=nimo-patents-fulltext FULLTEXT_FIRECRAWL_BUDGET=50000 FULLTEXT_SERPAPI_BUDGET=1500 FULLTEXT_SCRAPINGBEE_BUDGET=300000 FULLTEXT_HIMMPAT_BUDGET=150 .venv/bin/python -u ops/fulltext_acquire.py run --shard 2 --of 3
```

Provider credits are governed by the shared PostgreSQL ledger, not multiplied
per VM. Each worker limits itself to eight concurrent publications. Leases,
heartbeats, exponential backoff with jitter, bounded attempts, and expired
lease reclamation make SIGKILL recovery safe.

A rejected heartbeat stops the provider waterfall before its next request and
cannot complete or fail the lost lease. If a provider response already arrived,
its source bytes are cached first. Graceful shutdown returns an owned fetch job
immediately and removes that interrupted claim from its attempt count.

`NICHE_FACTORY_ISOLATED=1` is mandatory for these workers. It validates the
dedicated staging database identity, requires permanent GCS storage and the
parse handoff, and prevents writes to legacy corpus or ingest tables.

## Provider waterfall and paid controls

The canonical provider order is:

1. Existing local corpus, read only
2. MAREC
3. USPTO official US sources
4. EPO OPS
5. Direct Google Patents adapters
6. Existing self-hosted search and fetch
7. Firecrawl deterministic page scrape
8. ScrapingBee without JavaScript, then JavaScript only if required
9. SerpApi
10. Other explicitly configured project adapters

Unsupported jurisdictions and disabled providers are skipped. A provider
failure is recorded and isolated to that rung. Firecrawl search is used only
when a publication URL cannot be derived, and crawl mode is not used for a
single page. ScrapingBee premium proxies are not enabled automatically.

The standalone PR #27 provider layer also fails closed unless these are valid
non-negative integers:

```bash
export MAX_FIRECRAWL_CREDITS_PER_RUN=0
export MAX_SCRAPINGBEE_CREDITS_PER_RUN=0
export MAX_SERPAPI_REQUESTS_PER_RUN=0
```

## Parse, chunk, and embed

Backfill existing GCS objects once, seed locally complete publications, and
then keep the durable parse pool running:

```bash
python -m src.corpus.niche.parse --enqueue-gcs --input-bucket nimo-patents-fulltext
python -m src.corpus.niche.parse --enqueue-local
python -m src.corpus.niche.parse --stream --workers 4 --lease-seconds 300 --heartbeat-seconds 30
```

Canonical JSON preserves original-language title, abstract, structured claims,
claim parents, `chain_complete`, description paragraphs, figure captions,
citations, classifications, dates, and source metadata. It never rewrites
technical text with an LLM.

Chunk kinds are exactly `abstract`, `claim_own`, `claim_resolved`,
`description`, and `figure_caption`. A `claim_resolved` chunk is emitted only
when deterministic ancestry is complete. Long fields are split at a fixed byte
limit without dropping text. Each occurrence retains publication, family,
language, claim number, source location, and SHA-256 content hash.

Start the independent batch controller:

```bash
python -m src.corpus.niche.embed run --poll-seconds 30
```

## Isolated search build

On the high-memory build VM:

```bash
python -m src.corpus.niche.publish --batch-size 5000
python -m src.corpus.niche.tantivy_build --index-dir /srv/niche_full_v1/tantivy --batch-size 10000 --threads 4
```

Vector publication uses `FOR UPDATE OF stage SKIP LOCKED` and an idempotent
upsert before setting `published_at`. The HNSW index uses cosine distance,
`m=16`, and `ef_construction=128`. Tantivy deletes and adds by the stable
`(corpus_release, chunk_id)` document key, commits, then marks the database row.
A crash between those actions safely replays the same document.

Take a disk or database snapshot at release boundaries. A crash-consistent
snapshot is recoverable through PostgreSQL WAL, while Tantivy commits its own
atomic segment metadata.

## Permanent objects

Raw objects are immutable and content-addressed:

```text
patents/raw/{authority}/{publication_number}/{provider}/{sha256}.{ext}
patents/raw/{authority}/{publication_number}/{provider}/{sha256}.metadata.json
```

The integrated acquisition bridge uses the existing full-text bucket root and
stores the same information as deterministic gzip plus a sidecar:

```text
raw/{publication_number}/{provider}/{stored_sha256}.{ext}.gz
raw/{publication_number}/{provider}/{stored_sha256}.metadata.json
```

Metadata includes provider, fetch timestamp, hash, media type, safe HTTP
metadata, source URL, and byte count. Authorization, cookies, API keys, and
confidential request headers are never stored.

Canonical objects and batch inputs are replaceable derivations of immutable
source objects:

```text
gs://<bucket>/niche_full_v1/parsed/{authority}/{publication_number}.json
gs://<bucket>/niche_full_v1/chunks/...
gs://<bucket>/niche_full_v1/embed_batch/...
```

## Status and operations

Write the current machine-readable artifacts and print the live queue summary:

```bash
python -m src.corpus.niche.status
```

The report includes source coverage, publication and family completeness,
authority, CPC, language, provider, fetch state, credits, rate, failures,
queue leases, parse state, embedding batches, vector publication, and BM25
state. Reports are written to:

```text
artifacts/niche_corpus_status.json
artifacts/niche_corpus_status.csv
```

Do not interpret an in-progress manifest as a complete universe. Check
`source_scan_complete` and every range watermark.

## Safety

- Never point the staging writer DSN at the production database.
- Never grant the source-corpus role write privileges.
- Never apply these migrations to live retrieval tables.
- Never run embeddings from a fetch worker.
- Never create a production index or write active chunks.
- Never restart production PostgreSQL, nginx, or `patent-results`.
- Keep the existing description backfill running.
- Check source DB load, I/O wait, lock waiters, and public-site latency before
  raising discovery read pressure.
- Stop workers gracefully. If a VM dies, let the durable lease expire and
  reclaim it elsewhere.
