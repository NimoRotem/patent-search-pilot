# Niche patent corpus pipeline

This is an independent acquisition workstream for vacuum, suction, gripping,
lifting, handling, conveying, and robotic manipulation patents. It does not
write to the production `publications`, `claims`, `paragraphs`, `chunks`, or
vector tables. It does not call an embedding model.

The durable state lives in the dedicated PostgreSQL schema `niche_corpus`.
Source documents and normalized outputs live in a filesystem prefix or a shared
Google Cloud Storage prefix. Multiple VMs may consume the same queue when they
share both the staging database and object prefix.

## Architecture

The pipeline has four independent stages:

1. `discover` scans bounded primary-key ranges from the read-only source corpus,
   applies CPC, IPC, and terminology signals, and expands through families,
   citations, and co-classifications. It persists its watermark after an
   idempotent manifest upsert.
2. `fetch` leases preferred incomplete or unnormalized family publications with
   PostgreSQL `FOR UPDATE SKIP LOCKED`. It checks cached raw objects, then follows
   the fixed provider waterfall. A heartbeat extends the lease while a job is
   active.
3. `parse` replays permanently cached sources without a provider request and
   emits canonical JSON plus Parquet or JSONL chunks.
4. `status` writes continuous publication-level and family-level completeness
   reports to `artifacts/niche_corpus_status.json` and `.csv`.

Discovery classifications are signals, not inclusion walls. Citation-only and
known-result publications can enter the manifest without CPC or IPC data.

## Schema

Apply [sql/010_niche_fetch_queue.sql](../sql/010_niche_fetch_queue.sql) only to
the independent staging database. It creates:

- `niche_corpus.niche_publications`
- `niche_corpus.niche_fetch_attempts`
- `niche_corpus.niche_source_objects`
- `niche_corpus.corpus_fetch_jobs`
- `niche_corpus.niche_discovery_watermarks`

The migration is idempotent. No statement in it names an active corpus table as
a write target.

## Required configuration

Set these on every worker VM:

```bash
export NICHE_DATABASE_URL='postgresql://.../niche_staging'
export NICHE_SOURCE_DATABASE_URL='postgresql://.../patents'
export NICHE_OBJECT_URI='gs://YOUR_BUCKET/YOUR_PREFIX'
export FETCH_WORKERS=8
export MAX_FETCH_ATTEMPTS_PER_PUBLICATION=5
```

`NICHE_SOURCE_DATABASE_URL` must use a database role that cannot write to the
active corpus. The local provider additionally opens every transaction as read
only and rejects mutation statements before execution.

The default filesystem object root is useful for one-machine development. Use
`gs://` storage for multiple VMs so a raw response fetched by one worker is
immediately reusable by all others.

Optional provider configuration:

```bash
export MAREC_ROOT='/mnt/marec'
export USPTO_ODP_KEY='...'
export OPS_CONSUMER_KEY='...'
export OPS_CONSUMER_SECRET='...'
export NICHE_SELF_SERP_URL='https://...'
export FIRECRAWL_API_KEY='...'
export SCRAPINGBEE_API_KEY='...'
export SERPAPI_KEY='...'
```

Secrets belong in the runtime secret store or service environment, never in
Git, command history, raw-object metadata, or request logs.

## Paid-provider limits

Paid providers are disabled unless every relevant run cap is explicitly set to
a non-negative integer. Missing, negative, or non-numeric values become zero.

```bash
export MAX_FIRECRAWL_CREDITS_PER_RUN=0
export MAX_SCRAPINGBEE_CREDITS_PER_RUN=0
export MAX_SERPAPI_REQUESTS_PER_RUN=0
```

Increase only the provider intended for that run. The shared in-process credit
ledger reserves the worst-case request cost atomically before any network call.
Firecrawl uses one deterministic page scrape. ScrapingBee first requests the
page without JavaScript and retries once with JavaScript only when a successful
page is too thin. Premium proxy mode and Firecrawl crawl mode are not used.

## Runbook

Initialize the staging schema, audit one bounded source range, and enqueue one
preferred incomplete or not-yet-normalized publication per family:

```bash
python -m src.corpus.niche.discover \
  --init-schema \
  --batch-size 1000 \
  --max-batches 1 \
  --db-read-delay 1.0 \
  --enqueue
```

Repeat the command to resume from the persisted source watermark. Keep batches
small while production description backfills or other large reads are active.
The implementation caps a source ID window at 250,000 rows, one seed batch at
5,000 matches, and each graph expansion at 20,000 publications even if an
environment variable or CLI value is larger.

Start eight workers on this VM:

```bash
FETCH_WORKERS=8 python -m src.corpus.niche.fetch \
  --workers 8 \
  --lease-seconds 300 \
  --heartbeat-seconds 30 \
  --poll-seconds 5
```

Run the exact same command on another VM after giving it the same staging DSN,
object URI, provider credentials, and paid caps. `SKIP LOCKED` prevents a second
active lease. A killed worker leaves its publication in the queue; another
worker reclaims it after lease expiry. Attempts are bounded and retries use
exponential backoff with jitter.

Parse cached sources again without fetching:

```bash
python -m src.corpus.niche.parse --limit 1000 --chunk-format parquet
```

Write and display the current report:

```bash
python -m src.corpus.niche.status
```

To drain only currently eligible jobs and exit:

```bash
python -m src.corpus.niche.fetch --once --max-jobs 100
```

`--max-jobs` is shared across all threads in the process. It is not multiplied
by `--workers`.

## Provider waterfall

The order is fixed:

1. Existing local corpus, read only
2. MAREC deterministic archive lookup
3. USPTO official US adapter
4. EPO OPS for EP and WO full text
5. Direct Google Patents document page
6. Explicitly configured self-hosted fetch adapter
7. Firecrawl deterministic page scrape
8. ScrapingBee no-JavaScript request, then one JavaScript retry if needed
9. SerpApi Google Patents Details

Unsupported jurisdictions and disabled providers are skipped. Empty, partial,
and failed results stay isolated to their provider rung. Every non-empty source
is persisted before parsing, even when a later rung is still required.
When the audit says local text is already complete but no normalized object
exists, the job is local-only. It creates the parsed and chunk objects without
falling through to any network provider.

## Permanent objects

Raw object keys are content-addressed:

```text
patents/raw/{authority}/{publication_number}/{provider}/{sha256}.{ext}
patents/raw/{authority}/{publication_number}/{provider}/{sha256}.metadata.json
```

Metadata includes provider, fetch time, content hash, media type, HTTP status,
safe response headers, source URL, and byte count. Authorization, cookies, API
keys, and confidential request headers are never stored.

Normalized and chunk outputs are replaceable derivations of immutable raw data:

```text
patents/parsed/{authority}/{publication_number}.json
patents/chunks/{authority}/{publication_number}.parquet
```

Claims keep their own text, independent/dependent flag, exact parent numbers,
resolved inherited text, and `chain_complete`. Ambiguous ancestry remains
unresolved with `chain_complete=false`. Description paragraphs retain their
boundaries, section, language, page, and source location. Technical text is
never rewritten by an LLM.

Chunk rows use only these kinds: `abstract`, `claim_own`, `claim_resolved`,
`description`, and `figure_caption`. Fetch workers do not call Gemini. The
resulting Parquet or JSONL files are inputs to a separate batch embedding stage.

## Current bounded audit snapshot

The committed status artifacts were generated on 2026-08-22 from read-only
production queries into a disposable local staging database. No provider
network fetches were run and no production row was changed.

The snapshot contains 16,896 publications in 6,215 families. Publication-level
complete claims are 30.03 percent and complete descriptions are 17.63 percent.
It has 11,822 publications missing complete claims, 13,918 missing complete
descriptions, and 14,323 missing at least one full-text component. Family-level
complete claims are 51.58 percent and complete descriptions are 24.83 percent.
Provider success rates are not yet applicable and paid credits spent are zero.

This is deliberately not represented as the complete universe. Its persisted
source watermark is publication ID 1,197 of 6,436,391, or 0.02 percent. Resume
`discover` to expand the manifest safely. The JSON artifact records both the
watermark and `source_scan_complete=false` so downstream reporting cannot
mistake the bounded audit for a finished source scan.

## Safety checks

- Never point `NICHE_DATABASE_URL` at the production database.
- Never grant the source-corpus role write privileges.
- Never run this migration against live retrieval tables.
- Never launch fetching without explicit paid caps.
- Never run embeddings from a fetch worker.
- Never increase source batch size or concurrency without checking database
  blockers, I/O wait, and public search latency.
- Stop workers gracefully with SIGTERM or SIGINT. If a VM dies, wait for lease
  expiry and start workers elsewhere.
