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
"""
from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import embed
import runctx                          # the durable run this search belongs to, if any (else no-op)

from . import base as _base
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
PRESETS = {
    "keyword": ["exact", "bm25"],
    "vector": ["dense"],
    "hybrid": ["exact", "bm25", "dense", "cpc"],
    "hybrid_rerank": ["exact", "bm25", "dense", "cpc"],
    "agentic": ["dense", "brief_dense", "cpc", "citation", "qbe", "biblio",
                "crosslingual"],
    #  Claim focus BOOSTS claim text, it does not delete the rest of the patent.
    #  MEASURED (RECALL_STUDY_2026-08-02.md): this preset used to omit `dense`, so
    #  description paragraphs were unsearchable. On the case that prompted the rebuild the
    #  best passage of the #1 result was a description paragraph, the best passage of the
    #  reference the searcher named was a description paragraph, and only 10 of the 25
    #  displayed cards matched on a claim at all.
    "claim_agentic": ["claim_dense", "dense", "brief_dense", "claim_bm25", "cpc",
                      "citation", "qbe", "biblio", "crosslingual"],
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

#  Losing one of these means the answer is wrong rather than thinner, so they keep today's
#  behaviour exactly: the exception propagates. Dense retrieval is the dominant signal in every
#  measurement of this corpus, and a search that quietly returned only its lexical channels would
#  look like a bad result rather than a broken one.
REQUIRED_CHANNELS = frozenset({"dense", "claim_dense", "brief_dense"})

#  Enrichment. Any of these may fail without invalidating the answer, so the failure is recorded
#  through `failclosed` (which still RAISES in benchmark mode, so no measurement is ever taken
#  from a degraded run) and the channel contributes nothing.
OPTIONAL_CHANNELS = frozenset({"bm25", "claim_bm25", "exact", "cpc", "citation", "qbe",
                               "biblio", "crosslingual"})

_POOL = None
_POOL_LOCK = threading.Lock()


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


def shutdown_pool(timeout=10.0):
    """Close every worker's connection, then drop the pool. Idempotent."""
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
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
KNOWN_CHANNELS = frozenset({
    "dense", "claim_dense", "brief_dense", "bm25", "claim_bm25", "exact", "cpc",
    "biblio", "crosslingual", "citation", "qbe",
})


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


def _pass_key(query, mode, subject, wide, preset, phrases, cpc_hints, assignee_hints, topk):
    """A short, stable identity for ONE retrieval pass. -> 16 hex characters.

    Two passes with the same fingerprint would return the same hits, so sharing a checkpoint
    between them is correct. Two with different ones are different questions, and this is what
    stops the second reading the first's answer.
    """
    import hashlib
    import json as _json
    payload = _json.dumps(
        {"q": str(query or "")[:4000], "mode": str(mode), "subject": str(subject or ""),
         "wide": bool(wide), "preset": list(preset or []),
         "phrases": sorted(str(p) for p in (phrases or [])),
         "cpc": sorted(str(c) for c in (cpc_hints or [])),
         "assignee": sorted(str(a) for a in (assignee_hints or [])),
         "topk": int(topk or 0)},
        sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _run_phase(tasks, bound, wide=None, pass_key=""):
    """Run `tasks` concurrently, at most `bound` inside the database at once.

    `tasks` is an ordered sequence of (name, callable). The return is a dict in THAT order, never
    completion order, which is what keeps the output deterministic: RRF consumes channels in
    iteration order, so letting a fast channel jump the queue would silently reorder ties.

    Every task is submitted BEFORE any result is collected. Submitting one and blocking on it
    before submitting the next is sequential execution wearing a thread pool, and the barrier
    tests in `test_retrieval_concurrency` exist to make that impossible to ship by accident.

    UNDER A DURABLE RUN, TWO MORE THINGS HAPPEN, and neither changes the answer.

    * A channel an earlier attempt COMPLETED is restored from `retrieval_hits` instead of being
      re-run. The restored list is the same publication ids in the same rank order, and fusion
      consumes nothing else, so a resumed run fuses to the identical ranking. A checkpoint whose
      digest does not match what the ledger holds is treated as absent and the channel runs.
    * Cancellation is checked BEFORE a task is submitted and again on the worker thread just
      before the channel body runs, so a run whose lease was reaped while it queued behind the
      concurrency gate stops there rather than issuing the query.

    `pass_key` IS WHAT MAKES THE FIRST OF THOSE SAFE, and without it there is no checkpointing at
    all. One run calls `Retriever.search` once for the whole invention and once per element,
    roughly twenty times, and every one of those passes runs a channel called `dense`. Keyed on
    the channel alone, pass seven restores pass one's hits and answers a question nobody asked.
    An empty `pass_key` means the caller cannot say which pass this is, and the honest response to
    that is no checkpoint rather than a wrong one.
    """
    if not tasks:
        return {}
    gate = threading.Semaphore(bound)
    #  Captured HERE, at task creation, from the search that is submitting. Reading it inside the
    #  task would read whatever another overlapping request had most recently set.
    captured = _base.active_wide() if wide is None else bool(wide)
    #  Captured here for the same reason, and re-applied inside the task: the pool's threads are
    #  process-wide and outlive a search, so a thread-local left behind would attribute the next
    #  run's work, and its cancellation, to the previous one.
    ctx = runctx.active()
    if ctx is not None:
        ctx.check_cancelled("retrieval fan-out")

    def guarded(name, fn):
        def run():
            with gate, _base.profile_context(captured), runctx.adopt(ctx):
                #  Inside the gate: a channel that waited here while the lease was reaped must not
                #  then go and query for it.
                runctx.check_cancelled(f"channel:{name}")
                return fn()
        return run

    #  RESUME FIRST, SUBMIT WHAT IS LEFT. Ordering is preserved because the results dict is
    #  assembled in task order below, not in the order things finished or were restored.
    results, errors, restored = {}, {}, []
    pending = []
    for name, fn in tasks:
        banked = ctx.channel_hits(name, pass_key) if ctx is not None else None
        if banked is not None:
            results[name] = banked
            restored.append(name)
        else:
            pending.append((name, fn))
    if restored:
        print(f"[resume] {len(restored)} retrieval channel(s) reloaded from the hit ledger "
              f"instead of re-run: {', '.join(restored)}", flush=True)

    pool = _pool()
    futures = [(name, pool.submit(guarded(name, fn))) for name, fn in pending]  # all submitted first

    #  DRAIN FIRST, decide afterwards. Raising on the first failed future leaves its siblings
    #  still executing against the database while the caller already has the exception, so the
    #  search "returns" with queries in flight, and on the next request they are competing with
    #  it for the same bounded connections. Every future is waited on before anything is raised.
    for name, fut in futures:                                           # collected in task order
        try:
            results[name] = fut.result()
        except Exception as exc:                                        # noqa: BLE001
            errors[name] = exc

    #  A CANCELLED RUN IS NOT A DEGRADED CHANNEL. Every failure below either raises or is recorded
    #  as a source outage; a lease that was reaped is neither, and must reach the worker as itself.
    for name, _fn in tasks:
        exc = errors.get(name)
        if isinstance(exc, runctx.RunCancelled):
            raise exc

    #  CHECKPOINT WHAT ACTUALLY RAN, before the phase returns. A channel is only recorded when it
    #  produced a result: a failed channel has no checkpoint, so the retry runs it again.
    if ctx is not None:
        for name, _fn in pending:
            if name in results:
                try:
                    ctx.channel_done(name, results[name], pass_key)
                except Exception:                                       # noqa: BLE001
                    #  A checkpoint that will not persist costs the retry its saving, never the
                    #  search its answer.
                    import traceback as _tb
                    _tb.print_exc()

    if errors:
        #  Deterministic: the FIRST failure in task order, never whichever thread lost the race,
        #  or the error a user sees depends on scheduling.
        for name, _fn in tasks:
            exc = errors.get(name)
            if exc is None:
                continue
            #  Only names explicitly listed as optional may soft-degrade. A channel nobody
            #  classified must not become optional by omission: that is how a required signal
            #  quietly disappears from an answer that still looks complete.
            if name not in OPTIONAL_CHANNELS:
                raise exc

    out = {}
    for name, _fn in tasks:
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
        #  BEFORE THE QUERY IS EMBEDDED. The agent issues ~20 of these passes; a run whose lease
        #  was reaped during pass three must not pay for passes four to twenty.
        runctx.check_cancelled("retrieval.search")
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
        #  WHICH PASS THIS IS. Every input that changes what the channels return goes in, because
        #  a checkpoint restored for the wrong pass is a wrong answer rather than a slow one. The
        #  fused seeds phase 2 consumes are derived from phase 1 of this same pass, so they are
        #  covered by the same fingerprint.
        pass_key = _pass_key(query, mode, subject, wide_profile, preset, phrases, cpc_hints,
                             assignee_hints, topk)

        # ---- phase 1: everything whose inputs are ready at request start ------------------
        # Ordered by the preset, so the dict RRF consumes is ordered by the preset too. A channel
        # only appears when its input exists: `exact` needs phrases, `biblio` needs assignee
        # hints, `crosslingual` needs the alternate vectors, all of which are resolved above.
        p1 = []
        for name in preset:
            if name == "dense":
                p1.append((name, lambda: self.channel_dense(qvec, subject, mode)))
            elif name == "claim_dense":
                p1.append((name, lambda: self.channel_claim_dense(qvec, subject, mode)))
            elif name == "brief_dense":
                p1.append((name, lambda: self.channel_brief_dense(qvec, subject, mode)))
            elif name == "bm25":
                p1.append((name, lambda: self.channel_bm25(query, subject, mode)))
            elif name == "claim_bm25":
                p1.append((name, lambda: self.channel_claim_bm25(query, subject, mode)))
            elif name == "exact" and phrases:
                p1.append((name, lambda: self.channel_exact(phrases, subject, mode)))
            elif name == "cpc":
                p1.append((name, lambda: self.channel_cpc(cpc_hints, subject, mode)))
            elif name == "biblio" and assignee_hints:
                p1.append((name, lambda: self.channel_biblio(assignee_hints, subject, mode)))
            elif name == "crosslingual" and alt_query_vecs:
                p1.append((name, lambda: self.channel_crosslingual(alt_query_vecs, subject, mode)))
        ch.update(_run_phase(p1, bound, wide=wide_profile, pass_key=pass_key))

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
        p2 = []
        for name in preset:
            if name == "citation":
                p2.append((name, lambda: self.channel_citation_family(strong, subject, mode)))
            elif name == "qbe":
                p2.append((name, lambda: self.channel_qbe(strong, subject, mode)))
        ch.update(_run_phase(p2, bound, wide=wide_profile, pass_key=pass_key))

        # Re-order to the preset: `ch` is built in two passes, and channel order reaches the
        # output through `channel_hits` and through RRF's iteration.
        ch = {n: ch[n] for n in preset if n in ch}

        # Phase 2 can introduce a different member of a family phase 1 already found, so the
        # canonicalisation is redone over the complete set before the fusion that produces the
        # answer. It is a relabelling: no hit is added, dropped or reordered within a channel.
        ch = self.canonicalise(ch)
        fused = self.rrf(ch)
        fam = self.dedup_family(fused)

        do_rerank = (config == "hybrid_rerank") if do_rerank is None else do_rerank
        #  The cross-encoder head is the single most expensive call in this function (56.4 s
        #  measured) and it is the last thing before the answer. Nothing about it is worth paying
        #  for on a run somebody else now owns.
        runctx.check_cancelled("retrieval.rerank")
        if do_rerank and fam:
            fam = self.rerank_families(query, fam, top=min(RERANK_TOP, len(fam)))
        return Result(ranked_pubs=[(p, s, pr) for _, p, s, pr in fam][:topk],
                      family_ranked=fam[:topk], channel_hits={k: [p for p, _ in v] for k, v in ch.items()},
                      query=query)
