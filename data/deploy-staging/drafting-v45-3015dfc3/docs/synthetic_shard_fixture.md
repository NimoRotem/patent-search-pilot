# The synthetic shard fleet

`src/retrieval/testing.py`. A fake domain-shard fleet and a fake global backend, behind the real
seams, so the cold and global tiers can be tested before a single VM exists.

It lives in `src/` and not in `tests/` deliberately. Three workstreams need it: G wires the tiers
against it, and **E and F run the same assertions against their real implementations**. A seam
tested by one harness on one side and another on the other is a seam that is only tested once.

If this file and `src/retrieval/testing.py` disagree, the code is right.

## The shape of it

```python
from retrieval import testing

shards = {"B66C": testing.shard("B66C", docs=[(101, "FAM-A"), (102, "FAM-B")]),
          "unclassified": testing.shard("unclassified", docs=[(9001, "FAM-C")])}

with testing.installed(shards=shards) as (manager, router, _global):
    result = retriever.search(brief, config=["dense", "bm25", "cold"])

assert result.channel_hits["cold:dense"] == [101, 102]
assert manager.leaked_connections() == {}
```

`installed()` registers a `SyntheticShardManager` and a `SyntheticRouter` (and optionally a global
backend) and puts back whatever was there on the way out, including out of an exception. A test
that leaves a synthetic shard registered turns every later test in the process into a cold-tier
test, which is the kind of failure that gets attributed to the wrong change a week later.

`testing.reset()` unregisters everything; `register_backend(None)` restores each seam's own inert
default.

## A shard

```python
testing.shard(domain, docs=(), channels=None, fail_on=(), latency=0.0, hot=True)
```

| Argument | What it does |
|---|---|
| `docs` | `[(publication_id, family)]` or `[(publication_id, family, score)]`, best-first |
| `channels` | `{kind: [ShardDoc]}`, overriding `docs` for one channel |
| `fail_on` | `{"dense"}`: that channel raises when queried, AFTER the shard was hot |
| `latency` | seconds each query sleeps, for overlap and budget assertions |
| `hot` | `False` makes it a shard that has to be woken |

A shard records `sql_log` as `[(kind, sql, params)]`, and `opened` / `released` connection counts.
`kind` comes from `testing.classify(sql)`, which is the one place the double has to know the
queries: it recognises the channel from the SQL rather than being told, so a channel that changes
its query shape shows up here rather than silently returning the wrong fixture.

## The failures it exists to inject

| How | What it simulates |
|---|---|
| `hot=False`, `wake_after=2.0` | a shard that has to start, and takes two seconds |
| `never_wake={"B65G"}` | still "waking" when `SHARD_WAKE_TIMEOUT` expires |
| `connect_error={"B25J"}` | `connection()` raising instead of returning |
| `ensure_error=True` | the fleet is unreachable at all |
| `fail_on={"dense"}` | one channel raises mid-query on one shard |
| `latency=` / a subclassed `rows_for` | a slow or hung shard, for the tier budget |
| `SyntheticRouter(error=True)` | `wake()` raising |
| `routing_connection(error=True)` | the routing evidence cannot be read |
| `SyntheticGlobal(available=False)` | a backend mid-build, which MUST say so |
| `SyntheticGlobal(error=...)` | the catalog is down |

Every one of those must leave the local answer intact and must not raise. That is the whole
contract: a shard or a global backend that is unavailable degrades, it does not fail a search.

## Routing

`shard_router.route()` is real code and is not stubbed. It reads three things from the hot
database, so the tier exposes the connection it uses as a seam:

```python
monkeypatch.setattr(cold, "route_connection",
                    lambda: testing.routing_connection(
                        prior={"B66C": 4000, "B65G": 3000, "unclassified": 1000},
                        symbols=["B66C1/0225"],                  # the subject's own
                        classified={7: ("B25J15/06",)}))         # candidate votes
shard_router.reset_prior()      # the corpus prior is cached per PROCESS
```

`reset_prior()` matters: `historical_prior` is a 7.9 s `GROUP BY` over 53,473,700 classification
rows and is computed once per process, so a test that does not reset it measures the previous
test's corpus.

## What it does not do

Postgres. The fake connection answers by recognising which channel is asking, so it proves the
WIRING: routing, waking, parallelism, family collapse across tiers, caps, pooling, connection
release and degradation. It cannot prove that `to_tsvector` or `<=>` behave on a real shard, and it
cannot prove a shard's schema matches. Those need a real shard, and the point of the double is that
E can swap it out under the same tests.

## The assertions worth reusing

From `tests/test_retrieval_cold.py`, in the order they matter:

* `manager.leaked_connections() == {}` on both the success and the failure path. Nothing here fails
  loudly when a connection is kept, so a leak is invisible until the shard runs out.
* the cold SQL is character-for-character the hot SQL, captured from a recording connection. This
  is what keeps `connection()`-not-`search()` honest.
* a family that exists on a shard and in the hot corpus reaches the answer once, and the
  defect-injected twin of that test proves it goes red when the family hydration is removed.
* hydration never relabels a publication the hot corpus already knows.
* a `threading.Barrier` that a hot channel and a shard query must both reach, which sequential
  execution cannot satisfy and which cannot pass by luck.
