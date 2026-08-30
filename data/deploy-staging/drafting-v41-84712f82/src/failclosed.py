"""Benchmark runs fail. Production runs degrade, but never silently.

WHY
---
Every measurement in this project that turned out to be wrong was wrong the same way: something
failed, a fallback produced a plausible-looking value, and the run reported a number instead of an
error. The list is not short.

    a grounding probe using exact string matching       reported a working gate as broken
    a retrieval probe with iterative_scan left off      reported 780 chunks where there are 9,000
    tournament comparisons truncating their JSON        reported a 40% ranking regression
    a fallback that returned its INPUT order            scrambled the ranking it was asked to fix
    an LLM returning {} on error                        every caller read it as "nothing to say"

A bug that reports a plausible number instead of an error is the only kind that can waste a whole
experiment, because nothing downstream can tell it happened.

THE RULE
--------
In BENCHMARK mode a degraded path raises. The run dies, the manifest records why, and no number is
produced. In PRODUCTION the fallback runs, because a user waiting on a search is better served by
a degraded answer than by an error page, but every fallback taken is recorded on the run and
printed, so "this report was produced with the reranker down" is answerable afterwards.

    with failclosed.stage("screen"):
        ...

    value = failclosed.fallback("llm.chat_json", "vertex 503", {})   # raises in benchmark mode
"""
from __future__ import annotations

import os
import threading
import traceback

FLAG = "BENCHMARK_FAIL_CLOSED"


class DegradedRun(RuntimeError):
    """A stage degraded during a run that is not allowed to degrade."""


_state = threading.local()
_lock = threading.Lock()
_used = []              # process-wide record of fallbacks taken, for the manifest


def benchmark_mode() -> bool:
    """True when this run must not produce a number if anything degraded."""
    if getattr(_state, "forced", None) is not None:
        return bool(_state.forced)
    return os.environ.get(FLAG, "") not in ("", "0", "false", "False")


class force:
    """Context manager to turn benchmark mode on or off for a block (used by the harness)."""

    def __init__(self, on: bool):
        self.on = bool(on)
        self.prev = None

    def __enter__(self):
        self.prev = getattr(_state, "forced", None)
        _state.forced = self.on
        return self

    def __exit__(self, *exc):
        _state.forced = self.prev
        return False


def fallback(where: str, reason: str, value=None, kind: str = "fallback"):
    """Take a degraded path. Raises in benchmark mode; records and returns `value` otherwise.

    `where` is the call site, `reason` is what actually went wrong. Both end up in the run record,
    so a degraded report can be recognised as degraded after the fact instead of being read as a
    result.
    """
    rec = {"where": where, "reason": str(reason)[:300], "kind": kind}
    with _lock:
        _used.append(rec)
    msg = f"[degraded] {kind} at {where}: {reason}"
    if benchmark_mode():
        print(msg + "  -- benchmark mode, failing the run", flush=True)
        raise DegradedRun(f"{where}: {reason}")
    print(msg, flush=True)
    return value


def source_failed(name: str, error) -> None:
    """A source could not be reached or refused us. Distinct from a source returning nothing."""
    fallback(f"source:{name}", error, None, kind="source_failure")


def empty_result(name: str) -> None:
    """A source answered and had nothing. NOT a failure, and recorded separately so the two can
    never be confused: 'zero hits' and 'the adapter 401d' look identical in a result count."""
    with _lock:
        _used.append({"where": f"source:{name}", "reason": "returned zero results",
                      "kind": "empty_result"})


def used():
    with _lock:
        return list(_used)


def reset():
    with _lock:
        _used.clear()


def summary():
    """{kind: count} plus the distinct call sites, for the manifest and the report."""
    out, sites = {}, {}
    for r in used():
        out[r["kind"]] = out.get(r["kind"], 0) + 1
        sites.setdefault(r["kind"], set()).add(r["where"])
    return {"counts": out, "sites": {k: sorted(v) for k, v in sites.items()}}


class stage:
    """Wrap a pipeline stage so an exception inside it fails the run in benchmark mode.

    Production keeps the existing behaviour: the stage is skipped, the run continues, and the
    degradation is recorded rather than swallowed.
    """

    def __init__(self, name: str, reraise: bool = False):
        self.name = name
        self.reraise = reraise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        if isinstance(exc, DegradedRun):
            return False                      # already a benchmark failure; let it through
        if benchmark_mode() or self.reraise:
            print(f"[degraded] stage {self.name} raised {type(exc).__name__}: "
                  f"{str(exc)[:200]} -- benchmark mode, failing the run", flush=True)
            return False
        traceback.print_exc()
        fallback(f"stage:{self.name}", f"{type(exc).__name__}: {str(exc)[:200]}",
                 None, kind="stage_skipped")
        return True                           # swallowed in production only
