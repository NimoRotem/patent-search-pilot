# The shard and global-search seams

Owner of the contract: workstream B (retrieval). Implementor: workstream E, which owns the VMs.
The executable copies are `src/retrieval/shard_router.py`, `src/retrieval/shard_manager.py` and
`src/retrieval/global_search.py`. If this file and the code disagree, the code is right.

All three seams are live and inert today: they import, they are called, and they return nothing.
That is the point. A channel that returns an empty list is the existing fail-soft contract, so
retrieval already behaves correctly while E is still building.

**They are now CONSUMED.** `src/retrieval/cold.py` turns a shard connection into results and
`Retriever._tier_global` turns a global backend into a channel. Section 4 is what the orchestrator
does with them, section 5 is what E has to guarantee, and section 6 is the synthetic fleet E can
test its real implementation against without waiting for anyone.

MEASURED 2026-08-22, on the live corpus, three arms of three runs each: naming `cold` and `global`
in a preset with no backend registered returns a bit-identical answer (same channel hit counts,
same 2,994 families, same recall at 100, 500 and 2,500) in 6.62 s against 7.09 s without them,
i.e. inside the noise. Inertness is not an intention here, it is a number.

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

**`weights` is the electoral roll, not a set of adjustments.** A pid absent from a supplied weights
map does not vote. It used to fall back to a full vote of 1.0, which INVERTED the family dedup it
exists to implement: `_rank_weighted` suppresses every member of a family after the first, so the
five suppressed members of a six-member family each voted 1.0 while the one that survived voted
1/41. Measured on the regression that found it, a single family took 98.6% of the routing
distribution away from a genuinely distinct candidate (`B25J` 0.986 against `B66C` 0.004; after the
fix, 0.53 and 0.47). Fixed 2026-08-22 with `tests/test_shard_router.py::test_one_family_votes_once`.

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

`records(publication_ids) -> dict` is optional and maps external ids to a display record. An
external hit has no local row, so nothing downstream can read its title or abstract out of
`chunks`: an id missing from `Result.external` renders as a blank card and reranks on an empty
passage. The record only has to duck-type `federation.FederatedHit`; `.title` and `.abstract` are
what `fusion.best_text` reads.

`GLOBAL_TIMEOUT` (`GLOBAL_SEARCH_TIMEOUT`, 60 s) is checked BEFORE the call starts, because there
is no way to interrupt one that has. **A backend that talks to an external API must bound its own
request timeouts.**

### Why this tier exists at all

The `schmalz` benchmark subject has ten cited documents, four of them classified in acoustics,
exhaust silencers, vacuum cleaners and power tools, outside the eight indexed CPC branches. Its
measured `fused:all` recall@2500 is 0.4. No reordering of documents this corpus does not hold will
ever produce the missing six. That is a reach failure, and the global tier is the only fix for it,
which is why the architecture runs it in parallel with local search and never after it.

---

## 4. What the orchestrator does with all this

### The lanes

`orchestrator._run_phase` runs a phase over two independent lanes. The `db` lane is the hot corpus:
its bound is a share of one Postgres box's 100 connections. The `remote` lane is the cold shards
and the global tier: other hosts, and a task that can sit for the whole 20 s wake. One semaphore
across both would let a waking shard hold a slot the dense channel needed, which is exactly the
serialisation the cold tier exists to avoid. A remote worker never calls `base.worker_conn`, so the
remote pool costs the hot database nothing.

### The phases

| Phase | Hot | Remote |
|---|---|---|
| 1 | every channel whose input exists at request start | `cold` (routes, wakes, queries), `global` |
| 2 | `citation`, `qbe`, around the fused strong seeds | `cold` again: re-routed WITH the candidates |

Phase 2 re-routes because the candidate distribution is 50% of the mix and does not exist until the
cheap tiers have answered. A domain only that evidence indicates is woken then and gets the phase 1
channels run against it as well, since it was not reachable when they ran.

The `citations` source of the mix has no input at either point: citation neighbours are produced by
the citation channel, which runs in the same phase the routing decision starts, so consulting them
would mean waiting for the hot tier. `route()` redistributes that 15%, which is the documented
behaviour and not a silent loss.

**Phase 2 seeds are LOCAL ids only, and a local member always represents its family.** `citation`
joins `publications.id` and `qbe` reads that publication's chunks, so an external id is not a
weaker seed, it is a bigint cast error: measured on a live search the moment a global backend was
registered, `channel_citation_family` failed with `invalid input syntax for type bigint:
"fed:EP9999999"` and soft-degraded to zero hits, while `qbe` survived only because it reads the
first five seeds and the external one happened to sit lower. The federated bridge never hit this
because it fuses AFTER the local search; the global tier fuses in phase 1. `canonical_reps`
likewise now prefers a local member over an external one whatever the ranks say, because an
external id carries a title and an abstract at best and a local row carries chunks, claims, dates
and figures.

### The channel names

A cold channel is `cold:<kind>`: `cold:dense`, `cold:bm25`, and so on, one per channel the preset
itself names. `fusion.channel_weight` resolves the prefix, so **`cold:dense` carries exactly the
weight of `dense`**. The same query against a different host is the same evidence, and two weight
tables drift: the day one is retuned and the other is not, the ranking depends on which VM is
awake. The global tier is one channel named `global`, weighted 0.90.

Hits are pooled across shards, then collapsed to families once at the tier's cap. Each shard
collapses its own hits, but two shards can still hold two members of one family.

### The budgets

| Name | Default | What it bounds |
|---|---|---|
| `SHARD_WAKE_TIMEOUT` | 20 s | how long `hot_domains` waits for a waking shard |
| `SHARD_TIER_BUDGET` | 90 s | the whole cold tier, checked at start and between shards |
| `SHARD_MAX_FANOUT` | 5 | shards queried at once, one connection each |
| `GLOBAL_SEARCH_TIMEOUT` | 60 s | checked before the global call starts |

`SHARD_TIER_BUDGET` abandons a shard's RESULT, never its connection: the per-domain task releases
in its own `finally`, so a shard that overruns is dropped from the answer and does not leak.

---

## 5. What workstream E has to guarantee

1. **A publication id on a shard is the SAME id as in the hot corpus.** The cold tier hydrates
   family keys for ids the hot map has never seen, filling GAPS only and never overwriting, so a
   shard that renumbered its publications would silently attribute a hot family to a cold
   document. If ids cannot be global, return `"fed:<PUBNUM>"` strings instead and treat the shard
   as an external source.
2. **`release(domain, conn)` must reset or discard session state if connections are pooled.** The
   cold tier applies the ANN scan profile (`hnsw.ef_search`, `hnsw.iterative_scan`,
   `hnsw.max_scan_tuples`) to the connection it is handed, exactly as the hot path does to its own,
   because a dense channel at the shard's defaults is a different query.
3. **`ensure` must honour its timeout.** It is the only thing standing between a shard that will
   not wake and a search that never returns.
4. **A shard holds the same schema as the hot corpus**, at least `publications` (`id`,
   `publication_number`, `simple_family_id`, the date columns), `chunks` (`embedding`, `tsv`,
   `kind`, `text`), `classifications`, `citations` and `parties`. The cold channels are the hot
   SQL; there is deliberately no second implementation to adapt.
5. **`available()` must be False while a shard set is mid-build.** An empty result and a genuine
   miss are indistinguishable downstream and a miss scores as a recall failure.

---

## 6. Testing against it before anything is built

`src/retrieval/testing.py` is a synthetic fleet behind these seams: shards that wake on demand or
refuse to, connections that fail, channels that raise mid-query, and a global backend that is slow,
broken or unavailable. It is in `src/` and not in `tests/` on purpose, so that E and F can run the
same assertions against their real implementations. `docs/synthetic_shard_fixture.md` is how to use
it; `tests/test_retrieval_cold.py`, `tests/test_retrieval_global.py` and `tests/test_shard_router.py`
are worked examples.
