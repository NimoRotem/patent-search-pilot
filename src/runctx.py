"""The durable run bound to the search currently executing, keyed by slug.

WHY A MODULE-LEVEL REGISTRY AND NOT A PARAMETER
-----------------------------------------------
The pipeline is `webapp._generate` -> `agent.CoverageAgent.run` -> `retrieval` -> `deep_rank` ->
`deep_analysis`, four of them fanning work out across ThreadPoolExecutors. Threading a run handle
through every one of those signatures is a large change to code that is not otherwise moving, and
`contextvars` do not survive `ThreadPoolExecutor.submit`. A registry keyed by slug is visible from
every thread in the process, which is exactly the set of threads working on that slug.

EVERY FUNCTION HERE IS A NO-OP WHEN NO CONTEXT IS BOUND. That is deliberate: the gold-set runner,
the benchmark harness, `warm_reports` and the whole hermetic test suite call the same pipeline
without a worker, and they must behave exactly as they did before.
"""
from __future__ import annotations

import threading
import traceback

_LOCK = threading.RLock()
_BOUND: dict = {}


class DurabilityError(RuntimeError):
    """A required run checkpoint could not be persisted."""


class RunContext:
    """One durable run, as the pipeline sees it."""

    def __init__(self, run_id, slug, attempt=1, worker="", heartbeat=None):
        self.run_id, self.slug = run_id, slug
        self.attempt, self.worker = int(attempt), worker
        self.heartbeat = heartbeat           # runstore.Heartbeat, or None
        self._done = None                    # {stage: payload} lazily loaded once per attempt

    # -- checkpoints -------------------------------------------------------------------------
    def done_stages(self):
        import runstore
        if self._done is None:
            self._done = runstore.completed_stages(self.run_id)
        return self._done

    def stage_payload(self, stage):
        """The checkpoint an earlier attempt left for `stage`, or None."""
        return self.done_stages().get(stage)

    def checkpoint(self, stage, payload=None, n_out=None, n_in=None, detail=None):
        """Record `stage` complete with its checkpoint. Idempotent within an attempt."""
        import runstore
        self.check_lease()
        try:
            sid = runstore.stage_start(self.run_id, stage, self.attempt, n_in=n_in, detail=detail)
            runstore.stage_finish(sid, self.run_id, stage, status="done", n_out=n_out,
                                  payload=payload, detail=detail)
            if self._done is not None:
                self._done[stage] = payload
        except runstore.LeaseLost:
            raise
        except Exception as exc:
            raise DurabilityError(
                f"required checkpoint {stage!r} for run {self.run_id} was not persisted") from exc

    def reference_done(self, pub, ref):
        """One reference analysis, persisted the moment it lands.

        The CONTENT is already durable in `evidence_charts` (keyed by reference + checklist
        fingerprint), which is what makes a resumed run cheap. This row is the RUN's own ledger:
        which references this run has read, when, and at what cost.
        """
        import runstore
        try:
            runstore.substage(self.run_id, "read", str(pub), attempt=self.attempt,
                              payload={"found": bool((ref or {}).get("found")),
                                       "method": (ref or {}).get("method"),
                                       "chars": (ref or {}).get("chars"),
                                       "refuted": (ref or {}).get("refuted"),
                                       "cached": bool((ref or {}).get("cached"))},
                              detail={"seconds": (ref or {}).get("seconds")})
        except Exception:
            traceback.print_exc()

    def rescue_round(self, n, payload=None):
        import runstore
        try:
            runstore.substage(self.run_id, "rescue", str(n), attempt=self.attempt,
                              payload=payload or {})
        except Exception:
            traceback.print_exc()

    # -- the ledger --------------------------------------------------------------------------
    def note_query(self, channel, **kw):
        import runstore
        try:
            return runstore.record_query(self.run_id, channel, **kw)
        except Exception:
            traceback.print_exc()
            return None

    def note_hits(self, channel, hits, query_id=None, shard=""):
        import runstore
        try:
            return runstore.record_hits(self.run_id, channel, hits, query_id, shard)
        except Exception:
            traceback.print_exc()
            return 0

    def note_candidates(self, candidates):
        import runstore
        try:
            return runstore.upsert_candidates(self.run_id, candidates)
        except Exception:
            traceback.print_exc()
            return 0

    def note_usage(self, provider, **kw):
        import runstore
        runstore.record_usage(self.run_id, provider, **kw)

    # -- progress ----------------------------------------------------------------------------
    def event(self, payload):
        import runstore
        runstore.progress(self.run_id, payload)

    def check_lease(self):
        """Raise LeaseLost if the reaper handed this run to somebody else. Called at stage
        boundaries: continuing after that means two workers writing one report."""
        if self.heartbeat is not None:
            self.heartbeat.check()
            if self.worker:
                import runstore
                if not runstore.heartbeat(
                        self.run_id, self.worker,
                        getattr(self.heartbeat, "lease_seconds", None)):
                    lost = getattr(self.heartbeat, "lost", None)
                    if lost is not None:
                        lost.set()
                    raise runstore.LeaseLost(
                        f"{self.run_id} is no longer owned by worker {self.worker}")


# ---------------------------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------------------------
def bind(slug, ctx):
    with _LOCK:
        _BOUND[slug] = ctx
    return ctx


def unbind(slug):
    with _LOCK:
        return _BOUND.pop(slug, None)


def current(slug):
    """The RunContext for `slug`, or None when the search is not running under a worker."""
    if not slug:
        return None
    with _LOCK:
        return _BOUND.get(slug)


# -- no-op-safe module level helpers ----------------------------------------------------------
def event(slug, payload):
    ctx = current(slug)
    if ctx is not None:
        try:
            ctx.event(payload)
        except Exception:
            traceback.print_exc()


def checkpoint(slug, stage, payload=None, n_out=None, n_in=None, detail=None):
    ctx = current(slug)
    if ctx is not None:
        ctx.checkpoint(stage, payload=payload, n_out=n_out, n_in=n_in, detail=detail)


def stage_payload(slug, stage):
    ctx = current(slug)
    return None if ctx is None else ctx.stage_payload(stage)


def reference_done(slug, pub, ref):
    ctx = current(slug)
    if ctx is not None:
        ctx.reference_done(pub, ref)


def rescue_round(slug, n, payload=None):
    ctx = current(slug)
    if ctx is not None:
        ctx.rescue_round(n, payload)


def note_candidates(slug, candidates):
    ctx = current(slug)
    if ctx is not None:
        ctx.note_candidates(candidates)


def note_query(slug, channel, **kw):
    ctx = current(slug)
    return None if ctx is None else ctx.note_query(channel, **kw)


def note_hits(slug, channel, hits, query_id=None, shard=""):
    ctx = current(slug)
    return 0 if ctx is None else ctx.note_hits(channel, hits, query_id, shard)


def check_lease(slug):
    ctx = current(slug)
    if ctx is not None:
        ctx.check_lease()
