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
adopted: 001 003 004 005 006 007 008 009
pending: 002
009 checksum: 954e4ec3af83774db8da9af40581e392b62337e8031f1493d6b2db485a3633eb
```

The adoption ran from committed integration checkpoint `dbe01d7`. Every selected migration probed
`all`; the runner recorded the files and checksums without executing their DDL. A subsequent
`migrate.py status` reports those eight versions applied and only 002 pending.

`002` remains pending for a real reason: `ix_chunks_hnsw` and `ix_chunks_tsv` exist, but
`ix_bench1024_hnsw` and `ix_bench3072_hnsw` do not. Those index the dimension sweep benchmark
tables, which hold 1,308 rows each. Do not adopt partial 002 or insert a ledger row by hand. Apply
it through the runner only after the active corpus backfill stops competing for database resources,
or split the optional benchmark indexes into a later migration before 002 is recorded.

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

## The assigned version numbers

Three workstreams each wrote a `009` and nothing noticed, which is what this section exists to
stop happening again. Two files that differ only in NAME merge cleanly in git and then
`discover()` raises `DuplicateVersion` and every `migrate.py` command against the live database
stops, including `status`. Take a number from this table; if none is yours, ask before inventing
one.

| Version | Owner | File | State |
|---|---|---|---|
| 009, 012, 013 | durable execution | `009_durable_runs.sql`, `012_run_admission.sql`, `013_run_side_effects.sql` | 009 adopted; 012 and 013 unmerged |
| 010 | corpus release | `010_corpus_release.sql` | in the tree, not applied. B and F both wrote one, see below |
| 011 | **nobody** | held empty | the eval gold set. No V3 workstream owns it. Do not take 011 for something else |
| 014 | full-text acquisition | `014_fulltext_acquisition.sql` | **adopted** 2026-08-22, checksum `80af56ea` |
| 015 | integration | draft turns | in the tree, presence=all, not adopted |
| 016 | integration | the 002 split | reserved |
| **017** | **parse and embed** | **`017_parsed_embed.sql`** | **in the tree, created by the worker's own DDL on the live database, not applied through the runner** |
| 018 | niche pipeline | | reserved |

**017 was 013 until 2026-08-22.** It collided with durable execution's `013_run_side_effects.sql`,
which is the older claim and is referenced by that workstream's own tests, so the parse-and-embed
migration renumbered. Nothing had been applied through the runner under either number, so the
rename cost nothing: `ops/parsed_embed.py` reads the file by name at startup and was updated in
the same commit.

### 017 is present on the live database and was never applied by the runner

`ops/parsed_embed.py` executes `sql/017_parsed_embed.sql` on startup, the same way
`ops/desc_backfill.py` executes its own `DDL` and `src/sources/docstore.py` executes 008's. Every
statement in the file is `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS` or
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so it is replayable and the runner will probe it
`all`. It is a candidate for `adopt`, not for `apply`, and that is workstream H's call to take
once, deliberately.

The three `ALTER TABLE parsed_doc_ledger ADD COLUMN IF NOT EXISTS` statements are load bearing and
not tidiness. `fetched_number`, `source` and `donor_publication` were added after the table
already existed on the live database, created by an earlier run of the worker. A migration that
shipped only the new `CREATE TABLE` would be silently wrong on exactly the host that holds the
data, and the worker would fail on its first `INSERT`.
