# Continuous full-text acquisition

Status: running, two shards. Supervisor programs `patents-fulltext-acquire` (`--shard 0 --of 2`)
and `patents-fulltext-acquire-1` (`--shard 1 --of 2`), logs
`/home/nimrod_rotem/v3-logs/fulltext_acquire_0.log` and `_1.log`.

Measured over ten minutes with both shards up, 2026-08-22: **4,752 publications an hour**, 773 of
them answered by `serp_self` and 19 by `corpus:family`. Host load 1.06 on 8 vCPU against a 0.74
baseline, and `https://nimo.iptorch.com/` served in 38 to 81 ms against a 38 to 85 ms baseline, so
neither the database nor the public latency moved.

## The defect this closes

Text starvation in this system was circular, and it was measured. Enrichment fetched full text
only for the references a run had already chosen to read, and that choice was a screen score
computed from the text that had not been fetched. The attorney's single most comprehensive match
was screened 10/100 on a title alone, at retrieval rank 1,817. Separately, 14,379,018 description
paragraphs exist in the corpus and only 1,689,243 are chunked.

So acquisition had to stop being a side effect of reading. This is a continuously running fetcher
driven by a niche manifest, with its own pool, its own lease and its own cost ledger.

Measured on the live corpus, 2026-08-22:

| Number | Method |
|---|---|
| 78,061 publications carry one of the eight seed CPC subgroups | `SELECT count(DISTINCT publication_id) FROM classifications WHERE symbol = ANY(SEED_CPC)`, Bitmap Index Scan on `ix_class_symbol`, 0.3 s |
| **52,176 of them (66.8%) hold no claims and no paragraphs at all** | the same set, anti-joined against `claims` and `paragraphs`, 1.2 s |
| 22,099 of those (42.4%) have a family sibling that DOES hold claims | anti-join plus `EXISTS` over `simple_family_id` |
| CN 25,601, KR 7,484, JP 3,749, DE 3,426, TW 2,070, GB 1,197, FR 1,189 | `GROUP BY country` over the starved set |

Three quarters of the starvation is CJK, which is also what `to_tsvector('english')` is blind to.

## Where the output goes, and nowhere else

```
gs://nimo-patents-fulltext/raw/{publication}/{provider}.{ext}.gz     what the provider returned
gs://nimo-patents-fulltext/parsed/{publication}/{provider}.json      the normalised record
sources_docstore                                     merge-never-overwrite, via docstore._put_sync
corpus_ingest_queue                                  via runstore.queue_for_ingest
```

Never `publications`, `chunks`, `claims`, `paragraphs`, `classifications`, `citations` or any
other live retrieval table. The worker calls `corpus_guard.arm()` before it does anything else, so
the prohibition is a property of every connection the process opens rather than a convention:
`tests/test_fulltext_acquire.py::test_worker_run_arms_the_corpus_guard` opens a real cursor inside
the armed process and asserts that `INSERT INTO chunks` and `UPDATE publications` both raise
`CorpusWriteBlocked` before Postgres is asked. Deleting the `arm()` call turns that test red.

The bucket `gs://nimo-patents-fulltext` was created for this, US-CENTRAL1, uniform bucket level
access, public access prevention enforced. 58 KB per publication measured over the first ten
(gzipped HTML plus the parsed JSON), so the full 52,176 is of the order of 3 GB.

## The cascade

| # | Rung | Scope | Cost | State |
|---|---|---|---|---|
| 0 | `corpus` | anywhere | free | live. Answers from the publication itself, then from a family sibling |
| 1 | `marec` | EP, WO | free | INERT: no mirror exists, `MAREC_ROOT` unset |
| 2 | `uspto_bulk` | US | free | INERT: no mirror exists, `USPTO_FULLTEXT_ROOT` unset |
| 3 | `epo_ops` | EP, WO | free inside 4 GB/week | live |
| 4 | `pqai` | US | free, not quota counted | live |
| 5 | `serp_self` | anywhere | free | live. Google Patents from our own IP |
| 6 | `scrapingbee` | anywhere | 15 credits/page | live, budgeted |
| 7 | `himmpat` | CN, JP, KR, TW | metered trial key | live, budgeted |
| 8 | `serpapi` | anywhere | $0.0092/document | live, budgeted, LAST |

`FULLTEXT_CASCADE` (comma separated provider names) overrides the order, which is how a rung is
turned off without a deploy.

### Two deviations from the brief's ordering, both on cost

**SerpApi is last, not fifth.** The brief lists SerpApi at rung 5 and "the other adapters" at rung
6 while also calling SerpApi the last resort; those cannot both hold. ScrapingBee and SerpApi fetch
the SAME Google Patents page. SerpApi bills $0.0092 a document with 12,501 searches left of 30,000
this month; ScrapingBee bills 15 credits against 829,464 left of 1,000,000, renewing 2026-09-02,
which is about 55,000 pages. The paid channel is strictly more expensive for an identical document,
so it sits at the bottom.

**PQAI sits with the official APIs, at rung 4.** It is a free API with a published quota whose
per-publication route is not counted at all, and it is US-only, so it costs nothing and cannot
collide with the EP/WO rung. Putting it below `serp_self` would mean risking an IP block on
documents a free API already serves.

### MAREC

MAREC is a licensed research collection distributed by the Information Retrieval Facility, which
no longer operates. `sudo find / -maxdepth 4 -iname '*marec*'` on the patents VM returns nothing
and there is no MAREC credential in the advisor. The rung is therefore built and tested against
the mirror layout (`{root}/{PUB}.xml`, `{root}/{CC}/{PUB}.xml`, `.xml.gz`, local path or
`gs://bucket/prefix`) and reports `MAREC_ROOT is not set` until a mirror exists. Its practical
substitute for EP and WO is `epo_ops`, which is wired and live.

The USPTO's own equivalents do exist: its bulk product API lists `PTGRXML` (Patent Grant Full-Text
Data, No Images, XML) and `APPXML` (Patent Application Full-Text Data, No Images). They are weekly
ZIP archives rather than a per-publication API, so `uspto_bulk` shares the same mirror shape and
the same inert default. Verified against the live key on 2026-08-22: ODP has no per-publication
full-text route (`/patent/publications/{pub}` and `/patent/applications/{app}/full-text` both
403), which is why the US rung in practice is PQAI.

## The pool, the lease and the dedup

`fulltext_fetch_task`, one row per publication, `publication_number` as the PRIMARY KEY. That
primary key IS the dedup: a publication is in the pool exactly once however many times a manifest
names it, and seeding is `ON CONFLICT DO NOTHING`, so re-seeding is free.

`partition_id` is assigned at seed time from `md5(family_id or publication) % 16`. Family scoped,
because the corpus rung answers from a family sibling and two workers racing to fill in siblings of
one family would each do the other's work. `md5`, not `hash()`, because `PYTHONHASHSEED`
randomises string hashing per process and the partition would move under the worker's feet.

A worker takes `--shard i --of n` and owns `[p for p in range(16) if p % n == i]`. Disjoint by
construction and their union is every partition, so 1 to 16 workers run without ever reseeding.

Claiming is `FOR UPDATE SKIP LOCKED` over a partial index of pending rows only. `attempts` is
incremented at LEASE time, not at completion, so a publication that reliably kills its worker is
retired to `failed` after 3 attempts instead of poisoning the pool. `reap()` returns an expired
lease to the pool. `complete()` updates only where `lease_owner` is still this worker: a worker
that lost its lease mid-fetch keeps the text it fetched (the docstore merges, so that is never a
loss) but does not get to overwrite a row somebody else now owns.

## Rate limits, timeouts and the breaker

Enforced in the worker, not trusted from the provider. Every rung runs inside a `Gate`: a
semaphore, a minimum interval between calls, a wall-clock `asyncio.wait_for`, and a breaker that
opens for five minutes after eight consecutive failures. There is also a per-publication deadline,
so one publication cannot absorb the sum of every rung's timeout. A provider that hangs costs one
publication one rung's timeout and is then skipped for the whole partition.

`serp_self` additionally honours `gpatents_direct`'s own block latch. Measured 2026-08-14: a burst
of about 80 requests at concurrency 8-20 ran at 50 documents a second with every response 200, and
then the whole IP took a plain 503 on both the document pages and the XHR search, still refusing
2.5 minutes later. It is therefore paced at 2 in flight with 1.5 s between calls in production, and
when it trips its cooldown `available()` returns False and the cascade falls straight through to
`scrapingbee`.

## The budget is reserved, not counted afterwards

A cap in a process variable is four caps when four workers run, and a cap checked after the call
has already been exceeded. `ledger.reserve()` is one atomic UPDATE carrying the cap in its WHERE
clause; it either returns the new spend or refuses. `ledger.refund()` puts the reservation back
when the call did not happen, so a timeout does not leak budget.
`tests/test_fulltext_acquire.py::test_budget_is_a_hard_cap` goes red if the cap is dropped from
that UPDATE.

Production caps (Supervisor `environment=`): SerpApi 500 credits a month, ScrapingBee 300,000
credits (20,000 pages), HimmPat 150.

## The manifest seam

Workstream B owns the niche manifest. At the time this was built, branch `v3/B-corpus-manifest`
did not exist on the remote and no contract was published, so `manifest.ManifestReader` is the
interface and there are two implementations:

* `CorpusNicheReader` (`corpus-niche`, the default) is provisional: the publications the corpus
  already holds in the eight seed CPC subgroups that have no claims and no paragraphs. Keyset
  paginated on `publications.id` and driven by `ix_class_symbol`, so it never sequentially scans
  the 5M row corpus.
* `JsonlManifestReader` reads one JSON object per line over a file, a glob or a directory, and is
  generous about field names (`publication_number` / `publication` / `pub_number` / `pub`,
  `family_id` / `simple_family_id` / `family` / `docdb_family_id`).

Swapping to B's contract is a new subclass and one line in `open_reader()`. The cursor lives in
`fulltext_manifest_cursor`, so seeding is incremental and a manifest that grows is picked up by
re-running the seeder rather than by rebuilding the pool.

## Operating it

```
ops/fulltext_acquire.py ensure-schema             apply sql/014 (all IF NOT EXISTS), once
ops/fulltext_acquire.py ensure-bucket             create the GCS bucket
ops/fulltext_acquire.py seed [--manifest SPEC]    pull the next slice of the manifest into the pool
ops/fulltext_acquire.py status                    pool, per-provider outcomes, spend, rate
ops/fulltext_acquire.py quota                     what the paid providers say they have left
ops/fulltext_acquire.py run --shard 0 --of 1      the worker (this is what Supervisor runs)

sudo supervisorctl status  patents-fulltext-acquire
sudo supervisorctl stop    patents-fulltext-acquire     finishes the batch, releases the leases
sudo supervisorctl restart patents-fulltext-acquire
```

Scaling to four workers: copy `ops/patents-fulltext-acquire.conf` to `-1`, `-2`, `-3` with
`--shard 1/2/3 --of 4`, AND change `--of` on shard 0 to 4 in the same edit. Leaving one worker at
`--of 1` while another runs `--of 4` is the one mistake that puts two workers on the same
partition.

## The most expensive thing the ledger caught

`serp_self`, `scrapingbee` and `serpapi` all read the Google Patents index. When the free rung
fetches the page and the page carries no claims and no description section, the two paid rungs
fetch the identical page and produce the identical nothing. Measured on 2026-08-22, before the
fix: 1,018 publications, almost all of them pre-digital FR (335), SE (200), GB (189), NL (68), IT
(54), AT and AU documents, cost **15,480 ScrapingBee credits and the entire $4.58 SerpApi budget**
confirming what the free rung had already established.

Providers now declare an `upstream`. A rung that REACHED its upstream and found no full text
settles the question for every rung reading the same upstream, and the cascade records a `settled`
event instead of buying it again. The distinction that makes this safe is `FetchResult.reached`:
Google answering 404 means it does not hold the publication, which settles it; Google answering
503 means it refused us, which is exactly the outage the paid rungs exist for. Both halves are
defect injected (`test_a_settled_upstream_is_not_bought_twice`,
`test_an_unreached_upstream_still_falls_through_to_the_paid_rung`).

The SerpApi cap is what contained this to $4.58 rather than to the 12,501 searches left on the
account. It is raised to 1,500 credits after the fix, of which 500 are already spent.

## Two collisions this fetcher caused, and how they are settled

**Bulk demand must queue behind a live search.** `runstore.pending_ingest(limit=N)` returns the top
N rows by priority, and a search-time request takes `corpus_ingest_queue`'s default of 100. This
fetcher first queued at 80, which put tens of thousands of rows in front of every live request and
pushed them out of the window: `test_durable_runs.py::test_repeat_demand_for_one_publication_bumps_the_count`
went red on exactly that. `worker.INGEST_PRIORITY` is now 150 and the 1,611 rows already queued
were repriced. `test_bulk_demand_queues_behind_a_live_search_request` holds the invariant.

**A migration list cannot be a literal.**
`test_migrate.py::test_the_repo_migrations_are_discoverable_and_include_figure_images` asserted
that the repo holds exactly 001 to 009, so any new migration turned it red for the one reason that
is not a defect. It now asserts the invariants instead: 001 to 009 is the leading baseline,
discovery is in numeric order, and no two files claim one version.

## What another workstream needs to know

`runstore.claim_ingest(limit=100)` takes ANY pending row in `corpus_ingest_queue`, and
`tests/test_durable_runs.py:401` calls it with `limit=50` on every run of that file. Ten rows this
fetcher queued between 15:16:43 and 15:16:53 on 2026-08-22 were in state `claimed` a few minutes
later with no release process running. A test suite on any branch therefore silently consumes the
demand signal the release process is supposed to rank by. The rows and their payloads survive; the
`pending` state does not.
