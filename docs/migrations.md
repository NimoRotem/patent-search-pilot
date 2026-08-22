# Migrations

`src/migrate.py`. One command, one ledger, and a strong bias toward refusing rather than guessing.

## Why it exists

`run.sh` applies exactly two files, `001_schema.sql` and `002_indexes.sql`, with a raw `psql -f`.
Files 003 through 008 have **no applier at all**, and `figure_images.sql` has no version number.
Nothing anywhere recorded what had run against which database, so the numbering was decorative:
three workstreams independently wrote a `009_*.sql` in the same week and nothing noticed.

## Commands

```
migrate.py status          what is applied and what is pending, writes nothing; refuses legacy ambiguity
migrate.py plan            same thing
migrate.py apply           apply pending migrations, one transaction per file
migrate.py adopt           record fully present migrations WITHOUT executing them
migrate.py adopt --only 001 003 004    the same, for named versions
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
| No unversioned files | `figure_images.sql` is a hard error, not a skip |
| Dry run is dry | `status` and `plan` do not even create the ledger, and they report legacy ambiguity instead of calling present DDL pending |
| Adoption is truthful | every selected migration must probe `all`; `partial`, `none` and `unknown` are refused |
| Selection is exact | an empty or unknown `--only` set is refused instead of becoming a successful no-op |

## The legacy database, and why it is not a fresh install

An empty ledger does **not** mean an empty database. The runner probes each migration for the
objects it creates and classifies it `all`, `none` or `partial`, then:

* everything `none`: a genuinely fresh database, apply normally.
* everything `all`: `BootstrapRequired`. Adopting is right, but a human asks for it.
* a mixture: `BootstrapUndecidable`. Report which migration fell on which side and stop.

Replaying is not a safe fallback, for two measured reasons. `007_figure_compiler.sql` ends in a
bare `CREATE TRIGGER`, which has no `IF NOT EXISTS` form here, so a second run **raises**. And
`002_indexes.sql` builds `ix_chunks_hnsw`, which measures **94 GB** on the live box.

### Measured state of the live corpus database, 2026-08-22, read only

```
ledger exists: False
001  all        003  all        005  all        007  all
002  PARTIAL    004  all        006  all        008  all
```

`002` is partial for a real reason: `ix_chunks_hnsw` and `ix_chunks_tsv` exist, but
`ix_bench1024_hnsw` and `ix_bench3072_hnsw` do not. Those index the dimension sweep benchmark
tables, which hold 1,308 rows each and were evidently never indexed on purpose.

So the live database is **undecidable by the rule above, and the tool will refuse it**. That is the
correct answer, not an obstacle. The resolution is a human decision recorded explicitly:

```
migrate.py adopt --only 001 003 004 005 006 007 008
```

then decide `002` on its merits. Either build the two benchmark indexes so it becomes fully
present, or revise the not-yet-adopted legacy baseline so optional benchmark indexes are represented
separately. The runner will refuse to adopt partial `002`. **Do not insert a ledger row by hand or
run a bare `adopt`**, because that would record `002` as complete when two of its four indexes do
not exist, which is the exact lie the ledger exists to prevent.

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

Three unmerged branches each wrote a `sql/009_*.sql`. None is applied anywhere. At integration:

| Branch | File today | Becomes | Contents |
|---|---|---|---|
| `rebuild/v3-durable` | `sql/009_durable_runs.sql` | **`sql/009_durable_runs.sql`** (unchanged) | `search_runs`, `search_stages`, `search_queries`, `retrieval_hits`, `search_candidates`, `provider_usage`, `shard_leases`, `corpus_ingest_queue` |
| `rebuild/v3-corpus` | `sql/009_corpus_release.sql` | **`sql/010_corpus_release.sql`** | `corpus_niche_definition`, `corpus_release`, `corpus_release_active`, `corpus_release_member`, `corpus_release_shard`, `chunks_release`, `corpus_fetch_ledger` |
| `rebuild/v3-eval` | `sql/009_eval_gold_xy.sql` | **`sql/011_eval_gold_xy.sql`** | `eval_gold_set`, `eval_gold_subject`, `eval_gold_pair`, `eval_scorecard` |

Order is durable, then corpus, then eval, because the corpus release tables are the landing zone
the backfill already writes into and eval's gold set is the last thing to depend on either.

**This map is documentation only. No other worktree was edited to produce it**, and renaming the
files is an integration step. Once renamed, `discover()` enforces the rest: a fourth `009` becomes
a hard error instead of a coincidence nobody notices.

Also outstanding, and not fixed here because it is a product change rather than migration safety:

* `sql/figure_images.sql` has no version. It must be numbered or moved out of `sql/`, or
  `discover()` refuses the whole directory. It is currently refused.
* `run.sh` still carries the corpus Postgres password as a literal on two lines, and still applies
  only 001 and 002 by hand. Replacing those two `psql -f` calls with `migrate.py apply` removes
  both problems at once.
