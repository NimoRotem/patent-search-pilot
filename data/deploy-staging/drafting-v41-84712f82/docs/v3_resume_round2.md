# V3 resume, round 2. State at 2026-08-22 20:15 UTC

Read `docs/v3_resume.md` first for rounds 0 and 1. This file covers the six workstreams run on
2026-08-22 evening and what is true about production right now.

**Both Claude accounts are at their weekly limit and reset 2026-08-24 06:00 UTC.** Five of the six
workstreams died mid-work with a single line of output, `You've hit your weekly limit`. Everything
is committed and pushed. Nothing is lost, but five of the six branches end on a `wip` commit rather
than a finished piece of work.

## Production, exactly as it stands

| | |
|---|---|
| Deployed worktree | `/home/nimrod_rotem/patent-search-pilot`, detached at **`755dba49`**, which is `v3/L-cutover` |
| Durable route | **OFF.** No `DURABLE_SEARCH_RUNS` and no `DURABLE_WORKER_ENABLED` in `.env`, so `webapp` falls back to the legacy in-process dispatcher, deliberately |
| Durable worker | **NOT RUNNING.** `patent-search-worker.conf` is in `/etc/supervisor/conf.d/` but was never `add`ed |
| Health | `/healthz` ok, `https://nimo.iptorch.com/` HTTP 200, Draft Studio actively generating figures |
| Live ledger | applied `012_run_admission`, `013_run_side_effects`; adopted `001 003 004 005 006 007 008 009 014`; **pending `002 010 015`** |

So the cutover is **deployed but not switched on**. That is a coherent, safe state and not a half
cutover: the new code is live, the durable path is dark, and the legacy dispatcher is serving. The
remaining step is the one that was never taken, and it is the one that matters:

1. set the durable flags in the deployed `.env`,
2. `sudo supervisorctl reread && sudo supervisorctl add patent-search-worker`,
3. start a real search and **restart `patent-results` while it runs** to prove it survives.

Do not skip step 3. Without it nothing has been demonstrated. Roll back by clearing the flags,
which returns the route to the legacy dispatcher without a redeploy.

**`002` still cannot be applied as written.** `bench_emb_3072.embedding` is `vector(3072)` and
pgvector 0.8.5 caps HNSW at 2000 dimensions, so `ix_bench3072_hnsw` can never be created anywhere.
Split the bench indexes into 016 first.

## The six branches

| Branch | State | What it landed |
|---|---|---|
| `v3/J-shards` | wip | **Eight shard VMs exist and are all TERMINATED.** Lifecycle and a real Tantivy proven end to end on a real VM |
| `v3/K-cjk` | wip | The CJK answer, and HimmPat structurally barred from bulk |
| `v3/L-cutover` | wip | Migrations applied, code deployed, cutover not switched on |
| `v3/M-patentdata` | **complete, rc=0** | Reconciled the sibling pipeline. Report: `~/v3/reports/M-patentdata.md` |
| `v3/N-embed` | wip | `patents-parsed-embed` running, submitting Gemini batch jobs |
| `v3/O-release` | wip | Release manifests, atomic activation and rollback tested |

### The two findings that change the plan

**1. CJK is not a 3,600 day problem. It is a 7.5 day problem, and it is already running.**

`docs/cjk_acquisition.md` on `v3/K-cjk`. Measured on the live pool over three hours: Google Patents
fetched from this box's own IP, already the `serp_self` rung, returns English machine translations
at **99.99% hit rate for CN** (9,359 of 9,360), 98.1% KR, 95.4% JP, averaging 23,000 description
characters. Sustained 5,320 / 4,751 / 4,498 hits in three consecutive hours, 2 refusals in 17,733
attempts, at no cost. 898,377 CJK-only families is about **7.5 days**.

**The "900,463 families route to HimmPat" figure was a labelling artefact, not a measurement.**
`corpus_niche.SOURCE_LADDER` stamped `himmpat` as `best_source` for any family whose members are all
CN, JP or KR. The actual cascade asks Google Patents four rungs earlier and in that same live run
HimmPat answered **45** publications against `serp_self`'s **16,392**.

The exposure is real and should be recorded rather than solved: Google Patents is one provider on
one IP with no contract. It is 99.99% today and can be 0% tomorrow, and the only fallback is
ScrapingBee at 15 credits fetching the identical pages. **There is no free non-Google route to CJK
full text**, and BigQuery `patents-public-data` holds **zero** `description_localized` and
`claims_localized` rows for CN and JP against 22M for US.

HimmPat is now barred from bulk by `src/realtime_only.py`, which **defaults to deny**: only
`webapp.py` and `runner/worker.py` call `enable()`, so every offline process is refused without
having to know the module exists. Two independent doors, the HTTP boundary and the provider
builder, both defect-injected in `tests/test_cjk_acquisition.py`.

**2. Cold to hot does not fit in 20 seconds and never will.**

`ops/shards/README.md` on `v3/J-shards`. The agent was not lying about readiness; the wake is simply
longer than the budget. Two deliberate consequences: the wake now starts at **run claim time** via
`shard_backend.prewake_subject()` rather than when a query needs the shard, and a domain that
prewake did not predict **takes a first-query miss on purpose**. `c4-highmem-16` was out of stock in
`us-central1-b`, so the fleet is spread across `-a`, `-c` and `-f`, and `wake()` returns the last
error per shard so a zone stockout is visible rather than silent.

### Other things worth carrying

- **The DOCDB `-1` sentinel bit twice more.** In the sibling pipeline `normalize_family_id` mapped
  only the empty string, so 3,887 of 16,896 publications would never have been queued for fetch. And
  `acquire.tasks.seed` hashed `fam or pn`, so 3,779 of 4,615 gap entries would have landed in one
  partition of 16. Both fixed and defect-injected on `v3/M-patentdata`.
- **Firecrawl is useless for Google Patents**: 0 claim characters and 0 description characters on 9
  publications, 596,384 bytes of HTML with no `itemprop` anywhere. Ported, measured, left out of
  `DEFAULT_ORDER`.
- **Do not move the `fulltext_*` tables into a schema by `ALTER TABLE ... SET SCHEMA`.** Measured:
  `migrate.presence()` probes tables with `to_regclass`, which is `search_path` sensitive, and
  indexes with `pg_class.relname`, which is schema blind, so 014 becomes permanently `partial`,
  which is exactly what `adopt` refuses. Create the tables in the new schema in a new migration
  instead.
- Migration numbers now: 009/012/013 durable, 010 corpus release, 011 reserved and unowned, 014
  acquisition, 015 draft turns, 016 the 002 split, 017 parse-and-embed, 018 niche staging, 019 free.

## Long running jobs, all healthy

| Program | State |
|---|---|
| `patents-desc-backfill` | 11h+, over 9M rows staged into `chunks_stage_v3` |
| `patents-fulltext-acquire`, `-1` | ~4,800/h combined, pool over 326,000 rows |
| `patents-parsed-embed` | submitting Gemini batch jobs of 50,000 |

**They have been stopped twice from `34.68.181.216`, which is grabo-tech**, where a separate Codex
session is building an overlapping pipeline. `/home/nimrod_rotem/OWNERSHIP.md` on the patents VM
explains who owns what. That session also restarts `patent-results`, which is the defect the durable
worker exists to fix, so the cutover is worth finishing for that reason alone.

## First commands on resume

```bash
cat ~/v3/reports/M-patentdata.md          # the only finished report
cat ~/v3/K-cjk/docs/cjk_acquisition.md    # the CJK answer
cat ~/v3/J-shards/ops/shards/README.md    # the wake finding and the fleet
~/v3/bin/agents_status.sh                 # what is alive
```

Then finish `v3/L-cutover`, because it is three steps from done and it fixes a defect that is
losing production searches now.
