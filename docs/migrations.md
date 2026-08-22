# Migrations

`src/migrate.py`. One command, one ledger, and a strong bias toward refusing rather than guessing.

## Why it exists

`run.sh` historically applied exactly two files, `001_schema.sql` and `002_indexes.sql`, with raw
`psql -f`, while files 003 through 008 had no applier. Nothing recorded what ran against which
database, so the numbering was decorative: three workstreams independently wrote a `009_*.sql`
in the same week and nothing noticed. The image schema that used to be unversioned now belongs to
the unadopted legacy `001_schema.sql` baseline, so discovery is deterministic.

## Commands

```
migrate.py status          what is applied and what is pending, writes nothing; refuses legacy ambiguity
migrate.py plan            same thing
migrate.py apply           apply pending migrations, one transaction per file
migrate.py adopt           record fully present migrations WITHOUT executing them
migrate.py adopt --only 001 003 004    the same, for named versions
migrate.py apply --exclude 002         apply everything except the heavy index migration
```

Connection comes from `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, or from a file
named by `MIGRATE_ENV_FILE`. **There is deliberately no default password**: a missing setting is a
startup error, because a working default means nothing ever forces the environment to be configured
and the literal outlives every rotation.

## What it guarantees

| Guarantee | How |
|---|---|
| Nothing runs twice | `schema_migrations` ledger: version, filename, checksum, applied_at, applied_by, duration_ms, adopted |
| Deterministic order | sorted by `int(version)`, so 002 runs before 010, not after it |
| No half applied file | one transaction per file, and the ledger row is written inside it, so a failure rolls back both |
| No concurrent deploys | the session lock is acquired before ledger creation and released before `apply()` returns |
| No silent edits | filename and sha256 are checked; editing or renaming applied history is `ChecksumDrift` |
| No duplicate versions | two numeric aliases such as `009` and `9` are the same version and a hard error |
| No unversioned files | image-table DDL is part of 001; any future unnumbered SQL is a hard error |
| Dry run is dry | `status` and `plan` do not even create the ledger, and they report legacy ambiguity instead of calling present DDL pending |
| Adoption is truthful | every selected migration must probe `all`; `partial`, `none` and `unknown` are refused |
| Selection is exact | empty or unknown `--only` and `--exclude` sets are refused; the flags are mutually exclusive |

## The legacy database, and why it is not a fresh install

An empty ledger does **not** mean an empty database. The runner probes each migration for the
objects it creates and classifies it `all`, `none` or `partial`, then:

* everything `none`: a genuinely fresh database, apply normally.
* everything `all`: `BootstrapRequired`. The operator must invoke `adopt` explicitly.
* a mixture: `BootstrapUndecidable`. Report which migration fell on which side and stop.

Replaying is not a safe fallback, for two measured reasons. `007_figure_compiler.sql` ends in a
bare `CREATE TRIGGER`, which has no `IF NOT EXISTS` form here, so a second run **raises**. And
`002_indexes.sql` builds `ix_chunks_hnsw`, which measures **94 GB** on the live box.

### Live corpus database ledger, 2026-08-22

```
adopted:  001 003 004 005 006 007 008 009 014
applied:  012 013            (2026-08-22 19:07 UTC, the durable cutover)
pending:  002 010 015        (010 and 015 are not in this tree)
009 checksum: 954e4ec3af83774db8da9af40581e392b62337e8031f1493d6b2db485a3633eb
```

The adoption ran from committed integration checkpoint `dbe01d7`. Every selected migration probed
`all`; the runner recorded the files and checksums without executing their DDL. A subsequent
`migrate.py status` reports those eight versions applied and only 002 pending. `014` was adopted
separately by the full-text workstream at 17:06.

**012 and 013 were APPLIED, not adopted**, at 19:07 UTC on 2026-08-22, through
`migrate.py apply --only 012 013`, because durable execution cannot run without them: `012` is the
admission decision (`search_runs.admitted`, `admitted_at`, `charged_day` plus three partial
indexes) and `013` is `run_side_effects`, the once-per-run ledger that makes three attempts debit
once. Both are additive and nothing in the legacy path reads either, so neither is undone by a
rollback of the cutover. Applying them was cheap: `search_runs` held zero rows, so the one-time
`UPDATE` in 012 matched nothing and the three partial indexes built on an empty table. Neither
touches the corpus or the 94 GB HNSW.

**002 was NOT applied, and cannot be as written.** `ix_chunks_hnsw` and `ix_chunks_tsv` exist but
`ix_bench1024_hnsw` and `ix_bench3072_hnsw` do not, so presence is `partial` for ever.
`bench_emb_3072.embedding` is `vector(3072)` and pgvector 0.8.5 refuses an HNSW index above 2000
dimensions, so `ix_bench3072_hnsw` can never be created and the file cannot succeed on any fresh
install. Splitting the two benchmark indexes out into 016 is the fix, and it is not this
workstream's. Do not adopt partial 002 and do not insert a ledger row by hand.

**010 was NOT applied**, and is not even in this tree. Two different files claim that number, B's
79-line version and F's 299-line version, both creating `corpus_niche_definition` with different
primary keys and both using `CREATE TABLE IF NOT EXISTS`, so whichever applies first silently
wins. Applying it would settle that collision by accident.

### A stale copy of an applied migration is a real outage of the runner

`sql/006_draft_agent.sql` on this branch was an older revision than the one the live ledger
recorded, so `migrate.py` refused EVERY command with `ChecksumDrift` on 006, including `status`.
The file, not the ledger, was the stale thing: the recorded revision creates
`app_draft_turn_candidates`, which the live database has. Fixed by taking the adopted revision.
The lesson generalises: a branch that forks before a migration is edited carries a file that will
stop the runner dead on the live database, and the symptom names 006 while the cause is the fork
point.

## Sentinels

Presence is probed from the objects a migration creates: tables, indexes, views, added columns and
triggers. Two details that are not obvious:

* `005_profile_and_notifications.sql` creates no table at all, only `ALTER TABLE ... ADD COLUMN`.
  A table-only probe would call it absent for ever and try to replay it on every run, so columns
  are probed too.
* **Comments are stripped before any DDL is matched.** `008_sources_docstore.sql` explains itself
  with `(CREATE TABLE IF NOT` at the end of one comment line and `EXISTS);` at the start of the
  next. Matching across that line break invented an object named literally `IF`, which is never
  present, which pinned 008 at `partial` for ever and would have blocked adoption permanently for
  a reason nobody could see. Found by probing the live database, not by reading the code.

## Integration renumbering map

Three workstreams each started with a `sql/009_*.sql`. The assigned integration versions are:

| Branch | File today | Becomes | Contents |
|---|---|---|---|
| durable execution | `sql/009_durable_runs.sql` | **`sql/009_durable_runs.sql`** (unchanged) | `search_runs`, `search_stages`, `search_queries`, `retrieval_hits`, `search_candidates`, `provider_usage`, `shard_leases`, `corpus_ingest_queue` |
| `rebuild/v3-corpus` | `sql/009_corpus_release.sql` | **`sql/010_corpus_release.sql`** | `corpus_niche_definition`, `corpus_release`, `corpus_release_active`, `corpus_release_member`, `corpus_release_shard`, `chunks_release`, `corpus_fetch_ledger` |
| `rebuild/v3-eval` | `sql/009_eval_gold_xy.sql` | **`sql/011_eval_gold_xy.sql`** | `eval_gold_set`, `eval_gold_subject`, `eval_gold_pair`, `eval_scorecard` |

Order is durable, then corpus, then eval, because the corpus release tables are the landing zone
the backfill already writes into and eval's gold set is the last thing to depend on either.

The durable migration is now integrated at 009. Corpus and evaluation files must be renamed at
their own integration points. Once renamed, `discover()` enforces the rest: another numeric alias
becomes a hard error instead of a coincidence nobody notices.

`run.sh` now invokes the runner with an absolute virtual-environment path. It applies all light
migrations through `apply --exclude 002`, performs corpus construction, then applies only 002 at
the deliberate heavy-index step. Database credentials come from the configured migration
environment file; no password is embedded in the script.
