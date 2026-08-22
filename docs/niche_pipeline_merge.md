# Absorbing the `patentdata` niche pipeline

A Codex session named `patentdata`, running on `grabo-tech`, independently built a niche corpus
acquisition pipeline and pushed it as `Nimo/niche-corpus-pipeline` (PR #27, 42 files, 47,914
insertions). It covers ground workstreams B and C had already merged into the V3 tree. This
document is the per area judgement: what was taken, what was rejected, and the reason in each
case. Every number in it was measured on this branch and the command that produces it is named.

The rule applied throughout: **the implementation that has run at scale wins a tie, and a
measured result beats a better shape.** Their pipeline has never run. No `niche_corpus` schema
exists on the live database, none on `grabo-tech`, there is no staging database and no object
store, and `artifacts/niche_corpus_status.json` is a discovery report rather than a fetched
corpus. C's two workers have fetched over 17,000 publications into GCS, `sources_docstore` and
`corpus_ingest_queue`, and are running now.

## What their discovery result was actually worth

`ops/niche_reconcile.py` replays their bounded discovery read only, then classifies every
publication against B's manifest release and C's pool. Full numbers in
`data/niche_reconcile/report.json`; the reconciliation itself is in the workstream report.

Their 16,896 publications split, by evidence measured here rather than by the label they carry:

| Reason | Of all 16,896 | Of the 5,403 B does not hold |
|---|---:|---:|
| `b_boundary`, B's own rule admits it | 10,976 | **0** |
| `cpc_outside_b`, a real classification hit under their prefixes only | 119 | 98 |
| `terminology`, a real niche term in the title or abstract | 211 | 151 |
| `unclassified`, no CPC and no IPC at all | 227 | 178 |
| `graph_only`, no evidence beyond having been reached | 5,363 | 4,976 |

The zero in the first row is the single most useful thing the reconciliation produced. Every
publication their discovery found that B's boundary rule admits, B already holds. B's enumeration
is complete with respect to its own rule, and the disagreement between the two is entirely a
disagreement about where the boundary is, not about who enumerated it properly.

## The judgement, per area

| Area | Theirs | Ours | Taken | Why |
|---|---|---|---|---|
| Graph discovery | `corpus/niche/discover.py`, `domains.py`, `providers/local.py`: bounded primary key window, expands through family, citation and co-classification, admits publications with no CPC | B's `corpus_niche.Boundary`: a measured CPC rule plus family and X/Y citation closures, run in bulk | **theirs, as an audit probe** | It is the only mechanism in the tree that walks the corpus graph incrementally under a read only guard, and it found 5,403 publications B's rule does not reach. It is not the boundary: B's is measured against evidence density and theirs is a hand written prefix list. Kept for what it is good at, which is asking B's boundary a question it cannot ask itself. |
| Read only source access | `SET TRANSACTION READ ONLY`, `SET LOCAL statement_timeout = '15s'`, `guard_read_only_sql()` refusing any statement that is not `SELECT`/`WITH`/`SHOW`/`EXPLAIN`, and a required `NICHE_SOURCE_DATABASE_URL` that never defaults | `corpus_guard.arm()` in the worker process | **theirs, kept intact** | Belt and braces at a different layer. `corpus_guard` refuses a write from inside the process; theirs refuses it at the transaction. `ops/niche_reconcile.py` depends on this: it is what makes replaying their discovery against the live 62 GB box safe. |
| Provider cascade | `waterfall.ProviderWaterfall`, 9 adapters, sequential, in process `PaidBudget` | `acquire.providers` + `worker.cascade_for`, 9 rungs, async, per rung `Gate` with concurrency, minimum interval, timeout and breaker, Postgres budget ledger | **ours** | Three reasons, in order of size. (1) Ours has the shared upstream settle rule: a rung that reached Google Patents and found no full text settles the question for every other rung reading the same page. Measured before it existed, 1,018 old FR/SE/GB/NL/AT documents cost 15,480 ScrapingBee credits and the whole $4.58 SerpApi budget re-confirming a nothing. Theirs has no equivalent and would repeat it. (2) Their `PaidBudget` is constructed per run from `MAX_*_CREDITS_PER_RUN`, so the cap resets every restart and four workers hold four copies of it; ours reserves against `fulltext_budget` in Postgres, so the cap is one monthly cap shared by every worker. (3) Ours is running. |
| Fatal error propagation | `PipelineFatalError` stops provider fallback immediately on a local durability failure | no equivalent | **noted, not taken** | A good idea. Ours currently treats a storage failure as non fatal on purpose (`blobstore` records the error and still writes `sources_docstore`, so a GCS outage never loses paid for text). The two policies are both defensible and switching is a behaviour change to a running worker, so it is recorded here rather than made. |
| Firecrawl adapter | `providers/firecrawl.py`, the one provider we did not have | absent | **ported, then measured and rejected** | Ported to `acquire.providers.FirecrawlProvider` and measured with `ops/firecrawl_probe.py`. Firecrawl returns the patents.google.com Angular shell: 596,384 bytes with no `itemprop=` attribute anywhere, so none of the sections that carry the claims or the description. Nine pool publications, 0 claim characters and 0 description characters each, including three the pool already holds in full through another rung, which is the control that says the zero is Firecrawl's and not the documents'. `waitFor: 5000`, `formats: ["markdown"]` and `proxy: "stealth"` all return the same shell. 10 credits spent in total. The rung stays registered in `build()` and out of `DEFAULT_ORDER`, with the measurement in its docstring so nobody spends those credits again. |
| Parsing | `parse.py`, 662 lines: ST.36 style XML, Google Patents HTML and JSON through one dispatcher, claim ancestry resolution with cycle detection, `merge_parsed` | `sources.gpatents_direct.parse_document` for HTML, `acquire.providers.parse_st36` for XML, claim dependency handling in `patent_text` and `ingest_pg` | **theirs, kept, not wired** | It is genuinely wider than either of ours taken alone, and its `google-src-text` stripping matches what `gpatents_direct` already does for the same reason. It is not on the live path and wiring it there means re-validating every rung's output shape against a parser that has never seen a real response, which is a larger change than this workstream should make to two running workers. Kept as the better reference implementation for whoever does that. |
| Work queue | `queue.PostgresFetchQueue`: per job lease, heartbeat thread, exponential backoff with jitter, `reclaim_expired` | `acquire.tasks`: `fulltext_fetch_task`, family partitioned, `FOR UPDATE SKIP LOCKED`, lease expiry, attempts incremented at lease time | **ours** | Theirs claims by priority with no partitioning, so two workers can take two siblings of one family and each do the other's work. That is precisely what the family partition in `tasks.partition_of` exists to prevent, and the corpus rung answering from a family sibling is what makes it matter. Ours also holds 322,718 rows of real state. |
| Object store | `storage.FileObjectStore` / `GCSObjectStore`, content addressed, write once with an ifGenerationMatch precondition | `acquire.blobstore`, GCS JSON API, `raw/` gzipped plus `parsed/` | **ours, and their dependency line reverted** | Their commit added `google-cloud-storage==2.19.0` to `requirements.txt`. That package is deliberately absent from the shared virtualenv, and production `patent-results` runs from that virtualenv, so installing it is a dependency resolution against a live service. `blobstore` does the same job over the JSON API with no new dependency and has stored every publication the pool has fetched. The line is reverted and `GCSObjectStore`'s import is documented as lazy and off the live path. Their content addressing and write once precondition are the better durability story and are worth taking if that module is ever revived. |
| Status reporting | `status.py`, JSON and CSV artifacts from the staging schema | `ledger.progress()` behind `ops/fulltext_acquire.py status` | **ours** | Theirs reads a schema that does not exist on any database. Ours reads the live pool, the per provider outcomes and the spend. |
| Staging location | dedicated `niche_corpus` schema, a separate staging database, a read only role for the source | `fulltext_*` in `public` on the live corpus database | **theirs is the better shape, and it is not adopted. Decision recorded below.** | See the next section. |

## Two defects fixed in the code that was kept

**The DOCDB `-1` family sentinel.** `identifiers.normalize_family_id` mapped only the empty string
onto the publication number, so a `simple_family_id` of `-1` became the family `family:-1`. DOCDB
writes `-1` to mean "this publication has no simple family"; workstream I measured 21,862 live
publications carrying it. This is not a counting error. `manifest.choose_family_fetch_targets`
keeps exactly **one** record per family key, so every other member of the bucket is dropped from
the fetch set. On the 16,896 publication replay, 3,888 publications carry a sentinel, so 3,887 of
them would never have been queued. Fixed, with
`tests/test_niche_manifest.py::test_the_docdb_no_family_sentinel_does_not_collapse_the_fetch_set`,
which returns 1 target instead of 3 when the fix is removed.

The same sentinel reached C's seeder from the other side: `acquire.tasks.seed` hashed
`fam or pn`, so `''` fell through correctly and `-1` did not. 3,779 of the 4,615 entries in the
gap manifest carry `-1` and would have landed in a single partition of 16. Fixed with
`tasks.partition_key()`, tested by
`tests/test_fulltext_acquire.py::test_the_docdb_no_family_sentinel_is_not_a_family`. Measured
after seeding: the 3,764 sentinel rows are spread across all 16 partitions, 211 to 260 each.

**The `terminology` label is not a terminology match.**
`providers/local.py::_publication_rows` assigns `signal = "terminology"` in an `else` branch,
taken whenever neither a CPC prefix nor an IPC prefix matched. It never reads the title or the
abstract. 7,444 of the 16,896 carry that label. Combined with `family_members`, `citations` and
`co_classified` all calling `_publication_rows(include_all=True)`, which skips `in_niche()`, and
with `in_niche()` returning True for any record carrying a graph signal, a graph reached
publication is admitted with no evidence test of any kind. That is a defensible discovery choice
and it is how they reach art with no CPC, but the label cannot be used as evidence. The label is
left alone, because their tests and their status artifact depend on it, and
`niche_reconcile.classify_gap()` measures the evidence separately instead. That measurement is
what turned "16,896 publications we do not have" into "5,403, of which 4,976 carry no evidence
beyond having been reached".

## The schema decision, and why the better shape was not adopted

Their `docs/niche-corpus.md` is right that staging belongs behind its own schema with a read only
role for the source, and right that a separate database is better still. Ours puts
`fulltext_fetch_task`, `fulltext_fetch_event`, `fulltext_budget` and `fulltext_manifest_cursor` in
`public` on the live corpus database, next to a 94 GB HNSW index and a production search path.

**It was not adopted. The reason is the migration ledger, and it is measurable.**

`sql/014_fulltext_acquisition.sql` is **adopted** on the live database with checksum `80af56ea`,
recorded 2026-08-22 17:06:34+00. Its bytes are frozen: editing it is `ChecksumDrift` and would
break `migrate.py` for every workstream, which is exactly the 006 incident. So a schema move has
to be a new migration running `ALTER TABLE ... SET SCHEMA` against four tables. Three things then
happen, and the first is the one that decides it:

1. **The ledger stops being able to confirm what it recorded.** `migrate.presence()` probes a
   table with `to_regclass(name)`, which resolves against `search_path`, and an index with
   `SELECT count(*) > 0 FROM pg_class WHERE relkind='i' AND relname=%s`, which is schema blind.
   Measured on the live database inside a rolled back transaction: after a move, the table
   sentinel returns **False** and the index sentinel returns **True**. 014 has four table
   sentinels and six index sentinels, and all ten probe present today, so `presence()` is `all`.
   After the move it is **`partial`** and stays `partial` for ever. `docs/migrations.md` records
   what a permanent `partial` costs: it is the 008 defect, "blocked adoption permanently for a
   reason nobody could see". An adopted migration whose objects the runner can no longer find is
   worse than an ugly schema.
2. `ALTER TABLE ... SET SCHEMA` takes `ACCESS EXCLUSIVE` on `fulltext_fetch_task`, which two
   Supervisor workers lease from every 36 seconds. Doing it live either blocks behind their leases
   or requires stopping them, and stopping them is forbidden unless they are being replaced by
   something already tested and better.
3. 34 references to those four table names across `src/`, `sql/`, `tests/` and `docs/` move with
   it.

**Recorded decision: not now, and not as a repair to 014.** The right moment is when the pool is
next rebuilt from scratch, or when the staging database their design actually asks for is created
and the pool moves onto it wholesale. At that point the tables are created in the new schema by a
new migration and nothing is altered, so none of the three costs above is incurred. What their
design should be credited with, and what should carry over unchanged when that happens, is the
separate database with a source role that cannot write, which is strictly stronger than a schema
on the same box.

## The migration collision

`sql/010_niche_fetch_queue.sql` collided with workstream B's `sql/010_corpus_release.sql`. Both
used `CREATE TABLE IF NOT EXISTS`, so whichever ran first would have silently won, and B's 010 is
in the ledger's pending set. `migrate.discover()` treats two files at one version as a hard error,
which is the behaviour we want, and it raised `DuplicateVersion` on this branch until it was
fixed.

Theirs is renumbered **018**, `sql/018_niche_corpus_staging.sql`, recorded in
`docs/migrations.md`. It is not applied and must not be: it creates a schema for a staging
database that does not exist yet. `src/corpus/niche/cli.py`, `tests/test_niche_pipeline.py` and
`docs/niche-corpus.md` follow the rename. Neither 010 was edited; both have or expect a ledger
row and an applied migration is history.

`tests/test_migrate.py::test_the_real_sql_directory_has_no_duplicate_version` runs `discover()`
over `sql/` as it is actually checked in, which the pre-existing duplicate test could not do
because it builds its own `tmp_path`. Put any second file back at any existing number and it goes
red again.
