# V3 resume, round 3. State at 2026-08-22 22:30 UTC

Read `docs/v3_resume.md` and `docs/v3_resume_round2.md` first. This file records what changed on the
evening of 2026-08-22: the cutover was finished and PROVEN, the V3 corpus and retrieval work was
merged into the deployed line and served, and two defects were found that only exist once the
durable workers are actually running.

## Production, exactly as it stands

| | |
|---|---|
| Deployed worktree | `/home/nimrod_rotem/patent-search-pilot`, detached at `v3/P-live-integration` |
| Durable route | **ON.** `DURABLE_SEARCH_RUNS="1"` in `patent-results.conf`, `/healthz` reports `"source": "postgres"` |
| Durable workers | **RUNNING.** `patent-search-worker-quick` and `-deep`, both `add`ed and up |
| Health | `/healthz` ok, `https://nimo.iptorch.com/` HTTP 200 |
| Live ledger | applied `001 003 004 005 006 007 008 009 012 013 014`; pending `002 010 015 016` |

`v3/P-live-integration` branches from the deployed drafting line and merges `v3/H-integration`, so
production now serves BOTH programs' work: Draft Studio, and the V3 corpus and retrieval work that
had been sitting unmerged (B niche boundary and manifest, C full-text acquisition, G cold and
global retrieval tiers, I the DOCDB `-1` family sentinel).

**The cold and global tiers are deployed but inert, by design.** `cold.available()` and
`global_search.available()` are both false until a backend is registered, and only
`retrieval/testing.py` registers one. The eight shard VMs are still TERMINATED. So the deploy
carries the seams and changes no search behaviour until somebody wakes the fleet.

## The cutover is finished, and it was proven the only way that counts

Round 2 left three steps. All three are done.

1. `DURABLE_SEARCH_RUNS="1"` was already in `patent-results.conf` on disk but NOT in the running
   process, so the route was still legacy. `supervisorctl update patent-results`, which names one
   program, put it in.
2. Both workers were already `add`ed and running.
3. **A real search survived a restart of `patent-results` taken while it was mid-flight.** Run
   `adhoc-1950ef124829-1787430544-ec20a7`, claimed by the quick worker at 20:29:05 and at
   `stage=decompose` when `patent-results` was restarted at 20:29:54. It finished `done` at
   20:38:39 with **`attempts=1`**: not reaped, not resumed, simply untouched, because the app is no
   longer where the search runs. `patent-results` has been restarted 32 times today.

A second full search then ran end to end on the newly deployed code
(`adhoc-de7c5db64697`, quick lane, one attempt, about eight minutes).

## Two defects that only exist once the workers are live

### 1. Production was executing the test suite's rows

From the live quick worker's own log, within an hour of the cutover:

```
[worker] claimed test-resume-8b70bab362-... (slug=test-resume-8b70bab362 lane=quick attempt=1/3)
[profile test-resume-8b70bab362] concept depth=deep rounds=1 budget={...}
[worker] test-resume-8b70bab362-... FAILED after 2s -> None
```

The suite creates real rows in the real `search_runs` on purpose: the table has foreign keys into
the corpus and cannot live in a separate database. That was harmless while nothing was running that
could pick a row up. `admit_waiting` was admitting them too, and an admitted row counts toward the
lane's concurrency, so a fixture could hold a slot a real search was queued behind.

Fixed with a reserved slug prefix production cannot generate (`search_slug` returns `adhoc-<sha1>`,
the bench harness writes `bench-*`, the gold ids are a fixed hand-named list).
`runstore.ALLOW_TEST_SLUGS` defaults to False and `tests/conftest.py` opts the suite in once, so no
test needed changing and a new production entry point is isolated without knowing the flag exists.
`tests/test_run_store_isolation.py`.

### 2. The document that was READ was not the document the page CITED

Counsel's caution on the representative swap was right, and the divergence was already live. SIX
places resolved the family representative independently and only three passed the subject's date:
the screen, the reading top-up and the rescue were date-aware; the report page, the ranked list and
the ranked API were not. Measured on four real dated deep reports on this box, over each report's
first 180 ranked families:

| report | cutoff | read in full | families where the display disagreed with the reading | read refs the page showed as UNREAD |
|---|---|---|---|---|
| `adhoc-60085e96d7d0` | 2020-11-10 | 324 | 57 of 180 (32%) | 52 |
| `adhoc-c5830687f3ce` | 2021-08-02 | 268 | 64 of 180 (36%) | 49 |
| `adhoc-bad747ed6f77` | 2020-10-30 | 267 | 50 of 180 (28%) | 42 |
| `adhoc-d4bd75030e64` | 2024-09-09 | 318 | 26 of 180 (14%) | 25 |

Passing the date at the three blind sites is not enough on its own, for two reasons.
`_seed_families` REPLACES the representative for a document the examiner applied by number, and
that override lived only in the screen's local dict. And the corpus keeps growing, so a sibling
that lands next week can win the ordering and change which member an OLD report displays, under
quotes that were read from a different one.

So the choice is made ONCE and recorded. `webview.record_family_reps` writes `fam -> publication`
into the report after the seed override; `webview.reps_for` is the single entry point every stage
uses; `resolve_family_reps` takes a `pinned` list that outranks both readability and the date. The
newly generated report above recorded 2,496 representatives.

The filing path was already sound and stays that way: `verify_quotes` re-checks each quotation
against the stored full text of that document's own publication number, and `collapse_families`
keeps a whole document rather than transplanting rows between members, so a package is verified
against the document that is actually filed. `family_alternates` names the other members but
nothing substitutes one silently.

**On the second half of counsel's note:** fewer ANTICIPATED flags after the 112(d) fix is expected
and is already how the ledger behaves. A demotion is not a deletion: `claim_status` keeps the
finding as `adds_disclosed_by`, described in the code as the second half of a §103 combination and
what a reference gets cited for once the parent is met by something else. Nothing in the suite
asserts a minimum anticipation count, so the drop cannot read as a regression anywhere.

## Also in this round

`002_indexes.sql` no longer contains the bench HNSW indexes; they are in `016_bench_indexes.sql`,
without the 3072 one. `bench_emb_3072.embedding` is `vector(3072)` and pgvector 0.8.5 caps HNSW at
2000 dimensions, so that index can never be created, the whole file raised, and `run.sh`'s closing
`apply --only 002` failed on every fresh install. The two indexes 002 exists for, and which the
live search actually uses, were never built as a result.

The `orchestrator._run_phase` merge was a genuine three-way: the deployed line had added durable
checkpointing (`pass_key`, resume from the hit ledger, cancellation) and H had added the remote
lane for the cold tier. Both are in. The auto-merge also left two latent unpacking bugs, iterating
`tasks` and `pending` as two-tuples where a lane makes them three; both are fixed.

## Test state

**1 failed, 2043 passed, 145 skipped, 12m19s** on `v3/P-live-integration`. The single failure is
the known environmental one: `test_relevance_audit.py::test_cards_carry_server_rendered_content`
asserts some top card has a drawing on disk, and a `~/v3/*` worktree has 23 files in
`data/ops_images` against the deployed worktree's 921 because that image corpus is not in git. One
failure means green; two means yours.

**145 skipped is not all noise.** 134 of them are the `test_run_cutover.py` and
`test_worker_cutover.py` migration tests, which build a throwaway database on `127.0.0.1:5432`.
This VM has no local Postgres (the corpus is at 10.128.0.53), so they cannot run here at all,
with or without `PATENTS_TEST_PG_PASSWORD`. That is a real coverage hole in the migration DDL path
and it predates this round.

## What is still open

* The four checkpointed round-1 branches are still unmerged: `v3/A-durable-worker`,
  `v3/D-embed-pipeline`, `v3/E-shard-infra`, `v3/F-corpus-release`. Gitignored artifacts are
  archived at `~/v3/preserved/*.tgz`.
* `010` must still not be applied: workstream F's unmerged branch writes a different file at the
  same number. `015` and `016` are pending and unapplied, which costs nothing on the live box.
* The shard fleet is TERMINATED and the cold tier is inert until somebody registers a backend.
* Two agent programs still deploy to this one host. `~/OWNERSHIP.md`.
