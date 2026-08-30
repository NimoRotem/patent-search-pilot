# Resuming the V3 build

Written at the point eight parallel workstreams stopped: four finished, four were cut off
mid-sentence by a provider weekly limit that resets **2026-08-24 06:00 UTC**. Everything is
committed and pushed. Nothing is lost, but half of it is a checkpoint rather than a deliverable,
and a checkpoint that nobody can pick up is the same thing as lost.

This file is the pickup. It says, for each of the eight, what its branch holds, whether it is
finished, what its own author left open, and the one command to run first.

**Read `BRIEF.md` before touching anything.** It is the environment and the rules, and it has not
changed.

---

## 1. Where the work is

| Thing | Where |
|---|---|
| The VM | `nimo-iptorch-patents`, `10.128.0.63`. Run commands with `~/v3run '<cmd>'` |
| The worktrees | `~/v3/A-durable`, `B-corpus-manifest`, `C-fulltext`, `D-embed`, `E-shards`, `F-release`, `G-retrieval`, `H-integration`, `I-family` |
| From the builder box | `/home/nimrod_rotem/patvm/` is an sshfs mount of the VM's home, so `~/v3/X` is `~/patvm/v3/X`. Read, Edit and Glob work on it natively |
| Git | **always through `~/v3run`**, never through the mount. Git over a network mount is slow and can leave a half-written index |
| Tests | `~/v3run '~/v3/bin/pt ~/v3/<DIR> tests/test_thing.py -q'`. `pt` holds one of three flock slots so the database-backed suites do not pile up |
| Python | `.venv/bin/python` inside each worktree, a symlink to the shared venv. `.env` is symlinked in too |
| Live database | `10.128.0.53:5433`, database `patents`, user `patents`. Read freely; write almost never, see `BRIEF.md` |

`~/v3/bin/pt` runs the whole suite in about eight minutes.

---

## 2. The eight, at a glance

| | Branch | State | Integrated? |
|---|---|---|---|
| A | `v3/A-durable-worker` | **checkpoint**, 4 commits, code complete, route not cut over | no |
| B | `v3/B-corpus-manifest` | **complete**, 6 commits | **yes** |
| C | `v3/C-fulltext-acquisition` | **complete**, 5 commits, two workers running live | **yes** |
| D | `v3/D-embed-pipeline` | **checkpoint**, 1 commit, never executed once | no |
| E | `v3/E-shard-infra` | **checkpoint**, 1 commit, one bare VM exists | no |
| F | `v3/F-corpus-release` | **checkpoint**, 1 commit, no tests, one smoke build done | no |
| G | `v3/G-retrieval-wiring` | **complete**, 6 commits | **yes** |
| H | `v3/H-integration` | this branch | n/a |
| I | `v3/I-family-sentinel` | **complete**, 1 commit on top of G | **yes** |

The integrated suite on this branch after all four merges: **1 failed, 1883 passed, 94 skipped**.
The one failure is `tests/test_relevance_audit.py::test_cards_carry_server_rendered_content`,
which asserts that some cards have drawing files on disk. It fails identically on the unmerged
base, so it is environmental and predates all of this. Baseline before the merges was 1 failed,
1695 passed.

**Do not merge A, D, E or F.** Their tips are literally
`wip: workstream checkpoint, interrupted by a provider weekly limit`. Leave them on origin.

---

## 3. The four that are in

### B, the niche boundary and the manifest

`origin/v3/B-corpus-manifest`, 6 commits, merged at `3b782295`.

Adds `sql/010_corpus_release.sql`, `src/corpus_niche.py`, `config/niche_boundary.json`, six
`ops/niche_*.py` scripts, `docs/niche_boundary.md`, `docs/niche_manifest_contract.md`,
`docs/corpus_completeness.md` and `tests/test_corpus_niche.py`.

**Conflicted on `tests/test_migrate.py`**, resolved. See §5.

The two numbers that reframe the whole build: the corpus is already **100.0% of world
publications inside all six core CPC subclasses**, and only **10.8% of the niche has complete
text**. The gap was never breadth of classification. It is text, which is what C, D and F exist
to close.

`sql/010` is **not applied** and must not be yet. See §5.

### C, continuous full-text acquisition

`origin/v3/C-fulltext-acquisition`, 5 commits, head `ea41bdf4`, merged at `4b2ed212`.

Adds `sql/014_fulltext_acquisition.sql`, the `src/acquire/` package, `ops/fulltext_acquire.py`,
two supervisor units and `tests/test_fulltext_acquire.py`.

**Conflicted on `tests/test_migrate.py` and `docs/migrations.md`**, both resolved. See §5.

**014 is adopted, not applied**, done at integration and verified rather than taken on report:
`ops/migration_presence.py` calls the runner's own `presence()` and classified 014 `all` against
the live database. Ledger row recorded 2026-08-22, checksum `80af56ea`, `adopted = true`.

Two workers are running live and **must not be stopped**: `patents-fulltext-acquire` and
`patents-fulltext-acquire-1`, on disjoint shards. `ops/fulltext_acquire.py status` is the honest
view of the pool, the per-provider outcomes and the spend.

MAREC is unobtainable: the Information Retrieval Facility that licensed it no longer operates.
USPTO ODP has no per-publication full-text route, verified against the live key. Neither is worth
re-investigating.

### G, the cold and global retrieval tiers

`origin/v3/G-retrieval-wiring`, 6 commits, merged at `f9bc12d6`. **Merged clean.**

Adds `src/retrieval/cold.py`, `src/retrieval/channels.py`, `src/retrieval/testing.py` (a synthetic
shard fleet behind the real seams, in `src/` so E and F can run the same assertions against real
implementations), the exact-phrase selectivity guard, and four test files.

Seams that moved, which the unmerged branches have not seen:

```
GlobalBackend.records()                       new, optional
shard_router.backend / available / reset_prior  new accessors
Result.tiers                                  what the cold and global tiers did
fusion.channel_weight()                       resolves the cold: prefix to the hot weight
_run_phase(tasks, ...)                        tasks may be (name, fn) or (name, fn, lane)
```

`docs/shard_and_global_seams.md` §4 to §6 is the contract. §5 is what E has to guarantee.

### I, the DOCDB -1 family sentinel

`origin/v3/I-family-sentinel`, 1 commit `7f7f6798` on top of G, merged at `1d56f237`.
**Merged clean.**

A live recall fix, not V3 scaffolding. 21,862 publications carry `simple_family_id = '-1'`, DOCDB
saying "no simple family", and every family expression folded only the empty string. All 21,862
shared one key, so at most one could survive family collapse in any search, and
`legal._date_clause` excluded all 21,862 whenever the subject was one of them.

---

## 4. The four that are not

### A, the durable search worker

**Branch** `v3/A-durable-worker`, 4 commits, tip `d20d27a3`. 21 files, +4102/-586.

**What the last commit contains.** `d20d27a3` is a checkpoint of complete, coherent code, not
notes and not a half-finished feature: cancellation propagation (`src/runctx.py`, `RunCancelled`,
heartbeat `on_lost`), per-unit resume checkpoints, content-digested artifacts (`src/runartifact.py`)
and exactly-once side effects (`runstore.settle`, `claim_side_effect`, the `run_side_effects`
table), each with tests. What makes it "wip" is that **its commit message body is empty**: every
other commit on the branch ends with a test tally and this one does not. The limit hit before the
verification pass, not before the code.

**Adds `sql/012_run_admission.sql` and `sql/013_run_side_effects.sql`.** Neither applied.

**What its author left open**, quoted from `src/runner/worker.py:13-40`:

> "This worker is not enabled, and the route is not cut over: the production web route and status
> stream still use the legacy in-process dispatcher, and no Supervisor worker is running. That
> cutover is a coordinated step and is deliberately not taken here."

So: (a) the route and SSE cutover off `run_queue` + `_JOBS`, and a Supervisor worker with
`DURABLE_WORKER_ENABLED=1`; (b) migrations 012 and 013, which is H's call; (c) the missing test
tally for `d20d27a3`.

**Merge hazard.** A touches `src/retrieval/orchestrator.py` (71 lines). G rewrote large parts of
that file. Expect a real conflict, not a textual one.

**First command**

```
~/v3run 'cd ~/v3/A-durable && ./.venv/bin/python -m pytest tests/test_durable_resume.py -q -p no:cacheprovider -o addopts=""'
```

That 787-line file is what `d20d27a3` added to prove its own claim and is the one thing on the
branch with no recorded result. It creates and drops its own throwaway Postgres schema; it does
**not** need 012 or 013 on the live schema and must not be "fixed" by applying them.

### D, the embedding pipeline

**Branch** `v3/D-embed-pipeline`, 1 commit `1fff9ba5`. 13 files, +3149/-88.

**The commit message body is empty.** There is no handoff text anywhere on the branch; the intent
lives in unusually dense module docstrings that were clearly written as the handoff.

Adds `src/parsed_norm.py` (normalise a fetched publication and *assert* the claim split rather
than trust it), `src/stage_chunks.py`, `src/gcs_lite.py`, `ops/parsed_embed.py` (the worker, 676
lines), `ops/parsed_sources.py`, `ops/batch_embed.py`, `ops/embed_common.py`,
`ops/vector_space_check.py`, a supervisor unit, `sql/013_parsed_embed.sql` and
`tests/test_parsed_embed.py` (57 tests, three of them defect-injected).

**Not one `TODO`, `FIXME` or `NotImplementedError` in the added code.** What is unproven is
deployment, not implementation:

* `patents-parsed-embed.conf` is **not installed** in `/etc/supervisor/conf.d/`.
* There is no `~/v3-logs/parsed_embed.log`. **The worker has never been started once.**
* The GCS source has therefore never met real workstream C output.

**Two traps for whoever resumes.**

1. `sql/013_parsed_embed.sql` **collides on version number with A's `sql/013_run_side_effects.sql`.**
   Different filenames, so git merges both cleanly and then `migrate.discover()` raises. D must
   renumber to 017 or later (see §5).
2. The live `patents-desc-backfill` runs a standalone copy of `desc_backfill.py` out of `$HOME`.
   D refactored the worktree copy to import `embed_common`. **Deploying the new
   `desc_backfill.py` without also copying `embed_common.py` to `$HOME` breaks the running
   backfill.**

Measured and worth keeping: `gemini-embedding-001` at `output_dimensionality=768` is **not unit
normalised**, norm 0.589, and must not be normalised here; the pipeline reproduces stored vectors
to a **max cosine distance of 1.5e-10**. The Vertex embedding quota is account-wide: 3 shards x 16
workers tripped 429, 3 x 12 did not. 4.301 chars per token measured on 60 real chunks, so the
14,379,018 description paragraphs cost roughly **$540** at $0.15/Mtok.

**First command**

```
~/v3run 'cd ~/v3/D-embed && .venv/bin/python ops/vector_space_check.py'
```

The only read-only command on the branch, and it answers the question the interruption makes
urgent: did the provider limit leave the Vertex embedding path usable, and is the vector space
still the corpus's? At ~1e-10 the account is live. If it 429s, nothing else on the branch can
proceed.

### E, the cold domain shards

**Branch** `v3/E-shard-infra`, 1 commit `f32279a7`. 17 files, +2313/-2.

**The commit message body is empty and the branch adds no `*.md` at all.** Worse,
`ops/shards/shards.tsv:19` points at an `ops/shards/README.md` **that was never written**. The
statement of what is left open does not exist; what follows is reconstructed from the code.

Adds `src/retrieval/shard_backend.py` (691 lines, the GCE implementation of both shard seams),
`ops/shards/shardctl.sh` (the whole shard lifecycle in one script), `bootstrap.sh`, `shard_agent.py`,
`prewarm.py`, `idle_reaper.py`, a Tantivy port-holder stub, `shards.tsv`, three systemd units and
`tests/test_shard_backend.py`. **No SQL migration.**

**Seam status, which matters because G already consumes all three:**

| Seam | State |
|---|---|
| `ShardRouterBackend.wake()` | implemented, `shard_backend.py:432` |
| `ShardManagerBackend` state/ensure/connection/release | all four implemented, `shard_backend.py:313/348/388/410` |
| `GlobalBackend` available/search/family_keys | **not implemented, zero lines** |

**No backend is registered anywhere in production.** `register_backend` is called only inside
`install()`/`uninstall()`, and `install()` has exactly one caller: `ops/shards/_smoke.py`, a
throwaway. The branch's own `src/retrieval/__init__.py` says so: *"Imported, NOT installed."*
`bind_run(run_id)` has zero callers, so even if `install()` were called, wakes would take no lease.

**One VM exists and it is bare.** `patents-shard-03-transport`, `us-central1-b`, `c4-highmem-16`,
250 GB hyperdisk, TERMINATED. It has only ever been up **2 minutes 15 seconds**, which is nowhere
near enough for `bootstrap.sh` to install PostgreSQL 17 and pgvector. The other seven do not exist.
Shards are built from scratch, not from a snapshot; cloning eight off one golden image is not
considered anywhere and is the obvious thing to consider.

**The spend decision was never taken.** Eight shards standing on disk with every VM stopped is
about **$340/month**; a running `c4-highmem-16` is **$1.04/hour, $761/month each**.

**The biggest open risk, and it is nowhere in writing.** `shard_agent.py:5` asserts the kernel is
up ninety seconds before Postgres accepts. `SHARD_WAKE_TIMEOUT` is 20 s. If the 90 s is right,
`ensure()` can never return `hot` from cold and the cold tier is unreachable by construction.
`ops/shards/_wakebench.py` exists solely to produce that number and **has never been run**.

**First command**

```
~/v3run 'cd ~/v3/E-shards && ops/shards/shardctl.sh status'
```

Free, read-only, no VM state change, and it prints all eight rows plus each shard's own agent
answer. Then `shardctl.sh bootstrap 03-transport`, then `_wakebench.py 03-transport` to settle the
20 s question before anything else is designed around it.

### F, the corpus release

**Branch** `v3/F-corpus-release`, 1 commit `080b8de0`. 25 files, +9051, all additions.

**The commit message body is empty and the branch adds no `*.md`.** It cites
`docs/corpus_release.md` twice; that file **does not exist**. It was planned and never written.

Adds the `src/corpus/` package (`assign`, `builder`, `demand`, `lexical_build`, `manifest`,
`release_store`, `sizing`, `source`, `stats`), `ops/build_release.py`, `ops/corpus_sizing.py`,
`sql/010_corpus_release.sql` and a vendored Tantivy 0.26 `.so` under `vendor/tantivy/`.

**Zero tests.** No `tests/test_corpus_release.py` exists.

**It did run once, at about 3% scale.** Release `hot_v1` holds 61,513 chunks against a planned hot
corpus of 1,989,163. The result that justifies immutable releases, stated in bytes: the release's
HNSW index measured **3,202 bytes per chunk against the live index's 3,664, 12.6% smaller** at
identical `m`, `ef_construction` and dimension.

**The single biggest recovery risk.** `CORPUS_RELEASE_DSN` is not in the branch's `.env`,
`release_store.dsn()` has no default and raises, and there is no `pgdata/` in the worktree.
**Where `hot_v1` lives is recorded nowhere in the repo.** The only surviving trace of the
interrupted session is `data/logs/` in that worktree, which is gitignored and holds `plan.json`
(the 8-shard fleet plan, 602 domains packed, `fits: true`) and five `measure_*.py` scripts.
**Do not clean that worktree before recovering them.**

Also open: `docs/corpus_release.md` never written; `tantivy` never added to `requirements.txt`
(deferred to C); the 010 collision in §5.

**First command**

```
~/v3run 'cd ~/v3/F-release && ls -la data/logs/ && grep -n CORPUS_RELEASE_DSN .env'
```

Recover the DSN before anything else: without it neither `ops/build_release.py verify hot_v1` nor
`active` can run and the 12.6% result cannot be reproduced. The zero-risk sanity check that needs
no database is `python3 ops/corpus_sizing.py`.

---

## 5. Migrations: the ledger, the numbering and two collisions

**The live ledger, verified 2026-08-22 after the 014 adoption:**

```
adopted   001 003 004 005 006 007 008 009 014
PENDING   002 010 015
```

**The numbering as it now stands.**

| Version | Owner | State |
|---|---|---|
| 009, 012, 013 | A, durable execution | 009 adopted; 012 and 013 on an unmerged branch |
| 010 | B, corpus release | in the tree, **presence=none**, not applied |
| 011 | **nobody** | held empty on purpose. No V3 workstream owns the eval gold set, and `eval/RESULTS.md` is still what every recall claim is measured against. Do not take 011 for something else |
| 014 | C, full text | **adopted** |
| 015 | H | in the tree, presence=all, not yet adopted |
| 016 | H | reserved for the 002 split |
| 017 and up | free | ask H |

**Collision one: `sql/013` is claimed twice.** A's `013_run_side_effects.sql` and D's
`013_parsed_embed.sql`. Different filenames, so git merges both without a murmur and then
`migrate.discover()` raises `DuplicateVersion` and every `migrate.py` command against the live
database stops working. **D renumbers**, because A's 013 is the older claim and is referenced by
A's own tests. 017 is free.

**Collision two: `sql/010_corpus_release.sql` is written twice, at the same path.** B's (79 lines,
in the tree now) and F's (299 lines, unmerged). Both create `corpus_niche_definition`, with
disjoint columns and different primary keys: B keys on `(name, version)` and stores a frozen
boundary spec; F keys on `id` and stores one row per CPC symbol. **Both use
`CREATE TABLE IF NOT EXISTS`, so whichever applies first silently wins and the other workstream
reads a table it does not recognise.** Nothing fails loudly.

This is why **010 has not been applied**, even though it probes `none` and would apply cleanly.
Applying it now would decide that collision by accident. B's own file says the release tables
should be *appended to this file* rather than given a new number; F wrote a fresh file instead.
Settling it is a decision for whoever resumes F, and it has to be taken before 010 is applied.

**002 is not merely pending, it is unappliable as written.** It asks for `ix_bench3072_hnsw` on
`bench_emb_3072.embedding`, which the catalog reports as `vector(3072)`, and pgvector 0.8.5 refuses
an hnsw index above 2000 dimensions. So `run.sh`'s final `apply --only 002` cannot succeed on any
fresh install, and `presence()` will report `partial` for ever. That is what 016 is reserved for:
split the two benchmark-table indexes out, leaving 002 as the two real corpus indexes.

`ops/migration_presence.py` prints presence and replayability for every pending migration, and the
002 dimension check, without writing anything. Run it before any adopt-or-apply decision.

---

## 6. Facts that outlived their workstream

**`runstore.claim_ingest()` takes no filter, and a test suite eats the demand signal.**
`tests/test_durable_runs.py:401` (base numbering) calls it with `limit=50`. Ten rows C's fetcher
queued on the live database between 15:16:43 and 15:16:53 on 2026-08-22 were in state `claimed` a
few minutes later with no release process running. The rows and payloads survive; the `pending`
state does not. **Any branch running that test file silently consumes the demand signal
`corpus_release` is meant to rank by.** A's branch reduces the blast radius from 50 rows to 1 and
orders by priority, but the API defect stands: there is no way to scope a claim to a caller.
This is a real defect in A's area. It is recorded, deliberately not fixed here.

**The shard router's family vote was inverted.** `_rank_weighted` suppresses every member of a
family after the first, and `domains_of_publications` defaulted a pid missing from the weights map
to a *full* vote of 1.0, so the suppressed members outvoted the survivor 41:1. Measured on the
regression that found it: `B25J` 0.986 / `B66C` 0.004 before, 0.534 / 0.466 after. Fixed in G.
A supplied weights map is now the electoral roll, not a set of adjustments.

**The niche is 100.0% classified and 10.8% texted.** B measured that the corpus already holds
100.0% of world publications inside all six core CPC subclasses, and that only 10.8% of the niche
has complete text. Every remaining recall argument is about text, not about classification
breadth.

**One rule, two implementations, and they disagree.** `retrieval.family.FAMILY_SENTINELS` is
`("", "-1", "0")`; `corpus_niche.NO_FAMILY` is `{"", "-1", "0", "null", "none", "n/a", "\n"}`.
Censused on the live corpus over the union of both sets, exactly one value occurs: `-1`, on 21,862
rows. Nothing else, including the empty string every expression in the codebase was folding before
the fix. So the divergence is latent rather than live, and collapsing the two lists into one is
tidying, not urgent. Do it when something else touches either file.

**Two names for the unclassified route.** `corpus_niche.subclass_of` returns `""` where
`shard_router.domain_of` returns `"unclassified"`. They agree on every real symbol. A shard
registered under `""` while the router emits `"unclassified"` is 1,024,320 publications, 20.6% of
the corpus, quietly unreachable, and `hot_domains` would never match them.
`docs/shard_and_global_seams.md` §5.6 is the binding statement.

**MAREC is unobtainable** and **USPTO ODP has no per-publication full-text route**, the latter
verified against the live key. Do not spend another session on either.

---

## 7. What must keep running

```
patents-desc-backfill        ~/desc_backfill.py 0 1 core     against the patent-search-pilot venv
patents-fulltext-acquire     ~/v3/C-fulltext/ops/fulltext_acquire.py run --shard 0 --of 2
patents-fulltext-acquire-1   ~/v3/C-fulltext/ops/fulltext_acquire.py run --shard 1 --of 2
patent-results               the live search, port 8631
```

Two things about those paths that are easy to break by tidying.

**The two fetchers run out of `~/v3/C-fulltext`**, a workstream worktree, not out of the deployed
checkout. C's code is merged here, but the running processes still point at that directory. Do not
remove or `git clean` that worktree while they are up.

**`patents-desc-backfill` runs a standalone copy at `~/desc_backfill.py`**, not the one in any
worktree. Workstream D refactored the worktree copy to import `ops/embed_common.py`. If that
version is ever deployed, `embed_common.py` has to be copied to `$HOME` in the same step or the
backfill stops on an import error.

`patent-results` must never be restarted: one search can run over an hour, a restart kills it, and
a second restart leaves the run interrupted and needing a manual re-run.

**It was restarted five times on 2026-08-22**, at 15:25, 15:55, 16:29, 16:48 and 17:10. Every one
of them is `waiting for patent-results to stop` in `/var/log/supervisor/supervisord.log`, which is
a deliberate `supervisorctl` command and not a crash or an autorestart. **None of them is
attributable.** There is no cron entry, no `cron.d` file, no watchdog script anywhere under
`/home/nimrod_rotem` that issues it, and by the time this was checked no agent process was left on
the box to ask. So the rule was broken five times by sessions that have since exited, and whatever
did it will do it again unless somebody finds it. The next session should treat that as a real
open item, not as background noise: a live search that dies at minute fifty looks exactly like a
search that failed.

Never `pkill` on a pattern, this host runs other people's agents. Kill by port:
`lsof -ti tcp:PORT | xargs kill`.

The two fetchers are on **disjoint shards** (`--shard 0 --of 2` and `--shard 1 --of 2`). If a third
is added, `--of` must change on every worker in the same edit. Leaving one at `--of 1` while
another runs `--of 4` is the one mistake that puts two workers on the same partition.

---

## 8. The order to resume in

1. **F first, and only to recover `CORPUS_RELEASE_DSN` and `data/logs/`.** It is the one piece of
   state that exists nowhere in git and would be destroyed by a `git clean`.
2. **Settle the 010 collision**, then apply 010. B and F cannot both be right about
   `corpus_niche_definition`.
3. **Renumber D's 013 to 017**, before anyone merges either A or D.
4. **E's wake benchmark.** If cold to hot really is 90 s, the 20 s `SHARD_WAKE_TIMEOUT` and the
   whole cold tier need rethinking, and that decision invalidates work in three other workstreams
   if it is taken late.
5. **A's route cutover**, which is a coordinated step and the last one that should move.

D is the least blocked of the four: its only external dependency is that C has written something
to `gs://nimo-patents-v3/parsed/`, and C has been running for hours.
