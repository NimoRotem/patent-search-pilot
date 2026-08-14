"""Out-of-process cross-encoder reranker (removes the global generation lock).

WHY THIS EXISTS
---------------
`rerank.py` holds a module-level FlagReranker (bge-reranker-v2-m3). Its Rust tokenizer is NOT
thread-safe: two threads scoring at once raise "Already borrowed". The webapp worked around that
with a module-level `_GEN_LOCK` that serialized *entire* report generations — a ~3 minute critical
section to protect a ~3 second one, capping the whole app at ONE concurrent search.

Measured on instance-3: the reranker costs ~670 MB RSS once loaded and peaks ~1.86 GB while
scoring. So "just fork more web workers" is not affordable on a 16 GB box that already has 5 GB of
swap in use — each worker would carry its own copy.

The fix: keep exactly ONE reranker, in ONE dedicated child process, and let every web thread submit
to it. Each submission crosses a process boundary, so the tokenizer is only ever touched by a
single interpreter — no "Already borrowed", no shared-memory hazard, and one copy of the weights no
matter how many web threads exist. Report generations now overlap fully and only contend for the
few seconds they actually spend reranking.

Everything degrades to identity order on any failure, preserving the existing contract that
reranking is best-effort and must never crash a report.
"""
from __future__ import annotations
import os, threading, multiprocessing
from concurrent.futures import ProcessPoolExecutor, TimeoutError as _FutureTimeout
from concurrent.futures.process import BrokenProcessPool


def _flag(name, default):
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "")


# Generous: this is queue-wait + scoring for up to ~25 passages, and two concurrent reports may
# stack up behind the single child.
RERANK_TIMEOUT = float(os.environ.get("RERANK_TIMEOUT", "240"))
# Poor-man's maxtasksperchild (ProcessPoolExecutor gained that kwarg in 3.11; this box is 3.9):
# recycle the child periodically so any torch/tokenizer arena growth is reclaimed.
RERANK_MAX_TASKS = int(os.environ.get("RERANK_MAX_TASKS", "50"))
# The child gets its own thread budget. 2 leaves cores for the web worker + Postgres on a 4-vCPU box.
RERANK_THREADS = int(os.environ.get("RERANK_THREADS", "2"))
POOL_ENABLED = _flag("RERANK_POOL", "1")

_pool = None
_tasks = 0          # tasks handed to the current child
_inflight = 0       # submissions not yet resolved (never recycle while >0)
_lock = threading.Lock()


# ---- child-side ----------------------------------------------------------------------------
def _child_init():
    """Runs once per child. Loads the model eagerly so the first real request isn't cold."""
    try:
        import torch
        torch.set_num_threads(RERANK_THREADS)
    except Exception:
        pass
    try:
        import rerank
        rerank._load()
    except Exception:
        pass


def _child_rerank(query, passages, top_k):
    """Child entrypoint. Calls the ORIGINAL in-process implementation — the child never imports
    webapp, so `rerank.rerank` here is unpatched and this cannot recurse."""
    import rerank
    return rerank.rerank(query, passages, top_k=top_k)


# ---- parent-side ---------------------------------------------------------------------------
def in_spawned_child() -> bool:
    """True when this interpreter IS a multiprocessing child.

    WHY THIS EXISTS. "spawn" re-imports the parent's __main__ module in the child. A script whose
    work sits at module level, with no `if __name__ == "__main__":` guard, therefore RE-RUNS ITS
    ENTIRE JOB inside every child it spawns, and that job spawns again. Measured: eval scripts
    lacking the guard produced a four-deep tree of interpreters each holding a reranker, about
    1.3 GB apiece, which exhausted 16 GB of RAM and 16 GB of swap and froze the host. The reports
    were being written concurrently by several recursive copies of the same run, so the numbers
    would have been meaningless even if it had survived.
    The guard belongs in every such script, and it was missing from one. Relying on every future
    script remembering it is the same bet that just lost, so refuse here as well: a child never
    needs a rerank pool, it IS one.
    """
    try:
        return multiprocessing.parent_process() is not None
    except Exception:
        return False


def _get_pool_locked():
    global _pool, _tasks
    if in_spawned_child():
        raise RuntimeError(
            "refusing to create a rerank pool inside a spawned child: the parent script is "
            "missing an `if __name__ == \"__main__\":` guard, so this child re-ran the whole "
            "job. Guard the script's module-level work.")
    if _pool is None:
        # "spawn", never "fork": forking a process that already holds torch, psycopg and a genai
        # client is a classic deadlock source (locks copied in a held state).
        ctx = multiprocessing.get_context("spawn")
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=ctx, initializer=_child_init)
        _tasks = 0
    return _pool


def _drop_pool():
    """Tear the child down. Called after a crash/timeout, or for periodic recycling."""
    global _pool, _tasks
    with _lock:
        p, _pool, _tasks = _pool, None, 0
    if p is not None:
        try:
            p.shutdown(wait=False)
        except Exception:
            pass


def shutdown():
    _drop_pool()


def rerank(query, passages, top_k=None):
    """Drop-in replacement for `rerank.rerank`, executed in the dedicated child process.

    Returns a list of (index, score) sorted desc, exactly like the in-process version. ANY failure
    (child crash, timeout, model missing, bad shape) falls back to identity order so a report is
    never lost to a reranking problem."""
    if not passages:
        return []
    identity = [(i, 0.0) for i in range(len(passages))]

    if not POOL_ENABLED:                        # escape hatch: run in-process (tests/debug)
        import rerank as _r
        fn = getattr(_r, "_inprocess_rerank", _r.rerank)
        return fn(query, passages, top_k=top_k)

    global _tasks, _inflight
    # Acquire the child FIRST, in its own guard. If even spawning fails we must not fall through
    # to the accounting block below (that would decrement _inflight for a task never counted).
    try:
        with _lock:
            pool = _get_pool_locked()
            _tasks += 1
            _inflight += 1
    except Exception as e:                      # noqa
        print(f"[rerank_pool] could not start child ({type(e).__name__}: {str(e)[:80]}); identity order")
        return identity[:top_k] if top_k else identity

    recycle = False
    try:
        # NOTE: the lock is deliberately NOT held across .result(). The executor has a single
        # worker, so it already serializes execution; holding a Python lock here as well would
        # block unrelated web threads for no benefit.
        fut = pool.submit(_child_rerank, query, list(passages), top_k)
        #  RERANK_TOP is now really 50 (it was silently 25), and this box measures ~2.4-3.1 s per
        #  passage, so a flat 240 s timed out and fell back to identity order. Scale the budget
        #  with the work, plus a fixed allowance for the first call's model load.
        budget = max(RERANK_TIMEOUT, 60.0 + 6.0 * len(passages))
        out = fut.result(timeout=budget)
        if not isinstance(out, list) or len(out) > len(passages):
            return identity[:top_k] if top_k else identity
        return out
    except (BrokenProcessPool, _FutureTimeout) as e:
        print(f"[rerank_pool] child unusable ({type(e).__name__}); identity order, respawning")
        recycle = True
        return identity[:top_k] if top_k else identity
    except Exception as e:                      # noqa — reranking is always non-fatal
        print(f"[rerank_pool] failed ({type(e).__name__}: {str(e)[:80]}); identity order")
        return identity[:top_k] if top_k else identity
    finally:
        with _lock:
            _inflight -= 1
            # Only recycle when nothing else is waiting on this child, or we'd break their futures.
            if not recycle and _tasks >= RERANK_MAX_TASKS and _inflight == 0:
                recycle = True
        if recycle:
            _drop_pool()


def install():
    """Route `rerank.rerank` (and therefore retrieval.rerank_families / the agent) through the
    dedicated child. Idempotent. Keeps the original callable as `_inprocess_rerank`."""
    import rerank as _r
    if getattr(_r, "_pool_installed", False):
        return False
    _r._inprocess_rerank = _r.rerank
    _r.rerank = rerank
    _r._pool_installed = True
    return True
