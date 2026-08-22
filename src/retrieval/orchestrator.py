"""The Retriever: one object, every channel, and the pass that runs them.

Adaptive multi-channel retrieval cascade + RRF + family dedup + rerank (spec §6).

Separate channels, fused by reciprocal-rank fusion (never mixing raw incomparable scores):
 1 exact/phrase/proximity   2 BM25 (top ~1000)      3 dense (top ~1000)
 4 CPC/IPC hierarchy        5 citation+family graph 6 query-by-example
 7 cross-lingual DE<->EN    8 bibliographic (assignee/inventor)

Date/status filtering (spec §5) is AND-ed into every candidate query when a Subject+Mode is
given, so only citable prior art is returned. Widths kept wide even though the corpus is small
, the pilot measures recall, not speed.

STRUCTURE. Each channel lives in its own module as a mixin over `base.RetrieverBase`, which owns
the connection, the ANN width and the chunk-to-publication aggregation they all share. The mixins
are composed here rather than in `__init__` so that importing one channel does not drag in the
whole package.

CONCURRENCY. `search()` runs in two phases. Phase 1 is every channel whose inputs exist at request
start, fanned out over a bounded, reused thread pool. Phase 2 is `citation` and `qbe`, which expand
around and re-query from the STRONG SEEDS fused out of phase 1, so they are a real data dependency
and not an ordering preference. `base.RetrieverBase` resolves a connection per thread, so no two
channels ever share one; the pool is process-wide and reused precisely so the connection count
stays at the pool's size rather than growing by one pool per search.

Two invariants that the tests in `tests/test_retrieval_concurrency.py` exist to protect:

* Every task in a phase is SUBMITTED before any result is collected. Submitting one and blocking
  on it before submitting the next is sequential execution wearing a thread pool.
* Results are assembled in PRESET order, never completion order. RRF consumes channels in
  iteration order, so letting a fast channel jump the queue would silently reorder ties and make
  the same query return different pages on a loaded box.

LANES. A phase runs over two independent lanes, because the two have nothing in common but a
clock. The `db` lane is the hot corpus: its bound is a share of ONE Postgres box's 100
connections. The `remote` lane is the cold domain shards and the global tier: other hosts, other
connections, and a 20 s shard wake that must never sit in front of a hot channel. One semaphore
across both would let a waking shard hold a slot the dense channel needed, which is precisely the
serialisation the cold tier exists to avoid.
"""
from __future__ import annotations

import atexit
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import embed

from . import base as _base
from . import channels, cold, global_search
from .base import RetrieverBase
from .citations import CitationMixin
from .cpc import CpcMixin
from .dense import DenseMixin
from .exact import ExactMixin
from .family import FamilyMixin
from .fusion import RERANK_TOP, FusionMixin
from .legal import as_mode
from .lexical import LexicalMixin
from .qbe import QbeMixin

#  Channel presets. A caller may also pass an explicit list of channel names.
#
#  `cold` and `global` are TIERS, not channels: `cold` mirrors this preset's own channels onto the
#  woken domain shards (`cold:dense` is the same SQL as `dense`), and `global` is the 170M catalog.
#  Both are inert and free until their backend is registered, so a preset naming them behaves
#  exactly as it did before workstream E lands: no task, no query, no key in `channel_hits`.
PRESETS = {
    "keyword": ["exact", "bm25"],
    "vector": ["dense"],
    "hybrid": ["exact", "bm25", "dense", "cpc", "cold", "global"],
    "hybrid_rerank": ["exact", "bm25", "dense", "cpc", "cold", "global"],
    #  `exact` is in the deep presets because the agent already PRODUCES the phrases: `plan()`
    #  asks for "exact multiword phrases to match" and passes them to `search()`, where a preset
    #  without `exact` threw them away. See eval/RESULTS.md R9.
    "agentic": ["dense", "brief_dense", "exact", "cpc", "citation", "qbe", "biblio",
                "crosslingual", "cold", "global"],
    #  Claim focus BOOSTS claim text, it does not delete the rest of the patent.
    #  MEASURED (RECALL_STUDY_2026-08-02.md): this preset used to omit `dense`, so
    #  description paragraphs were unsearchable. On the case that prompted the rebuild the
    #  best passage of the #1 result was a description paragraph, the best passage of the
    #  reference the searcher named was a description paragraph, and only 10 of the 25
    #  displayed cards matched on a claim at all.
    "claim_agentic": ["claim_dense", "dense", "brief_dense", "claim_bm25", "exact", "cpc",
                      "citation", "qbe", "biblio", "crosslingual", "cold", "global"],
}


# ---- concurrency bound ---------------------------------------------------------------------
# The corpus box reports `max_connections = 100` and serves the live application from the same
# server. This is the share ONE search process may hold, not the server's total: the agent runs
# several Retrievers at once and each fan-out thread keeps its own connection for the life of the
# thread, so the ceiling that matters is threads-per-process, not channels-per-search.
DB_CONNECTION_BUDGET = 12

# Worker threads the shared pool may ever create, and therefore the most connections this process
# adds. Kept below the budget so an agent element pass and a seed pass can overlap without
# reaching it.
MAX_DB_CONCURRENCY = 8

# Channels allowed to be inside the database simultaneously in one search. Six because the box has
# 8 vCPU and the page that renders the search needs some of it, because six channels at 256 MB
# `work_mem` is already 1.5 GB of sort space, and because `max_parallel_workers` is 8 server-wide
# so six channels each able to take parallel workers is already oversubscribed.
DB_CONCURRENCY = int(os.environ.get("RETRIEVAL_DB_CONCURRENCY", "6"))

#  The stated answer to "how many database operations can this run at once". A channel is one
#  operation at a time: the channels that issue several statements (exact loops its phrases, qbe
#  and crosslingual loop their vectors) do so sequentially on their own thread's connection.
MAX_CONCURRENT_DB_OPERATIONS = DB_CONCURRENCY

# ---- the remote lane -------------------------------------------------------------------------
# The cold shards and the global tier. Separate from the database lane on purpose: these tasks
# spend other hosts' resources, not this Postgres box's connection budget, and one of them can sit
# for the full 20 s shard wake. Sharing the db lane's semaphore would let that wake block a hot
# channel, which is exactly the serialisation the parallel cold tier exists to prevent.
#
# Two remote tasks per search (the cold tier and the global tier), so six is three overlapping
# searches before anything queues, and a queued tier task carries its own deadline: it is skipped
# rather than added to the critical path of a search whose local answer is already complete.
MAX_REMOTE_CONCURRENCY = 6
REMOTE_CONCURRENCY = int(os.environ.get("RETRIEVAL_REMOTE_CONCURRENCY",
                                        str(MAX_REMOTE_CONCURRENCY)))

DB_LANE = "db"
REMOTE_LANE = "remote"

#  Losing one of these means the answer is wrong rather than thinner, so they keep today's
#  behaviour exactly: the exception propagates. Dense retrieval is the dominant signal in every
#  measurement of this corpus, and a search that quietly returned only its lexical channels would
#  look like a bad result rather than a broken one.
REQUIRED_CHANNELS = frozenset({"dense", "claim_dense", "brief_dense"})

#  Enrichment. Any of these may fail without invalidating the answer, so the failure is recorded
#  through `failclosed` (which still RAISES in benchmark mode, so no measurement is ever taken
#  from a degraded run) and the channel contributes nothing.
#
#  `global` and `cold` are here because a tier that reaches another host is enrichment BY
#  DEFINITION: a global outage or a shard that will not wake costs the art it would have added and
#  nothing else. Their module-level wrappers already swallow, so this is the second line, not the
#  first.
OPTIONAL_CHANNELS = frozenset({"bm25", "claim_bm25", "exact", "cpc", "citation", "qbe",
                               "biblio", "crosslingual", "cold", "global"})


def _is_optional(name):
    """Optional by NAME or by tier. A `cold:<kind>` result is a tier product, not a channel a
    caller asked for, so it can never be the thing that fails a search."""
    return name in OPTIONAL_CHANNELS or cold.is_cold(name)


_POOL = None
_POOL_LOCK = threading.Lock()
_REMOTE_POOL = None


def resolve_concurrency(n=None):
    """Validate a requested bound against the connection budget."""
    n = DB_CONCURRENCY if n is None else int(n)
    if n < 1:
        raise ValueError(f"retrieval concurrency must be at least 1, got {n}")
    #  The pool has MAX_DB_CONCURRENCY worker threads. A bound above that is not a bound: the
    #  surplus tasks simply queue INSIDE the executor while the caller believes it asked for more
    #  parallelism, so the number is wrong in the one direction that is hard to notice.
    if n > MAX_DB_CONCURRENCY:
        raise ValueError(
            f"retrieval concurrency {n} exceeds the pool size {MAX_DB_CONCURRENCY}, so it could "
            f"never be reached. Raise MAX_DB_CONCURRENCY too, and only up to the per-process "
            f"connection budget {DB_CONNECTION_BUDGET}, which is itself a share of the server's "
            f"max_connections of 100.")
    return n


def _pool():
    """One process-wide pool, created once and reused.

    A pool per search would be correct and ruinous: `base.worker_conn` caches a connection per
    THREAD, so fresh threads every search means fresh connections every search, and the server's
    100 would be gone in an afternoon. Reusing the threads is what keeps the connection count at
    the pool's size, once, for the life of the process.
    """
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = ThreadPoolExecutor(max_workers=MAX_DB_CONCURRENCY,
                                           thread_name_prefix="retrieval")
    return _POOL


def _remote_pool():
    """The lane for the cold shards and the global tier. Its own pool, created on first use.

    A remote worker never calls `base.worker_conn`, so it never holds a connection to the hot
    corpus: the cold tier binds the retriever to the SHARD's connection on its own thread, and the
    routing probe opens and closes a transient one of its own. That is why this pool's size is not
    charged against `DB_CONNECTION_BUDGET`.
    """
    global _REMOTE_POOL
    if _REMOTE_POOL is None:
        with _POOL_LOCK:
            if _REMOTE_POOL is None:
                _REMOTE_POOL = ThreadPoolExecutor(max_workers=MAX_REMOTE_CONCURRENCY,
                                                  thread_name_prefix="retrieval-remote")
    return _REMOTE_POOL


def shutdown_pool(timeout=10.0):
    """Close every worker's connection, then drop the pool. Idempotent."""
    global _POOL, _REMOTE_POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
        remote, _REMOTE_POOL = _REMOTE_POOL, None
    if remote is not None:
        try:
            remote.shutdown(wait=False)
        except Exception:                                              # noqa: BLE001
            pass
    if pool is None:
        _base.close_worker_conn()          # nothing was ever forked; close this thread's, if any
        return
    #  Every worker must run the close, so they are held at a barrier until all of them have
    #  arrived; otherwise one fast thread services every task and the rest keep their connections.
    barrier = threading.Barrier(MAX_DB_CONCURRENCY, timeout=timeout)

    def _close():
        try:
            barrier.wait()
        except Exception:                                          # noqa: BLE001
            pass
        _base.close_worker_conn()

    try:
        futures = [pool.submit(_close) for _ in range(MAX_DB_CONCURRENCY)]
        for f in futures:
            try:
                f.result(timeout=timeout)
            except Exception:                                      # noqa: BLE001
                pass
    except RuntimeError:
        #  At interpreter exit `concurrent.futures.thread` runs its own atexit hook first and
        #  refuses new work ("cannot schedule new futures after shutdown"). Its hook has already
        #  joined the worker threads, so their connections are going away with the process
        #  anyway; there is nothing left to close but this thread's.
        pass
    finally:
        try:
            pool.shutdown(wait=False)
        except Exception:                                          # noqa: BLE001
            pass
    _base.close_worker_conn()


atexit.register(shutdown_pool)


#  Every channel `search()` knows how to build. An explicit preset naming anything else is a
#  typo, and a typo used to remove a channel silently: the builder simply had no branch for it.
KNOWN_CHANNELS = frozenset(
    set(channels.PHASE1) | set(channels.PHASE2) | set(channels.TIERS))


def resolve_preset(config, presets=None):
    """Resolve a preset name or an explicit list into a validated, de-duplicated channel order.

    Two failures this closes, both of which used to be silent:

    * an unknown CHANNEL name in an explicit list did nothing at all, so a typo quietly removed a
      channel from the search and the result looked merely worse rather than wrong. It now raises,
      before any task is submitted.
    * a repeated name issued the same query twice. The dictionary of results hid it, because the
      second write simply replaced the first, but the database still did the work. First
      occurrence wins and the order is preserved.

    An unknown preset NAME keeps its existing fallback: that is a different thing from a bad
    channel name and callers rely on it.
    """
    presets = PRESETS if presets is None else presets
    if isinstance(config, (list, tuple)):
        names = list(config)
        unknown = [c for c in names if c not in KNOWN_CHANNELS]
        if unknown:
            raise ValueError(
                f"unknown retrieval channel(s): {', '.join(map(str, unknown))}. "
                f"Known channels are: {', '.join(sorted(KNOWN_CHANNELS))}.")
    else:
        names = list(presets.get(config, ["bm25", "dense"]))
    return list(dict.fromkeys(names))


def _run_phase(tasks, bound, wide=None, remote_bound=None):
    """Run `tasks` concurrently, at most `bound` inside the database at once.

    `tasks` is an ordered sequence of `(name, callable)` or `(name, callable, lane)`. A task with
    no lane runs on the database lane, which is what every channel is. The return is a dict in
    TASK order, never completion order, which is what keeps the output deterministic: RRF consumes
    channels in iteration order, so letting a fast channel jump the queue would silently reorder
    ties.

    Every task is submitted BEFORE any result is collected. Submitting one and blocking on it
    before submitting the next is sequential execution wearing a thread pool, and the barrier
    tests in `test_retrieval_concurrency` exist to make that impossible to ship by accident. The
    lanes matter here for the same reason: the cold tier is submitted alongside the hot channels,
    with its own gate and its own pool, so a shard wake can never take a slot the dense channel
    needed.
    """
    if not tasks:
        return {}
    norm = [(t[0], t[1], (t[2] if len(t) > 2 else DB_LANE)) for t in tasks]
    gates = {DB_LANE: threading.Semaphore(bound),
             REMOTE_LANE: threading.Semaphore(int(remote_bound or REMOTE_CONCURRENCY))}
    pools = {DB_LANE: _pool, REMOTE_LANE: _remote_pool}
    #  Captured HERE, at task creation, from the search that is submitting. Reading it inside the
    #  task would read whatever another overlapping request had most recently set.
    captured = _base.active_wide() if wide is None else bool(wide)

    def guarded(fn, gate):
        def run():
            with gate, _base.profile_context(captured):
                return fn()
        return run

    futures = [(name, pools[lane]().submit(guarded(fn, gates[lane])))     # all submitted first
               for name, fn, lane in norm]

    #  DRAIN FIRST, decide afterwards. Raising on the first failed future leaves its siblings
    #  still executing against the database while the caller already has the exception, so the
    #  search "returns" with queries in flight, and on the next request they are competing with
    #  it for the same bounded connections. Every future is waited on before anything is raised.
    results, errors = {}, {}
    for name, fut in futures:                                           # collected in task order
        try:
            results[name] = fut.result()
        except Exception as exc:                                        # noqa: BLE001
            errors[name] = exc

    if errors:
        #  Deterministic: the FIRST failure in task order, never whichever thread lost the race,
        #  or the error a user sees depends on scheduling.
        for name, _fn, _lane in norm:
            exc = errors.get(name)
            if exc is None:
                continue
            #  Only names explicitly listed as optional may soft-degrade. A channel nobody
            #  classified must not become optional by omission: that is how a required signal
            #  quietly disappears from an answer that still looks complete.
            if not _is_optional(name):
                raise exc

    out = {}
    for name, _fn, _lane in norm:
        if name in results:
            out[name] = results[name]
            continue
        import failclosed
        #  Still raises in benchmark mode, so no measurement is ever taken from a degraded run.
        #  It happens here, after the drain, so the raise cannot strand a sibling either.
        failclosed.source_failed(
            f"channel:{name}",
            f"{type(errors[name]).__name__}: {str(errors[name])[:160]}")
        out[name] = []
    return out


@dataclass
class Result:
    ranked_pubs: list           # [(publication_id, fused_score, provenance dict)]
    family_ranked: list         # [(family_key, publication_id, fused_score, provenance)]
    channel_hits: dict          # channel -> [publication_id,...] in rank order
    query: str
    # --- optional, populated by the federation bridge / domain detector -------------------
    # publication_id -> federation.FederatedHit for hits that have NO local row. A local id is
    # a bigint; an external one is the string "fed:<PUBNUM>". Renderers must look here when a
    # publication_id is not an int.
    external: dict = field(default_factory=dict)
    federation: dict = None     # federation.FederatedResult.to_dict(), when federation ran
    domain: dict = None         # domain_detect.DomainVerdict.to_dict(), when it was computed
    # True when the query looks out-of-domain and the UI should OFFER the wider federated
    # search. Federation is never run implicitly, see federation.search_two_tier.
    federation_offered: bool = False
    # What the tiers that are not the hot corpus did: which domains were routed to, which woke,
    # which were queried, what failed and how long the wake took. A cold miss is invisible in the
    # ranking by design (it is simply art that is not there), so this is the only place a reader
    # can tell "no shard had anything" from "no shard answered".
    tiers: dict = field(default_factory=dict)

    def channel_hits_ranked(self) -> dict:
        """channel_hits as {name: [(pid, score)]} so a Result can be re-fused. Only the rank
        ORDER matters to RRF, so a synthetic descending score is sufficient."""
        return {k: [(p, float(len(v) - i)) for i, p in enumerate(v)]
                for k, v in (self.channel_hits or {}).items()}

    def is_external(self, pid) -> bool:
        return isinstance(pid, str) and pid.startswith("fed:")


class Retriever(DenseMixin, LexicalMixin, ExactMixin, CpcMixin, CitationMixin, QbeMixin,
                FamilyMixin, FusionMixin, RetrieverBase):
    """Every channel, the fusion over them, and the family collapse that follows."""

    # ---- top-level search ----------------------------------------------------------------
    def search(self, query, subject=None, mode=None, config="hybrid",
               cpc_hints=None, assignee_hints=None, phrases=None, alt_query_vecs=None,
               do_rerank=None, topk=1000, wide=False, db_concurrency=None):
        """`wide=True` runs the funnel at the seed profile (see SEED_CHUNK_FETCH). Reserved for
        whole-invention passes: it roughly doubles the pass and there are ~20 element passes."""
        mode = as_mode(mode)
        if not query or not query.strip():          # degenerate: an empty query has no signal
            return Result(ranked_pubs=[], family_ranked=[], channel_hits={}, query=query or "")
        #  The width THIS call decided on, captured once. `self._wide` is still maintained for
        #  sequential callers, `scan_profile` and `fork`, but it is no longer what the fan-out
        #  reads: another request sharing this Retriever can flip it at any moment.
        wide_profile = bool(wide)
        if wide_profile != getattr(self, "_wide", False):
            self.scan_profile(wide=wide_profile)
        qvec = embed.embed_query(query[:8000], 768)
        ch = {}
        presets = PRESETS
        # Callers may pass an explicit bounded channel sequence.  Resolve that before a mapping
        # lookup: ``dict.get(config, ...)`` still hashes ``config`` before evaluating its fallback,
        # so a list raises ``TypeError: unhashable type: 'list'`` even though it is otherwise a
        # valid explicit preset.
        preset = resolve_preset(config, presets)
        # Cross-lingual query translation is available (query_translations) and used by the agent,
        # but M5 diagnosis showed it does NOT help the DE gap and even hurts (the corpus is
        # English-dominant, so translating a German query to English promotes English distractors).
        # The DE fix that worked is embedding the enriched DE/EP/WO claims (see M5 report). Enable
        # per-request via alt_query_vecs / xlingual=True when a caller wants it.
        if getattr(self, "_force_xlingual", False) and "crosslingual" not in preset:
            preset = preset + ["crosslingual"]
        if ("crosslingual" in preset and not alt_query_vecs
                and config not in ("agentic", "claim_agentic")):
            alt_query_vecs = self.query_translations(query)

        bound = resolve_concurrency(db_concurrency)

        args = channels.ChannelArgs(
            query=query, qvec=qvec, subject=subject, mode=mode, cpc_hints=cpc_hints,
            assignee_hints=assignee_hints, phrases=phrases, alt_query_vecs=alt_query_vecs)
        hot_names = [n for n in preset if n not in channels.TIERS]
        p1_kinds = [n for n in hot_names if channels.phase_of(n) == 1]
        p2_kinds = [n for n in hot_names if channels.phase_of(n) == 2]

        # The cold tier mirrors THIS preset's channels onto the woken shards, and is built only
        # when a shard backend is registered: with none, no routing query is issued and no thread
        # is created, so a search behaves exactly as it did before the tier existed.
        tier = None
        if "cold" in preset and cold.available():
            tier = cold.ColdTier(self, args, wide=wide_profile)
        external = {}
        tiers = {}

        # ---- phase 1: everything whose inputs are ready at request start ------------------
        # Ordered by the preset, so the dict RRF consumes is ordered by the preset too. A channel
        # only appears when its input exists: `exact` needs phrases, `biblio` needs assignee
        # hints, `crosslingual` needs the alternate vectors, all of which are resolved above.
        p1 = [(name, channels.thunk(self, name, args))
              for name in p1_kinds if channels.has_input(name, args)]
        # The remote lane, submitted in the SAME phase and therefore genuinely in parallel with
        # the hot channels. Never after them: waiting for the local answer and then waking a shard
        # would add the whole 20 s wake to the search instead of overlapping it.
        if tier is not None:
            deadline = time.monotonic() + cold.TIER_BUDGET
            p1.append(("cold", lambda: tier.run(p1_kinds, deadline=deadline), REMOTE_LANE))
        if "global" in preset and global_search.available():
            g_deadline = time.monotonic() + global_search.GLOBAL_TIMEOUT
            p1.append(("global",
                       lambda: self._tier_global(args, wide_profile, g_deadline), REMOTE_LANE))
        raw = _run_phase(p1, bound, wide=wide_profile)

        cap = _base.SEED_PUB_CAP if wide_profile else _base.PUB_CAP
        raw_global, raw_cold = raw.pop("global", None), raw.pop("cold", None)
        ch.update(raw)                                  # hot channels first, in preset order
        self._absorb_global(ch, external, raw_global)
        self._absorb_cold(ch, raw_cold, cap)

        # ---- phase 2: the channels that consume the fused strong seeds --------------------
        # These are a genuine dependency, not an ordering preference: citation expands the graph
        # AROUND the strong hits and qbe re-queries FROM their best chunks, so both need phase 1
        # fused and cannot be started earlier without changing what they mean.
        # Collapsing inside each channel leaves a family able to arrive as publication A from the
        # dense channel and publication B from bm25: the same disclosure, two ids. RRF then splits
        # the family's votes between them and neither wins, which is worse than not collapsing at
        # all. Canonicalising rewrites every channel onto ONE member per family first, so the
        # seeds are one per family too and citation and qbe expand around the right member.
        ch = self.canonicalise(ch)
        base_fused = self.rrf({k: v for k, v in ch.items()})
        strong = [pid for pid, _, _ in base_fused[:40]]
        args2 = args.with_seeds(strong)
        p2 = [(name, channels.thunk(self, name, args2))
              for name in p2_kinds if channels.has_input(name, args2)]
        if tier is not None:
            # Re-routed WITH the candidates, which are 50% of the documented routing mix and do
            # not exist until the cheap tiers have answered. A domain only this evidence indicates
            # is woken now and gets the phase 1 channels run against it too (`catch_up`), because
            # it was not reachable when they ran.
            deadline2 = time.monotonic() + cold.TIER_BUDGET
            p2.append(("cold", lambda: tier.run(p2_kinds, catch_up=p1_kinds,
                                                candidate_pids=strong, seeds=strong,
                                                deadline=deadline2), REMOTE_LANE))
        raw2 = _run_phase(p2, bound, wide=wide_profile)
        raw2_cold = raw2.pop("cold", None)
        ch.update(raw2)
        self._absorb_cold(ch, raw2_cold, cap)
        if tier is not None:
            tiers["cold"] = tier.status

        # Re-order to the preset: `ch` is built in two passes, and channel order reaches the
        # output through `channel_hits` and through RRF's iteration. The tier channels follow the
        # hot ones, in the preset's own channel order, so the ordering is a property of the
        # preset and not of which shard answered first.
        order = [n for n in hot_names if n in ch]
        order += [cold.cold_name(n) for n in hot_names if cold.cold_name(n) in ch]
        if "global" in ch:
            order.append("global")
        ch = {n: ch[n] for n in order}

        # Phase 2 can introduce a different member of a family phase 1 already found, so the
        # canonicalisation is redone over the complete set before the fusion that produces the
        # answer. It is a relabelling: no hit is added, dropped or reordered within a channel.
        ch = self.canonicalise(ch)
        fused = self.rrf(ch)
        fam = self.dedup_family(fused)

        do_rerank = (config == "hybrid_rerank") if do_rerank is None else do_rerank
        if do_rerank and fam:
            fam = self.rerank_families(query, fam, top=min(RERANK_TOP, len(fam)),
                                       external=external or None)
        return Result(ranked_pubs=[(p, s, pr) for _, p, s, pr in fam][:topk],
                      family_ranked=fam[:topk], channel_hits={k: [p for p, _ in v] for k, v in ch.items()},
                      query=query, external=external, tiers=tiers)

    # ---- the tiers that are not the hot corpus -------------------------------------------
    def _tier_global(self, args, wide, deadline=None):
        """The global channel: search, register the external families, fetch display records.

        Runs on the remote lane and NEVER raises: `global_search`'s module wrappers swallow, and
        this adds the budget check that a queued task needs. A global outage costs the art this
        corpus does not hold, which is the whole point of the tier, and nothing else.
        """
        if deadline is not None and time.monotonic() >= deadline:
            return {"hits": [], "external": {}, "state": "budget spent before the tier started"}
        cap = _base.SEED_PUB_CAP if wide else _base.PUB_CAP
        hits = global_search.search(args.query, subject=args.subject, mode=args.mode,
                                    limit=cap, qvec=args.qvec)
        rows, seen = [], set()
        for item in hits or ():
            try:
                pid, score = item[0], float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if pid in seen:
                continue
            seen.add(pid)
            rows.append((pid, score))
        ext_ids = [p for p, _ in rows if isinstance(p, str) and p.startswith("fed:")]
        #  Registering the family key is what stops a global hit and a local hit of the same
        #  disclosure appearing as two rows. `family_keys` returning {} is allowed and means each
        #  external id is its own family: nothing is merged that should not have been.
        for pid, fk in (global_search.family_keys(ext_ids) or {}).items():
            if fk:
                self.register_external(pid, fk)
        return {"hits": self.collapse_pairs(rows, cap),
                "external": global_search.records(ext_ids) or {},
                "state": "ok"}

    def _absorb_global(self, ch, external, part):
        """Put the global tier's hits in under the channel name `global`, weight 0.90."""
        if not isinstance(part, dict):
            return
        rows = part.get("hits") or []
        if rows:
            ch["global"] = rows
        for pid, rec in (part.get("external") or {}).items():
            external[pid] = rec

    def _absorb_cold(self, ch, part, cap):
        """Merge one cold pass into `ch`, pooling with anything an earlier pass already found.

        Phase 2 can query a domain phase 1 never woke, so the same `cold:<kind>` name can arrive
        twice. Pooling and re-collapsing is right and replacing is not: the second pass is more
        shards, not a better answer from the same ones.
        """
        if not isinstance(part, dict):
            return
        for name, rows in part.items():
            if name in ch:
                pooled = {}
                for pid, sc in list(ch[name]) + list(rows):
                    if sc > pooled.get(pid, float("-inf")):
                        pooled[pid] = sc
                rows = self.collapse_pairs(
                    sorted(pooled.items(), key=lambda t: t[1], reverse=True), cap)
            ch[name] = list(rows)
