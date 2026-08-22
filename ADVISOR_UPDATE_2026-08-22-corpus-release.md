# Advisor update: the offline corpus release builder, and whether eight shards are enough

2026-08-22. Workstream O of the V3 rebuild. Two things in this note need a decision or at least an
acknowledgement: the builder database now has a written-down address, and the shard sizing that
eight VMs are being provisioned against has been re-derived from scratch.

---

## 1. The sizing, because eight machines are being built right now

**Eight cold shards plus a hot tier fit the corpus, with about five times the headroom they need.**
Each domain shard carries 4.76M chunks: 18.4 GiB of index against a 96.9 GiB resident budget on a
124 GiB machine, and 43.0 GiB of disk against 230 GiB usable. The 124 GiB specification is
generous for what these will actually hold.

That answer was not previously known, though a file claimed it. The plan the fleet was being
provisioned against reported `fits: true` by comparing a **0.4% sample's** chunk mass against a
**whole-corpus** capacity: 142,148 against 25,911,302. The mass was understated 32-fold overall
and 426-fold for the unclassified population, which is the single largest home domain in the
corpus. The comparison tested nothing. It happened to come out true.

It has now been derived without sampling: all 4,984,254 publications, all 3,431,375 families,
exact per-family chunk counts read from the live corpus in bounded key ranges, plus the
description backfill's remaining work counted per publication.

```
chunks live today                              27,623,460
chunks already staged by the description backfill 8,992,335
description paragraphs with no chunk anywhere   3,576,933
POST-BACKFILL CORPUS                           40,125,612 chunks
  hot tier (the eight seed subgroups)           2,024,262
  eight cold domain shards                     38,101,350  ->  4,762,670 each
```

**The line to watch.** Eight cold shards stop fitting at 200,957,184 chunks, which is 3,582,124
fully texted publications, **72% full-text coverage of the corpus**. Below that, eight is right.
Above it, the fleet grows past eight or the vectors move to `halfvec`, which halves the HNSW and
would make disk rather than RAM the binding constraint. `halfvec` has not been measured for recall
and should not be adopted without measuring it.

Nothing needs to change today. Eight is correct, and the working detail is in
`docs/shard_sizing.md`.

## 2. Where the builder database lives

The release builder deliberately has no default database, because a default that happens to be
production is how an offline build puts an HNSW index on the live box. The consequence was that
its address existed only in one process's memory. It is now written down.

`CORPUS_RELEASE_DSN` points at a **PostgreSQL 17 cluster named `relbuild` on 127.0.0.1:5544** on
the patents VM, database `relbuild`, pgvector 0.8.6, loopback only. The secret is in
`patent-search-pilot/.env` and nowhere else, and that cluster's `pg_hba.conf` has a local trust
line, so `sudo -u postgres psql -p 5544` is a way back in if the secret is lost. Release snapshots
land in `/home/nimrod_rotem/v3-releases/`. Full detail in `docs/corpus_release.md` and a summary
in `condensed.md` section 17.

## 3. Three defects found and fixed, all of the same shape

Each of these looked like nothing and was quietly destroying information.

**The demand signal was being eaten by the test suite.** `runstore.claim_ingest()` took no filter,
so `claim_ingest(limit=50)` claimed the top fifty pending rows of whichever database the process
was pointed at, and `tests/test_durable_runs.py` called exactly that on every run while cleaning up
only the one row it created. Measured on the live queue: **549 rows** sitting in state `claimed`,
never ingested, with no release process running anywhere. Eleven runs of that file at fifty rows
each, less the one it did delete. Those rows are invisible to `pending_ingest`, so the demand
signal the release process is meant to rank by was being consumed by tests on any branch. A claim
now has to name its rows or name itself, and abandoned claims can be swept back onto the queue.

**Every release ever built failed its own verification.** The manifest recorded the number of
publications *selected* while a shard's self-check counts the ones that actually contributed a
chunk. `hot_v1` recorded 4,136 and held 3,909. A shard that cannot prove what it is serving would
either refuse to join the fan-out or, worse, be waved through. The manifest now records what the
database holds.

**A restored shard held no families.** The snapshot dumped the chunk partition and nothing else,
so a shard restored from it held every chunk, reported zero families, could not answer the
domain-to-release lookup the router needs, and failed its own verification. Members and domain
rows now travel with the release.

All three are covered by tests that were checked by removing the fix and watching them go red.

## 4. What exists now

A release is built offline, sealed, content-addressed, and switched over in one transaction that is
reversible in both directions. `hot_v2` was built end to end from staged data on 2026-08-22:
511,783 chunks over 7,749 families, HNSW in 100 s, Tantivy in 14 s, snapshot in 240 s, and it
verifies. It was activated, rolled back to `hot_v1`, and rolled forward again. A deliberately
smaller `hot_v3` was refused activation by the completeness gate, which named every metric that had
gone backwards before the switch rather than after.

`sql/010_corpus_release.sql` has **not** been applied to the live database and must not be until
the version-number collision with the niche pipeline is resolved. It runs only against the builder
cluster and against the throwaway databases the tests create.
