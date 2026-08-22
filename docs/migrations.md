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
pending:  002 010 015        (all three are in THIS tree, after the H merge)
009 checksum: 954e4ec3af83774db8da9af40581e392b62337e8031f1493d6b2db485a3633eb
014 checksum: 80af56ea66ca4340239f4ec89a7d2747c790d34ebcec4b5023c53fbf36a48e32
014 adopted:  2026-08-22 17:06:34+00
```

The first adoption ran from committed integration checkpoint `dbe01d7`. Every selected migration
probed `all`; the runner recorded the files and checksums without executing their DDL. **014 was
adopted at the workstream C integration**, on the same terms and for the same reason: its four
tables and six indexes were created by an explicit operator command and `presence()` classifies it
`all`.

The three that remain pending are pending for three different reasons, and only one of them is
"nobody has got to it yet".

`015` probes `all` and is simply not adopted yet. One command, whenever someone is confident.

`010` probes `none`, so it would have to be APPLIED and not adopted. **It must not be applied
yet.** Workstream F's unmerged branch writes a different file at the same path and the same
number, and both create `corpus_niche_definition` with disjoint columns and different primary
keys. Both use `CREATE TABLE IF NOT EXISTS`, so whichever runs first silently wins and the other
workstream reads a table it does not recognise. Applying 010 now decides that collision by
accident rather than on purpose.

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

Verified against the live database on 2026-08-22 by workstream H, read only:

| Object | State | Measured |
|---|---|---|
| `ix_chunks_hnsw` | present | 94 GB |
| `ix_chunks_tsv` | present | |
| `ix_bench1024_hnsw` | **missing** | table `bench_emb_1024` holds 1,308 rows, `embedding` is `vector(1024)` |
| `ix_bench3072_hnsw` | **missing and impossible** | `bench_emb_3072.embedding` is `vector(3072)`, and pgvector 0.8.5 refuses an hnsw index above 2,000 dimensions |

Every ledger row carries `adopted = true` and `applied_at = 2026-08-22 10:05:54+00`.

### The 002 decision, taken 2026-08-22 by workstream H

**Not today.** `patents-desc-backfill` has been running for over six hours and must stay running,
and V3 schema integration is still moving. Nothing in 002 is urgent: both indexes the search path
actually uses are already built.

**When it is taken, the route is split, not apply as written.** The two production indexes in 002
already exist, so their `CREATE INDEX IF NOT EXISTS` are catalog no-ops. The only real work left in
the file is the trailing `ANALYZE chunks` over 27.6M rows and the two benchmark HNSW builds over
1,308 rows each, and the `ANALYZE` is the part that competes with the backfill. So:

1. Move `ix_bench1024_hnsw`, `ix_bench3072_hnsw` and `ANALYZE chunks` into `sql/016_bench_indexes.sql`.
2. `migrate.py adopt --only 002`, which executes no DDL at all and records the truth: both of its
   remaining objects are present, so it probes `all` and adoption is honest rather than partial.
3. Apply 016 at a quiet moment, when its cost is one small index build and one `ANALYZE` and
   nothing else is contending.

**Correction to step 3, measured 2026-08-22 against the live catalog.** It is ONE index build, not
two. `bench_emb_3072.embedding` is `vector(3072)` and pgvector 0.8.5 caps the hnsw access method at
2,000 dimensions, so `CREATE INDEX ix_bench3072_hnsw ... USING hnsw` cannot succeed on any database,
ever, not merely not today. **002 as written is unappliable**, which means `run.sh`'s final
`apply --only 002` would fail on a fresh install and `presence()` will report `partial` for ever.
The split is therefore not an optimisation, it is the repair. When 016 is written, that index needs
either a different access method (`ivfflat` has no such limit) or to be dropped along with the
3072-dimension sweep it belonged to.

Editing 002 is safe precisely because it has no ledger row. That is the whole reason the split is
available: the moment 002 is recorded, its bytes are frozen like every other applied migration.

Nobody should execute any of this without saying so first. Migration application against the live
database is one deliberate decision, and today's answer is no.

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

## Version assignments, V3 rebuild

Workstream H owns this table. It is the authority. Take a number from here before you name a file,
and if you need one that is not listed, ask H rather than picking the next one you see: two agents
picking the same next one is how the last collision happened, and it happened again on 2026-08-22
(see below). `discover()` turns a duplicate into a hard error, but only after both files exist.

| Version | File | Owner | State |
|---|---|---|---|
| 001 to 008 | legacy baseline | deployed | adopted on the live database |
| 009 | `sql/009_durable_runs.sql` | durable execution | adopted on the live database |
| **010** | `sql/010_corpus_release.sql` | workstreams B and F, **contested** | B's version integrated; F's unmerged version writes the same path. presence=none, NOT applied |
| **011** | `sql/011_eval_gold_xy.sql` | **unclaimed** | reserved, see below |
| **012** | `sql/012_run_admission.sql` | workstream A, durable worker | on `origin/Nimo/v3-worker-cutover` |
| **013** | `sql/013_run_side_effects.sql` | workstream A, durable worker | on `v3/A-durable-worker`, **also claimed by workstream D**, D renumbers |
| **014** | `sql/014_fulltext_acquisition.sql` | workstream C, full text | integrated and **adopted** on the live database |
| **015** | `sql/015_draft_turn_candidates.sql` | workstream H | integrated, see the 006 incident |
| **016** | `sql/016_bench_indexes.sql` | workstream H | reserved for the 002 split |
| **017** and above | free | | ask H. Workstream D takes 017 when it renumbers off 013 |

Two corrections to what this document used to say. The branches it named, `rebuild/v3-corpus` and
`rebuild/v3-eval`, **do not exist**; the work moved to `v3/F-corpus-release` and, in eval's case,
nowhere. And 011 is **claimed by nobody**: no workstream in the V3 build owns the eval gold set, so
011 is held empty rather than reassigned, because `eval/RESULTS.md` and the gold set it describes
are still the thing any recall claim is measured against. Do not take 011 for something else.

Ordering is durable, then corpus, then retrieval, then infrastructure, because the corpus release
tables are the landing zone the backfill already writes into.

### 014 is ADOPTED, not applied, and why that is not a precedent

`sql/014_fulltext_acquisition.sql` is entirely `CREATE TABLE IF NOT EXISTS` and
`CREATE INDEX IF NOT EXISTS` over four new tables that nothing else reads, and none of its indexes
touches a live table. Its objects were created on the live database on 2026-08-22 by an explicit
operator command, `ops/fulltext_acquire.py ensure-schema`, rather than by the runner. That is a
deliberate exception and not a pattern to copy: it is an operator command, it is not called at
worker startup, and the worker refuses to start if the tables are absent
(`acquire.tasks.require_schema`).

Because the objects are already there, the ledger is made honest with `adopt` and not with `apply`.
`migrate.py adopt --only 014` records the file and its checksum without executing the DDL. Every
statement in it is idempotent so `apply` would also succeed rather than raise the way 007 does, but
adopting is what keeps the ledger a record of what actually ran.

### The 006 incident, 2026-08-22

`sql/006_draft_agent.sql` was adopted into the live ledger at 10:05 with checksum `a7ad2750`. At
11:08, commit `e4199f5b` on the deployed `Nimo/drafting-ready` line appended a
`CREATE TABLE IF NOT EXISTS app_draft_turn_candidates` block to that same file, taking it to
`4347e3df`. From then until this repair, `migrate.py` refused **every** command against the live
database with `ChecksumDrift` on 006. Not just Draft Studio's: all eight V3 workstreams lost the
ability to run `migrate.py status`.

The repair is what the runner's own doctrine says: 006 is back to `a7ad2750`, byte for byte, and
the new table is `sql/015_draft_turn_candidates.sql`. No DDL changed. The table already exists on
the live database with two rows, created out of band, so 015 probes present.

Consequences worth knowing:

* **`sql/006_draft_agent.sql` is frozen.** Its bytes are the ledger's. It contains three em dashes
  and they have to stay, because removing one re-breaks the checksum. Fix them in a new migration
  or not at all.
* **`sql/002_indexes.sql` is not frozen**, because it has no ledger row yet.
* An applied migration is history. New schema goes in a new file, always, even a three line table.

`run.sh` now invokes the runner with an absolute virtual-environment path. It applies all light
migrations through `apply --exclude 002`, performs corpus construction, then applies only 002 at
the deliberate heavy-index step. Database credentials come from the configured migration
environment file; no password is embedded in the script.
