# The offline corpus release builder

Workstream O. This file exists because the location of the builder database was, for a while,
knowledge that lived in one agent's head and in no file. If you read nothing else, read section 1.

---

## 1. Where the builder database is

**`CORPUS_RELEASE_DSN` is the builder / release database. It has no default and never will.**
A default would be a way to point a release build, and therefore an HNSW build, at production by
forgetting to set an environment variable. `release_store.connect()` raises when it is unset.

| | |
|---|---|
| Host | `nimo-iptorch-patents` (the same VM the search app runs on), **loopback only** |
| Cluster | PostgreSQL **17**, cluster name **`relbuild`**, `pg_lsclusters` shows it |
| Port | **5544** (`listen_addresses = 127.0.0.1`, so it is unreachable off the box) |
| Database | `relbuild` |
| Role | `relbuild`, superuser in that cluster, scram-sha-256 over TCP |
| Data directory | `/var/lib/postgresql/17/relbuild` |
| Extensions | `vector` **0.8.6** (pgvector), `plpgsql` |
| Secret | the password is in `patent-search-pilot/.env` as `CORPUS_RELEASE_DSN`, and **only** there |
| Snapshots | `/home/nimrod_rotem/v3-releases/<release_id>/` |

The DSN line in `.env` looks like this, with the password redacted here on purpose:

```
CORPUS_RELEASE_DSN=host=127.0.0.1 port=5544 dbname=relbuild user=relbuild password=<in .env>
```

`.env` is gitignored and symlinked into every v3 worktree, so any worktree that loads it gets the
builder DSN for free. That is the whole of the wiring.

### If the password no longer works

It has already happened once. `ops/release_measurements/set_dsn.sh` rotates the role password
*before* it checks whether `.env` already has a line, so a second run rotates the secret in
Postgres and then declines to write the new one down. Do not run it again as it stands. To
reconcile, make Postgres agree with `.env` rather than the other way round:

```bash
cd ~/v3/O-release
PW=$(grep '^CORPUS_RELEASE_DSN=' .env | sed 's/.*password=//')
sudo -u postgres psql -p 5544 -tAc "ALTER ROLE relbuild WITH PASSWORD '$PW' LOGIN"
```

`local all all trust` is in that cluster's `pg_hba.conf`, so `sudo -u postgres psql -p 5544` is
always a way back in even with the password lost entirely.

### Why the builder is not production, stated once

An HNSW build on the live database is an outage: it takes `maintenance_work_mem`, it takes
parallel workers, and it takes an `ACCESS EXCLUSIVE`-adjacent share of the box that the serving
index is already short of. V3 exists *because* a 101 GB index is being queried on a 62 GB host.
So the release builder has its own cluster, on its own port, holding its own mirror of the light
tables, and reads the live corpus read-only and in key ranges.

`CORPUS_SOURCE_DSN` is the *other* DSN: the corpus to read. It falls back to
`PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD` from `.env`, which is the live corpus, and
`source.connect_source()` opens it with `default_transaction_read_only=on` set as a session GUC at
connect time, so Postgres enforces the read-only-ness rather than this repo's good intentions.

### Builder box limits, measured

8 vCPU, 31 GiB RAM, 197 GB disk with 156 GB free (2026-08-22). `maintenance_work_mem` is 2 GiB and
`shared_buffers` 2 GiB on the `relbuild` cluster. pgvector builds an HNSW graph inside
`maintenance_work_mem` and falls back to a much slower two-phase on-disk build when it does not
fit, so **this box can only build small releases in memory**: about 590k chunks at fp32's measured
3,654 B/chunk. `sizing.build_ram_required_gib()` prints the requirement for a given release size.
A full domain shard's index does not fit here; the shard VMs are where those get built.

---

## 2. The pipeline

```
staged chunks -> family home-shard assignment -> pgvector load -> HNSW build
              -> Tantivy build -> completeness stats -> disk snapshot -> release manifest
```

`ops/build_release.py` is the whole interface.

```bash
ops/build_release.py mirror                 # publications + CPC symbols -> builder DB
ops/build_release.py assign                 # every family gets ONE home domain
ops/build_release.py plan --shards 8 --json data/logs/plan.json
ops/build_release.py build --shard-key hot --kind hot --niche
ops/build_release.py verify   hot_v1
ops/build_release.py activate hot_v1 --actor nimo --reason "..."
ops/build_release.py rollback hot
```

Builder-local staging tables (`src_publications`, `src_classifications`, `src_family_home`,
`src_mirror_state`) are created by `source.MIRROR_DDL` and are **not** part of `sql/010`. Nothing
in them is released; they are the scratch a build works from.

---

## 3. Migration 010

`sql/010_corpus_release.sql` is workstream O's. **It has not been applied to the live database and
must not be** until the number collision is resolved: two files currently claim 010, the other
being the niche pipeline's, which workstream M is renumbering to 018. The release tables exist
today only in the `relbuild` cluster, which is where the builder needs them anyway.

---

## 4. What a release is

A release is immutable. It is identified by `release_id` (`<shard_key>_v<n>`), it carries a
content hash over its manifest, and it is never edited after `sealed_at` is set. `corpus_release_active`
is the switch: one row per shard key, updated in one transaction, with the previous release id kept
in `previous_release_id` so `rollback` is a single statement and not an archaeology exercise.

A shard verifies it is serving what it thinks it is with `release_store.verify_serving()`, which
recomputes the content hash from the database rows and compares the on-disk artefact checksums
against the manifest's `SHA256SUMS`.
