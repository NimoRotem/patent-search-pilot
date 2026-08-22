# The cold domain shards

Eight VMs, normally TERMINATED, each holding one eighth of the CPC subclasses. A search routes to
the domains it needs, the shards wake, the same retrieval SQL runs against them as against the hot
corpus, and fifteen idle minutes later they stop again.

This directory is the whole of that: the table, the controller script, what runs on the box, and
the two measurements the design rests on.

---

## The number that shapes everything

**MEASURED 2026-08-22 on `patents-shard-03`, `ops/shards/wakebench.py domain_03`, an empty shard:**

| Milestone | Seconds after the start call |
|---|---|
| `start` API returned | 0.3 |
| instance reports RUNNING | 12.6 |
| the shard agent answers, saying `waking` | 24.2 |
| Postgres accepts connections | 24.2 |
| the agent says `hot` | **25.0** |
| blocking prewarm phase | 0.086 (nothing to warm) |

`SHARD_WAKE_TIMEOUT` is 20 s. **Cold to hot does not fit inside it, and it never will.** 12.6 s of
that is GCE reaching RUNNING, which is not ours to tune, and the remaining 11.6 s is a Debian
kernel and a PostgreSQL start. A loaded shard is slower still: the blocking prewarm phase reads its
budget (`SHARD_PREWARM_BLOCKING_MB`, default 6144) off a disk provisioned at 515 MB/s, which is
about 12 s more.

**The report the previous session left, that the agent calls the shard ready ninety seconds before
Postgres accepts, is wrong.** The agent's first answer was `waking`, and Postgres was accepting on
the same poll that first reached the agent. The agent never lied. The wake is just longer than the
budget.

### What was done about it

**Not raising the timeout.** The 20 s exists because the cold tier runs in parallel with the hot
tier and a search cannot wait; raising it moves the wait onto every search that routes to a cold
domain, which is the cost the whole design exists to avoid.

**The wake is started earlier instead.** `shard_backend.prewake_subject()` is called from
`runner/worker.py` at the moment a run is CLAIMED, minutes before the cold tier asks for anything.
It starts the shards the subject's own CPC symbols point at, plus the `unclassified` shard, which
`route()` always emits. Those symbols are 25% of the routing distribution and are knowable with one
indexed query and none of the evidence the cheap tiers produce. The 25 s is then spent inside work
the search was doing anyway, and by the time `hot_domains` is asked the shard is hot rather than
five seconds short of it.

**And a first-query miss is accepted, deliberately.** A domain that prewake did not guess and that
only the candidate evidence routes to will not be hot on the first search that wants it. It is
woken anyway, the lease keeps it up for fifteen idle minutes, and the next search that routes to
the same domain gets it hot. A CPC subclass is coarse on purpose so that this reuse happens; a
finer domain would wake a shard per query and never reuse one. The cold tier already reports what
it did in `Result.tiers["cold"]`, so a miss is visible rather than silent.

Rejected, and why:

* **A warm standby.** A shard kept RUNNING is $1.04/hour, $761/month. Eight of them is the entire
  cost argument for the cold tier, undone.
* **SUSPENDED instead of TERMINATED.** Suspend writes 124 GB of guest memory to disk per shard,
  which is charged, and a resume takes zone capacity exactly as a start does, so it buys nothing
  against the failure below.
* **Prewarm from a snapshot.** A snapshot shortens the disk read, not the kernel boot, and the
  kernel boot is where 24 of the 25 seconds are.

---

## The second measurement, which matters more

**`instances.start` on a TERMINATED `c4-highmem-16` fails.** MEASURED 2026-08-22, three times in
half an hour in `us-central1-b`: the `start` POST returns success, and about six seconds later the
operation reaches DONE carrying `ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`, with the instance
still TERMINATED. `instances.create` fails the same way. A stopped VM holds no capacity
reservation, so this is not a build-time accident: it is the cold tier's steady state.

Three things follow, and all three are in the code:

1. **`create` retries a capacity error** in the same zone with linear backoff
   (`SHARD_CREATE_RETRIES`, `SHARD_CREATE_RETRY_SECONDS`) and deletes a half-created instance
   before giving up. Quota, flag and permission errors are not retried.
2. **`_start` watches its own operation** and reissues it, on a daemon thread, OFF the wake path.
   `ensure` sees the instance still TERMINATED, reports `cold` and returns, so a search never waits
   for a zone to find capacity. `wake()` returns the last error per shard in its `errors` key, so a
   domain that contributed nothing because of a stockout is distinguishable from one that
   contributed nothing because it is empty.
3. **The fleet is spread over four zones**, two shards each in `us-central1-a`, `-b`, `-c` and
   `-f`. Eight shards in one zone means one stockout takes the whole cold tier out at once; two per
   zone costs a quarter of it, and the tier is already designed to answer with the shards that came
   up. Cross-zone inside a region is internal-IP traffic and a shard answers with
   `(publication_id, score)` rows, so it is not a latency argument.

---

## The files

| File | What it is |
|---|---|
| `shards.tsv` | THE shard table. `shard`, `vm`, `zone`, `domains`. Read by bash and by Python |
| `plan_to_table.py` | regenerates the `domains` column from workstream O's `plan.json`; `--check` fails on drift |
| `shardctl.sh` | the whole lifecycle from the controller: create, bootstrap, schema, start, stop, wake, health, ready, reap, cost |
| `bootstrap.sh` | runs ON the shard as root: PostgreSQL 17, pgvector, pg_prewarm, the agent, Tantivy, the prewarm unit |
| `shard_agent.py` | :8639. The shard's own honest `cold` / `waking` / `hot` answer |
| `tantivy_server.py` | :8635. A real Tantivy index reader that says `available: false` until an index with documents in it opens |
| `tantivy_serve.sh` | execs workstream C's compiled server if it is installed, otherwise the Python one |
| `prewarm.py` | `pg_prewarm` in two phases: a bounded blocking budget, then the rest in the background |
| `idle_reaper.py` | the lease-driven fifteen-minute shutdown. Cron it on the controller |
| `verify_ids.py` | a shard's publication ids ARE the hot corpus's ids, or the shard is not allowed to serve |
| `wakebench.py` | the cold-to-hot measurement above |
| `sql/shard_status.sql` | the readiness ledger, in the SHARD database |
| `systemd/` | the three units |

The Python side is `src/retrieval/shard_backend.py`, which implements both
`shard_manager.ShardManagerBackend` and `shard_router.ShardRouterBackend`.
`docs/shard_and_global_seams.md` is the contract.

---

## The domain table is not ours

The eight-way split of the 602 CPC subclasses is workstream O's, computed once and written to
`data/logs/plan.json` in the `O-release` worktree (archived in `~/v3/preserved/F-release-data.tgz`).
`plan_to_table.py --plan <plan.json> --check` fails if `shards.tsv` has drifted from it.

**Do not hand-edit the domain lists.** A subclass loaded onto one shard and routed to another is a
shard that wakes, is queried and answers nothing, which downstream is indistinguishable from a
genuine miss, and a miss scores as a recall failure.

`domain_08` carries the `*` catch-all and the `unclassified` route. `shard_of("")` and
`shard_of(shard_router.UNCLASSIFIED)` both land there, because `corpus_niche.subclass_of` returns
`""` where `domain_of` returns `"unclassified"` and they are the same 1,024,320 publications, 20.6%
of the corpus. A shard reachable under only one of the two names makes that share unreachable.

The per-domain masses in `plan.json` read 142,148 for six domains and 142,147 for two, which cannot
arise from packing atomic subclasses. **The ASSIGNMENT is real and is used; the SIZING is a
placeholder and is not used anywhere.** Workstream O is re-deriving it.

---

## The three states, and who is entitled to each

```
cold      the CONTROLLER says this: the instance is TERMINATED or SUSPENDED
waking    the instance is up and the box has not said it is serving
hot       THE SHARD says this, never the controller
unknown   no such instance, or GCE would not answer. Never started, never waited for
```

`hot` requires all three of: Postgres accepting, `shard_status.state = 'ready'`, and the blocking
half of prewarm returned. The controller may only ever downgrade the shard's answer.

**A shard that is up and empty is not hot, and that is the point.** An empty result set and a
genuine miss are indistinguishable to fusion, and a miss is scored as a recall failure, so a shard
that cannot answer must say so rather than answer nothing. `shard_status.state` is `building` from
bootstrap until whoever loaded the shard commits the flip in the load's own transaction. Tantivy
answers the same way: `available: false` unless a real index with documents in it opens.

---

## The lease is the only thing keeping a shard awake

`shard_leases` (`sql/009_durable_runs.sql`, adopted on the live database) is the store, and there is
not a second one. `ensure` and `wake` take or refresh a lease for the run bound with `bind_run`,
`runstore.Heartbeat` refreshes it while the run lives, `worker.execute` releases it in a `finally`,
and `idle_reaper.py` stops any shard nobody holds.

The reaper's order of tests IS the safety property:

1. expire the leases nobody heartbeats,
2. a held lease keeps the shard,
3. inside the idle window, keep,
4. **an in-flight query keeps the shard even with no lease**, because the thread that heartbeats a
   lease can die while the database keeps working, and stopping a VM under a running query loses
   the work and returns a wrong answer to a live search,
5. cannot tell whether anything is running: keep until the hard ceiling, then stop and say so.

```
* * * * * cd /path/to/repo && PYTHONPATH=src .venv/bin/python ops/shards/idle_reaper.py \
          >> /var/log/patents-shard-reaper.log 2>&1
```

---

## Publication ids

**A shard's publication ids must be the hot corpus's publication ids.** `retrieval.cold` hydrates
the family key of every cold hit from the shard and writes it into the retriever's family map,
filling gaps and never overwriting. A shard that renumbered would silently attribute a hot family
to a cold document: not a crash, not an empty result, a wrong answer that looks like a right one.

`verify_ids.py` samples the shard and compares against the hot corpus by primary key, and
`shardctl.sh ready` refuses to flip `shard_status` on a mismatch. The gate is upstream of the query
rather than a fallback inside it, so a renumbered shard is never `hot` and is never asked.

---

## Running it

```bash
./shardctl.sh list                     # the table
./shardctl.sh status                   # every shard, instance state plus its own answer
./shardctl.sh cost                     # the arithmetic, from the machine and disk actually used

./shardctl.sh create-all               # idempotent, with stockout retry, leaves everything stopped
./shardctl.sh bootstrap domain_03      # pg17 + pgvector + agent + tantivy + prewarm
./shardctl.sh schema domain_03         # the repo's own migrations, 002 excluded (see the script)
./shardctl.sh verify-ids domain_03
./shardctl.sh ready domain_03 <generation>

./shardctl.sh wake domain_03           # start and wait for hot, through the real backend
./shardctl.sh reap --dry-run
./shardctl.sh verify-cold              # exit 1 unless every shard is TERMINATED
```

`bootstrap` borrows an external address for the apt install and takes it off again: there is no
Cloud NAT in this project, so that window is the whole of a shard's internet access and it exists
only for the length of an install. A shard's steady state is private IP only.

`schema` excludes migration 002 on purpose. MEASURED against a fresh PostgreSQL 17 + pgvector 0.8.6
shard: `CREATE INDEX ix_bench3072_hnsw` fails with `column cannot have more than 2000 dimensions
for hnsw index`, and `bench_emb_3072` is 3072, so 002 as written cannot be applied anywhere.

---

## Turning it on

The backend is registered from `webapp.py` and is **OFF unless `SHARD_BACKEND_ENABLED` is set.**
Registering it makes `cold.available()` True, which costs a routing query and VM time on every
search, and until a corpus release is loaded every shard answers `building`, so the cold tier could
only pay and return nothing. Flipping it on is the same decision as "there is a corpus on the
shards", and it belongs in the environment of whoever took it.

---

## Cost

`./shardctl.sh cost 8` prints it from the machine and disk types the script actually uses. At the
provisioned 4500 IOPS / 515 MB/s the standing disk cost is what the fleet costs with every VM
stopped; a running `c4-highmem-16` is $1.04/hour on top.
