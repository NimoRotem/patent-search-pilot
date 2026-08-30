"""Proactive top-N enrichment for a report's results.

WHY
---
Drawings/PDF/worldwide-family are recovered lazily today: a card shows "no drawing available",
and only when the user clicks does `/api/ref` -> `enrich_display.enrich_for_display` run the
multi-source recovery (Google Patents images -> EPO OPS facsimile -> PDF render). That recovery
works — clicking is exactly why it fixes itself — it just is not triggered up front.

This module CALLS that same recovery eagerly for the TOP-N ranked results (and the Feature-1
worldwide family), so their content is ready without a click. It is deliberately BOUNDED and
THROTTLED:

  * TOP-N only (default 10, `PREFETCH_TOP_N`) — never all 25 and never the long tail, so a burst
    of searches cannot hammer the shared 4 GB/week OPS image budget.
  * a small global worker pool (default 2, `PREFETCH_CONCURRENCY`) shared across ALL searches, so
    prefetch can never starve the live request path or the reranker on the RAM-constrained box.
  * it reuses the EXISTING recovery path (`enrich_display` / `ops_drawings` / `ops_family`), which
    already honor the persisted OPS weekly byte budget + throttle; it does not reimplement any of
    it, and it stops early once the budget guard trips.

Non-blocking: `prefetch_top` returns immediately after scheduling; the report render is never
delayed. Cards pick up the results by polling the disk-only `/api/figs` and `/api/family`
endpoints (see app.js), so a card updates in place as content lands and a genuinely drawing-less
publication still resolves to an honest "no drawing available".
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

TOP_N = int(os.environ.get("PREFETCH_TOP_N", "10"))
CONCURRENCY = max(1, int(os.environ.get("PREFETCH_CONCURRENCY", "2")))

# One shared pool across every search, so the total prefetch pressure on OPS + CPU is bounded
# regardless of how many reports are open.
_POOL = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="prefetch")
_LOCK = threading.Lock()
_STATUS: dict[str, dict] = {}      # slug -> {pubs, done, running, started, budget_stop}


def _snapshot(slug):
    st = _STATUS.get(slug)
    if not st:
        return {"slug": slug, "started": False, "running": False, "pubs": [], "done": [], "n": 0}
    return {"slug": slug, "started": True, "running": st["running"],
            "pubs": st["pubs"], "done": sorted(st["done"]), "n": len(st["pubs"]),
            "budget_stop": st.get("budget_stop", False)}


def status(slug):
    with _LOCK:
        return _snapshot(slug)


def _one(slug, pub):
    """Recover drawings + worldwide family for one publication via the EXISTING paths."""
    import ops
    # Respect the shared weekly OPS budget: if it is spent, stop touching OPS entirely — the
    # recovery paths would just no-op, but checking here lets us flag it and avoid busywork.
    try:
        if ops.have_creds() and ops.budget_remaining() <= 0:
            with _LOCK:
                _STATUS[slug]["budget_stop"] = True
            return
    except Exception:
        pass
    try:
        import enrich_display
        enrich_display.enrich_for_display(pub)     # cached + idempotent; runs full recovery if cold
    except Exception:
        pass
    try:
        import ops_family
        ops_family.fetch_family(pub)               # cached forever; INPADOC family -> timeline
    except Exception:
        pass
    with _LOCK:
        st = _STATUS.get(slug)
        if st is not None:
            st["done"].add(pub)


def prefetch_top(slug, pubs, n=None):
    """Schedule proactive enrichment of the top-N `pubs` (already in listwise rank order).

    Idempotent per process: a slug is only ever scheduled once. Returns the status snapshot
    (including the exact pub list scheduled) immediately; work happens on the shared pool.
    """
    n = TOP_N if n is None else n
    top = [p for p in (pubs or []) if p][:max(0, n)]
    with _LOCK:
        if slug in _STATUS:                        # already scheduled this process — do not double-run
            return _snapshot(slug)
        _STATUS[slug] = {"pubs": top, "done": set(), "running": bool(top),
                         "started": time.time(), "budget_stop": False}
    if not top:
        return status(slug)

    def _runner():
        futs = [_POOL.submit(_one, slug, p) for p in top]
        for f in futs:
            try:
                f.result()
            except Exception:
                pass
        with _LOCK:
            if slug in _STATUS:
                _STATUS[slug]["running"] = False

    threading.Thread(target=_runner, name=f"prefetch-{slug[:16]}", daemon=True).start()
    return status(slug)
