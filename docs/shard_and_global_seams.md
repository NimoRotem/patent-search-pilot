# The shard and global-search seams

Owner of the contract: workstream B (retrieval). Implementor: workstream E, which owns the VMs.
The executable copies are `src/retrieval/shard_router.py`, `src/retrieval/shard_manager.py` and
`src/retrieval/global_search.py`. If this file and the code disagree, the code is right.

All three seams are live and inert today: they import, they are called, and they return nothing.
That is the point. A channel that returns an empty list is the existing fail-soft contract, so
retrieval already behaves correctly while E is still building.

---

## 1. `shard_router`: which domains a query needs

### A domain is a CPC subclass

Four characters, e.g. `B66C`. The eight seed subgroups sit inside five subclasses, and the
neighbouring art the benchmark subjects need (`B25J` robot grippers, `B65G` conveyors, `B23Q`
machine tools) is one subclass away, not one group away. Finer and the router wakes a shard per
query with no reuse; coarser and `B65G`'s 6.8M classification rows become one route that carries no
information.

### The routing distribution

```python
shard_router.route(conn, *, candidate_pids=(), predicted_cpc=(), citation_pids=(),
                   family_key=None, max_routes=5, include_prior=True)
    -> [{"domain": str, "weight": float, "sources": {source: float}}]   # best-first
```

| Source | Weight | What it is |
|---|---|---|
| `candidates` | 50% | Domain distribution of the candidate families the cheap tiers already found, rank-decayed by `1/(40+rank)`, one vote per family |
| `predicted_cpc` | 25% | The subject's own symbols, most specific first, symbol *i* weighted `1/(i+1)` (`Retriever.subject_cpc`) |
| `citations` | 15% | Domain distribution of the citation-graph neighbours, same rank decay |
| `prior` | 10% | Corpus-wide domain frequency, computed once per process |

A source that produced nothing does not silently vanish: its weight is redistributed across the
sources that did produce something, and every route carries its `sources` breakdown so a routing
decision can be explained.

Weights in the returned list are normalised to sum to 1 across the routes returned.

### The unclassified route is never dropped

MEASURED: **1,024,320 publications carry no classification at all**, 20.6% of the corpus. A router
that only wakes classified shards makes that share unreachable, and unclassified documents skew old
and foreign, which is the exact population the gold citation lists are drawn from.

`route()` therefore always emits a `"unclassified"` route, appending it at the weight it earned or
at its corpus share if it earned none, and can never return an empty list. A publication with no
symbols votes for it; a publication with several symbols spreads one vote across their domains
rather than voting once per symbol.

`shard_router.domain_of(symbol)` and `shard_router.domains_of_publications(conn, pids, weights)`
are exported so E can reuse the same domain definition rather than reimplement it.

### The wake seam

```python
class ShardRouterBackend:
    def wake(self, routes) -> dict: ...        # routes = route() output

shard_router.register_backend(backend)
shard_router.wake(routes) -> dict              # never raises
```

Today `wake` returns `{"woken": [], "state": "not_implemented", "routes": [...]}`.

---

## 2. `shard_manager`: shard lifecycle and connections

```python
class ShardManagerBackend:
    def state(self, domain) -> str                      # "hot" | "waking" | "cold" | "unknown"
    def ensure(self, domains, timeout=WAKE_TIMEOUT) -> dict[str, str]
    def connection(self, domain)                        # a psycopg connection, or None
    def release(self, domain, conn) -> None

shard_manager.register_backend(backend)
shard_manager.hot_domains(routes, timeout=20.0) -> list[str]
```

`connection()` and not `search()`, deliberately: the cold shards hold the same schema as the hot
corpus, so the cold dense and cold lexical channels are the same SQL this package already runs,
pointed at a different host. Handing back a connection keeps one implementation of each channel
instead of two that drift apart.

`WAKE_TIMEOUT` defaults to 20 s (`SHARD_WAKE_TIMEOUT`). **A shard that is not hot within the
timeout is not waited for.** The architecture runs the cold tier in parallel with the hot one
precisely so that a cold miss costs the art it would have added and nothing else.

Every module-level wrapper (`state`, `ensure`, `connection`, `release`) swallows exceptions and
degrades: unknown state, no connection, no crash.

---

## 3. `global_search`: the 170M catalog and the external APIs

```python
class GlobalBackend:
    def available(self) -> bool
    def search(self, query, *, subject=None, mode=None, limit=2500, qvec=None) -> list[tuple]
    def family_keys(self, publication_ids) -> dict

global_search.register_backend(backend)
global_search.search(query, subject=..., mode=..., limit=..., qvec=...)   # never raises
```

`search` returns `[(publication_id, score)]` best-first. `publication_id` is the string
`"fed:<PUBNUM>"` for a document with no local row (the convention `federation.py` already uses and
`Result.is_external` already tests) or a local bigint when the global tier resolved the hit onto a
publication this corpus holds.

`family_keys` maps those external ids to family keys. Without it a global hit cannot be deduped
against a local one and the same disclosure appears twice. Returning `{}` is allowed: the
orchestrator then treats each external id as its own family, which is the safe direction because
nothing is silently merged.

The channel name in fusion is `"global"`, weighted 0.90 in `fusion.CHANNEL_WEIGHTS`, the same
standing as the federated bridge: the hits arrive already ranked by a real engine, and they are the
only source that can reach art this corpus does not hold at all.

### Why this tier exists at all

The `schmalz` benchmark subject has ten cited documents, four of them classified in acoustics,
exhaust silencers, vacuum cleaners and power tools, outside the eight indexed CPC branches. Its
measured `fused:all` recall@2500 is 0.4. No reordering of documents this corpus does not hold will
ever produce the missing six. That is a reach failure, and the global tier is the only fix for it,
which is why the architecture runs it in parallel with local search and never after it.
