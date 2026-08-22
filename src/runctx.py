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

CANCELLATION
------------
A run whose lease was reaped is already being executed by somebody else. Until the token below
existed the heartbeat thread noticed that and nothing downstream heard it: the reading stage kept
issuing provider calls for however long it had left, and only at the very end did the worker
discover it could not settle the run. Every call site that spends money now checks
`check_cancelled()` between units of work, not merely between stages, and raises `RunCancelled`,
which the worker recognises as "stop, do not settle, somebody else owns this".

The token is set from the heartbeat THREAD, so the check is a plain `Event.is_set()` on the
working threads and costs nothing. Cancellation is cooperative: it is raised where the next unit of
work would have started, never in the middle of writing an artifact, so a cancelled run leaves
either a complete checkpoint or none.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

import runstore

_LOCK = threading.RLock()
_BOUND: dict = {}
#  The run THIS thread is working for. Set by the worker on its own thread and re-applied by
#  `adopt` inside a pool task, because the retrieval pool's threads outlive a single run and a
#  thread-local left behind would attribute the next run's work to the previous one.
_LOCAL = threading.local()


class DurabilityError(RuntimeError):
    """A required run checkpoint could not be persisted."""


def _pid(text):
    """A publication id as the retriever uses it: a local bigint, or an external string key."""
    s = str(text)
    try:
        return int(s)
    except ValueError:
        return s


class RunCancelled(runstore.LeaseLost):
    """This run must stop spending NOW.

    The base class is deliberate: `runstore.LeaseLost` is already what the worker treats as "drop
    this run, do not settle it, somebody else owns it", and a run cancelled because its lease was
    reaped needs exactly that handling. Being its own type is what lets a call site say WHY it
    stopped, and lets a test assert the pipeline stopped cooperatively rather than crashed.
    """

    def __init__(self, message, reason="cancelled"):
        super().__init__(message)
        self.reason = reason


class RunContext:
    """One durable run, as the pipeline sees it."""

    def __init__(self, run_id, slug, attempt=1, worker="", heartbeat=None, artifact_root=None):
        self.run_id, self.slug = run_id, slug
        self.attempt, self.worker = int(attempt), worker
        self.heartbeat = heartbeat           # runstore.Heartbeat, or None
        self.artifact_root = Path(artifact_root) if artifact_root else None
        self._done = None                    # {stage: payload} lazily loaded once per attempt
        self._sub = {}                       # parent -> {key: payload}, one query per parent
        self._cancelled = threading.Event()
        self._cancel_reason = ""
        #  THE HEARTBEAT IS THE PRODUCER OF THE SIGNAL. It already detects a lost lease; this is
        #  what makes the detection reach the threads that are spending.
        if heartbeat is not None and hasattr(heartbeat, "on_lost"):
            heartbeat.on_lost(lambda: self.cancel("lease lost"))

    # -- cancellation ------------------------------------------------------------------------
    def cancel(self, reason="cancelled"):
        """Ask every spending call site to stop at its next unit of work. Idempotent, thread safe."""
        if not self._cancelled.is_set():
            self._cancel_reason = str(reason)
        self._cancelled.set()

    @property
    def cancelled(self):
        """Whether this run has been told to stop.

        Also consults the heartbeat directly, so a context built before the heartbeat learned to
        publish its loss still stops. Cheap: two `Event.is_set()` calls.
        """
        if self._cancelled.is_set():
            return True
        hb = self.heartbeat
        lost = getattr(hb, "lost", None) if hb is not None else None
        if lost is not None and lost.is_set():
            self.cancel("lease lost")
            return True
        return False

    def check_cancelled(self, where=""):
        """Raise RunCancelled if this run has been told to stop. Called BETWEEN units of work."""
        if self.cancelled:
            raise RunCancelled(
                f"run {self.run_id} stopped"
                + (f" at {where}" if where else "")
                + f": {self._cancel_reason or 'cancelled'}",
                reason=self._cancel_reason or "cancelled")

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

    # -- substage checkpoints (one channel, one reference, one rescue round) -------------------
    def substages(self, parent, refresh=False):
        """{key: payload} for every completed substage of `parent`. ONE query per parent per
        attempt: a 700-reference read stage must not cost 700 round trips to find out what it
        already has."""
        import runstore
        if refresh or parent not in self._sub:
            try:
                self._sub[parent] = runstore.substages_done(self.run_id, parent)
            except Exception:                                        # noqa: BLE001
                traceback.print_exc()
                self._sub[parent] = {}
        return self._sub[parent]

    def substage_payload(self, parent, key):
        return self.substages(parent).get(str(key))

    def substage_done(self, parent, key, payload=None, n_out=None, detail=None):
        import runstore
        runstore.substage(self.run_id, parent, str(key), attempt=self.attempt, payload=payload,
                          n_out=n_out, detail=detail)
        self.substages(parent)[str(key)] = payload

    # -- artifacts ---------------------------------------------------------------------------
    def artifact_dir(self):
        """Where this RUN's checkpoint artifacts live, or None when nobody told us a root.

        Per run, not per slug: two attempts of one slug share a run id and therefore share their
        checkpoints, but two different runs of the same slug must never read each other's.
        """
        if self.artifact_root is None:
            return None
        d = Path(self.artifact_root) / "runs" / str(self.run_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def artifact_path(self, kind, key):
        d = self.artifact_dir()
        if d is None:
            return None
        import hashlib
        safe = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:24]
        return d / f"{kind}.{safe}.json"

    def save_artifact(self, kind, key, payload):
        """Write one checkpoint artifact atomically. -> (path, sha256) or (None, None)."""
        import runartifact
        p = self.artifact_path(kind, key)
        if p is None:
            return None, None
        return str(p), runartifact.write(p, payload)

    def load_artifact(self, ref):
        """The artifact a checkpoint points at, ONLY if its digest still matches. Else None.

        A truncated or partially written artifact is the failure this exists to catch, so a
        mismatch is treated as "there is no checkpoint", never as "close enough".
        """
        import runartifact
        if not isinstance(ref, dict) or not ref.get("path"):
            return None
        return runartifact.read(ref["path"], ref.get("sha256"))

    # -- per-channel retrieval checkpoints -------------------------------------------------------
    #  A channel is a bounded, independently repeatable unit of retrieval, and it is where the
    #  money is: the local channel alone measured 958 s. A worker killed after six of nine channels
    #  used to re-run all nine. `retrieval_hits` is the landing zone the schema already reserved
    #  for the raw, pre-fusion hit set, so the checkpoint IS the evidence.
    @staticmethod
    def _hit_digest(rows):
        import runartifact
        return runartifact.digest_bytes(runartifact.serialise(rows))

    def channel_done(self, channel, hits):
        """Record `channel` complete with the exact hit list fusion will consume. -> its digest.

        `hits` is the channel's own [(publication_id, score)] in rank order. The publication id is
        a local bigint or the string form of an external one, so it is stored as text and restored
        to whichever it was.
        """
        import runstore
        rows = [{"publication_number": str(pid), "rank": i + 1,
                 "score": (None if score is None else float(score))}
                for i, (pid, score) in enumerate(hits or [])]
        digest = self._hit_digest(rows)
        runstore.replace_hits(self.run_id, channel, rows)
        self.substage_done("retrieve", channel,
                           {"n": len(rows), "digest": digest}, n_out=len(rows))
        return digest

    def channel_hits(self, channel):
        """The hit list an earlier attempt banked for `channel`, or None to run it again.

        None on ANY disagreement: no checkpoint row, no rows in the ledger when the checkpoint says
        there were some, or a digest that does not match what came back. A half-written channel
        reloaded as though it were whole is a silently short retrieval, which looks like a bad
        result rather than a broken one.
        """
        import runstore
        row = self.substage_payload("retrieve", channel)
        if not isinstance(row, dict) or "digest" not in row:
            return None
        try:
            stored = runstore.channel_hits(self.run_id, channel)
        except Exception:                                            # noqa: BLE001
            traceback.print_exc()
            return None
        rows = [{"publication_number": str(h.get("publication_number")),
                 "rank": h.get("rank"),
                 "score": (None if h.get("score") is None else float(h.get("score")))}
                for h in stored]
        if len(rows) != int(row.get("n") or 0) or self._hit_digest(rows) != row["digest"]:
            print(f"[resume {self.run_id}] the {channel} channel checkpoint does not match what "
                  f"the hit ledger holds; the channel is run again", flush=True)
            return None
        return [(_pid(h["publication_number"]), h["score"]) for h in rows]

    # -- per-run side effects ------------------------------------------------------------------
    def once(self, kind, detail=None):
        """True for the ONE attempt that owns this side effect, False for every other.

        Keyed on run_id in Postgres with a uniqueness constraint, never on a flag in memory: the
        attempt that sends the mail and the attempt that does not are usually different processes
        on different days.
        """
        import runstore
        try:
            return runstore.claim_side_effect(self.run_id, kind, attempt=self.attempt,
                                              detail=detail)
        except Exception as exc:
            #  A side effect whose ledger is unreachable must NOT fire: performing it would risk
            #  doing it twice, and that is the failure this call exists to prevent.
            raise DurabilityError(
                f"could not claim the once-per-run side effect {kind!r} for run "
                f"{self.run_id}") from exc

    # -- the ledger --------------------------------------------------------------------------
    def reference_done(self, pub, ref, fp=""):
        """One reference analysis, persisted the moment it lands.

        The CONTENT is already durable in `evidence_charts` (keyed by reference + checklist
        fingerprint), which is what makes a resumed run cheap. This row is the RUN's own ledger:
        which references this run has read, when, and at what cost. When an artifact root is
        configured the chart itself is written beside it with a digest, so a resumed run reloads
        the reading instead of paying for it again even when the evidence store is off, cold or
        answering about a different checklist.

        `fp` fingerprints the checklist this chart answers. A resumed run that built a different
        one (a different budget, a different disclosure list) must not reuse it: the chart would be
        an answer to a question nobody asked.
        """
        import runstore
        try:
            payload = {"found": bool((ref or {}).get("found")),
                       "method": (ref or {}).get("method"),
                       "chars": (ref or {}).get("chars"),
                       "refuted": (ref or {}).get("refuted"),
                       "cached": bool((ref or {}).get("cached")),
                       "fp": str(fp or "")}
            #  Only a real reading is worth reloading. An "error" or "no-text" result is a
            #  statement about one attempt's luck with a source, not a durable fact, and
            #  resuming onto it would make a transient failure permanent.
            if ref and ref.get("method") == "llm":
                path, sha = self.save_artifact("read", pub, ref)
                if path:
                    payload["artifact"] = {"path": path, "sha256": sha}
            runstore.substage(self.run_id, "read", str(pub), attempt=self.attempt,
                              payload=payload, detail={"seconds": (ref or {}).get("seconds")})
            self.substages("read")[str(pub)] = payload
        except Exception:
            traceback.print_exc()

    def reference_payload(self, pub, fp=""):
        """The chart an earlier attempt banked for `pub`, digest-checked, or None to read again."""
        row = self.substage_payload("read", pub)
        if not isinstance(row, dict):
            return None
        if str(fp or "") != str(row.get("fp") or ""):
            return None
        return self.load_artifact(row.get("artifact"))

    def rescue_round(self, n, payload=None):
        import runstore
        try:
            runstore.substage(self.run_id, "rescue", str(n), attempt=self.attempt,
                              payload=payload or {})
            self.substages("rescue")[str(n)] = payload or {}
        except Exception:
            traceback.print_exc()

    # -- resumable rounds (the rescue's phases, and anything else shaped like them) --------------
    def round_payload(self, parent, key, fp=""):
        """What a COMPLETED round produced, or None when it has to run again.

        None on: no row, a fingerprint that says the round was run against a different question,
        or an artifact whose digest no longer matches. A round reloaded from a fragment is worse
        than a round re-run, because the run then finishes on it.
        """
        row = self.substage_payload(parent, key)
        if not isinstance(row, dict) or "state" not in row:
            return None
        if str(fp or "") != str(row.get("fp") or ""):
            return None
        if row["state"] == "inline":
            return row.get("payload")
        got = self.load_artifact(row.get("artifact"))
        if got is None:
            print(f"[resume {self.run_id}] the {parent}:{key} checkpoint does not match its "
                  f"artifact; the round is run again", flush=True)
        return got

    def round_done(self, parent, key, payload, fp="", n_out=None):
        """Record one round complete, with a content digest when it went to an artifact."""
        row = {"fp": str(fp or ""), "state": "inline", "payload": payload}
        path, sha = self.save_artifact(parent, f"{key}", payload)
        if path:
            row = {"fp": str(fp or ""), "state": "artifact",
                   "artifact": {"path": path, "sha256": sha}}
        self.substage_done(parent, key, row, n_out=n_out)
        return row

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
                    self.cancel("lease lost")
                    raise runstore.LeaseLost(
                        f"{self.run_id} is no longer owned by worker {self.worker}")


# ---------------------------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------------------------
def bind(slug, ctx):
    with _LOCK:
        _BOUND[slug] = ctx
    activate(ctx)
    return ctx


def unbind(slug):
    with _LOCK:
        ctx = _BOUND.pop(slug, None)
    if getattr(_LOCAL, "ctx", None) is ctx:
        _LOCAL.ctx = None
    return ctx


def current(slug):
    """The RunContext for `slug`, or None when the search is not running under a worker."""
    if not slug:
        return None
    with _LOCK:
        return _BOUND.get(slug)


def activate(ctx):
    """Make `ctx` the ambient run for THIS thread. -> the previous one."""
    prev = getattr(_LOCAL, "ctx", None)
    _LOCAL.ctx = ctx
    return prev


def active():
    """The run this thread is working for, without being told a slug.

    Retrieval, reading and rescue fan out over pools whose signatures carry no slug, and the
    cancellation check has to be reachable from inside them. A pool task captures the ambient run
    at SUBMIT time and re-applies it with `adopt`; the single-bound fallback covers the threads
    nobody had to teach, which is the durable worker's shape: one run per process at a time.
    """
    ctx = getattr(_LOCAL, "ctx", None)
    if ctx is not None:
        return ctx
    with _LOCK:
        if len(_BOUND) == 1:
            return next(iter(_BOUND.values()))
    return None


class adopt:
    """Bind `ctx` as this thread's ambient run for the duration of a block, then restore.

    Restored on the way out of an exception too. The retrieval pool's threads live for the life of
    the process, so "whatever runs next on this thread" is a real search, not a hypothesis.
    """

    __slots__ = ("ctx", "prev")

    def __init__(self, ctx):
        self.ctx, self.prev = ctx, None

    def __enter__(self):
        self.prev = activate(self.ctx)
        return self.ctx

    def __exit__(self, *exc):
        _LOCAL.ctx = self.prev
        return False


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


def reference_done(slug, pub, ref, fp=""):
    ctx = current(slug)
    if ctx is not None:
        ctx.reference_done(pub, ref, fp=fp)


def reference_payload(slug, pub, fp=""):
    ctx = current(slug)
    return None if ctx is None else ctx.reference_payload(pub, fp=fp)


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


def once(slug, kind, detail=None):
    """True when this attempt owns a once-per-run side effect.

    True with NO durable run bound, which is what keeps the gold set, the benchmark and the test
    suite behaving exactly as they did: without a run there are no attempts to be exactly-once
    across.
    """
    ctx = current(slug)
    return True if ctx is None else ctx.once(kind, detail=detail)


# -- ambient (slug-free) helpers, for the fan-outs ---------------------------------------------
def cancelled():
    """Whether the run THIS thread is working for has been told to stop. False with no run."""
    ctx = active()
    return bool(ctx is not None and ctx.cancelled)


def check_cancelled(where=""):
    """Raise RunCancelled if the run this thread is working for has been told to stop.

    The one function every expensive fan-out calls between units of work. A no-op with no durable
    run bound, so the gold set and the benchmark are unaffected.
    """
    ctx = active()
    if ctx is not None:
        ctx.check_cancelled(where)


def round_payload(parent, key, fp=""):
    """A completed round's output, for the fan-outs that carry no slug. None with no run."""
    ctx = active()
    return None if ctx is None else ctx.round_payload(parent, key, fp=fp)


def round_done(parent, key, payload, fp="", n_out=None):
    ctx = active()
    if ctx is not None:
        ctx.round_done(parent, key, payload, fp=fp, n_out=n_out)


def cancel(slug=None, reason="cancelled"):
    ctx = current(slug) if slug else active()
    if ctx is not None:
        ctx.cancel(reason)
