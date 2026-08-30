"""Background Claim x Reference grid for uploaded patent documents.

The ordinary report grid compares the agent's decomposed *elements* with the retrieved
references.  When the query came from an uploaded patent, users also need the patent's own
claims kept intact as rows.  This module builds that second view after the ranked cards are
available, without delaying the first or final report render.

The work is deliberately bounded and honest:

* at most ``MAX_CLAIMS`` claims (all independent claims first, then dependent claims in document
  order) are compared with the top ``MAX_REFS`` ranked references;
* one shared worker performs grids across all searches, so background analysis cannot stampede
  the database or LLM service;
* every cell reuses ``claim_chart.build_chart`` and therefore carries a grounded quotation,
  deterministic source coordinate, and the independent refutation pass;
* the completed JSON is written atomically and survives process restarts.  In-memory state is
  only progress information; a missing cache after a restart is safe to schedule again.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import claim_chart

VERSION = 1
MAX_CLAIMS = 12
MAX_REFS = 8

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="query-claim-grid")
_LOCK = threading.Lock()
_STATUS: dict[str, dict] = {}


def _path(reports: Path, slug: str) -> Path:
    return Path(reports) / f"{slug}.claim-grid.json"


def _read_cache(reports: Path, slug: str) -> dict | None:
    p = _path(reports, slug)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    return data if data.get("version") == VERSION and data.get("status") == "done" else None


def _claims(report: dict) -> tuple[list[dict], int]:
    """Return the bounded, display-ordered claim set and the source document's total count."""
    qd = (report or {}).get("query_document") or {}
    if qd.get("source") != "upload":
        return [], 0
    cleaned = []
    for i, raw in enumerate(qd.get("claims") or []):
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("text") or "").split())
        if not text:
            continue
        try:
            claim_no = int(raw.get("claim_no") or i + 1)
        except (TypeError, ValueError):
            claim_no = i + 1
        cleaned.append({
            "claim_no": claim_no,
            "text": text[:8000],
            "independent": bool(raw.get("independent")),
            "_order": i,
        })
    total = len(cleaned)
    if total <= MAX_CLAIMS:
        selected = cleaned
    else:
        # Keep every independent claim before filling the remaining budget with dependents.  Put
        # the selected rows back into document order so claim 1 never appears below claim 12.
        preferred = [c for c in cleaned if c["independent"]]
        preferred.extend(c for c in cleaned if not c["independent"])
        selected = sorted(preferred[:MAX_CLAIMS], key=lambda c: c["_order"])
    for c in selected:
        c.pop("_order", None)
    return selected, total


def metadata(report: dict) -> dict:
    claims, total = _claims(report)
    qd = (report or {}).get("query_document") or {}
    return {
        "available": bool(claims),
        "source": qd.get("source"),
        "label": qd.get("label") or "uploaded patent",
        "n_claims": total,
        "n_selected": len(claims),
        "max_claims": MAX_CLAIMS,
        "max_refs": MAX_REFS,
    }


def build_grid(report: dict, view: dict) -> dict:
    """Build a complete, JSON-safe Claim x Reference result (synchronous worker body)."""
    started = time.time()
    claims, total_claims = _claims(report)
    cards = [c for c in ((view or {}).get("cards") or []) if c.get("pub")]
    refs = cards[:MAX_REFS]
    if not claims or not refs:
        return {
            "version": VERSION,
            "status": "done",
            "available": False,
            "reason": "no uploaded claims" if not claims else "no ranked references",
            "rows": [],
            "columns": [],
        }

    texts = [c["text"] for c in claims]
    charts = []
    for card in refs:
        try:
            chart = claim_chart.build_chart(texts, card["pub"])
        except Exception as exc:
            chart = {
                "pub": card["pub"],
                "method": "error",
                "rows": [],
                "error": str(exc)[:160],
            }
        charts.append(chart)

    rows = []
    counts = {"disclosed": 0, "partial": 0, "uncertain": 0, "absent": 0}
    for i, claim in enumerate(claims):
        cells = []
        for card, chart in zip(refs, charts):
            source_rows = chart.get("rows") or []
            raw = source_rows[i] if i < len(source_rows) else {}
            verdict = str(raw.get("verdict") or "absent").lower()
            if verdict not in counts:
                verdict = "uncertain"
            counts[verdict] += 1
            cells.append({
                "pub": card["pub"],
                "verdict": verdict,
                "quote": str(raw.get("quote") or "")[:1200],
                "location": str(raw.get("location") or "")[:160],
                "coord": raw.get("coord") if isinstance(raw.get("coord"), dict) else {},
                "confidence": raw.get("confidence", 0.0),
                "grounding": raw.get("grounding") or "no-row-returned",
                "method": raw.get("method") or chart.get("method") or "unknown",
            })
        rows.append({**claim, "cells": cells})

    return {
        "version": VERSION,
        "status": "done",
        "available": True,
        "source": "upload",
        "source_label": ((report.get("query_document") or {}).get("label") or
                         "uploaded patent"),
        "columns": [{
            "pub": c["pub"],
            "title": str(c.get("title") or "")[:300],
            "rank": c.get("rank"),
        } for c in refs],
        "rows": rows,
        "n_claims_total": total_claims,
        "n_claims_shown": len(claims),
        "n_refs_total": len(cards),
        "n_refs_shown": len(refs),
        "truncated_claims": max(0, total_claims - len(claims)),
        "truncated_refs": max(0, len(cards) - len(refs)),
        "counts": counts,
        "seconds": round(time.time() - started, 2),
    }


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, default=str)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _snapshot(slug: str) -> dict:
    st = _STATUS.get(slug)
    if not st:
        return {"slug": slug, "status": "idle", "available": False}
    return {k: v for k, v in st.items() if not k.startswith("_")}


def status(slug: str, reports: Path) -> dict:
    cached = _read_cache(reports, slug)
    if cached is not None:
        return cached
    with _LOCK:
        return _snapshot(slug)


def ensure(slug: str, report: dict, view: dict, reports: Path) -> dict:
    """Schedule one background grid if applicable; return immediately with its status."""
    cached = _read_cache(reports, slug)
    if cached is not None:
        return cached
    meta = metadata(report)
    if not meta["available"]:
        return {"slug": slug, "status": "unavailable", **meta}
    refs = [c for c in ((view or {}).get("cards") or []) if c.get("pub")]
    if not refs:
        return {**meta, "slug": slug, "status": "unavailable", "available": False,
                "reason": "no ranked references"}

    token = f"{time.time_ns()}-{threading.get_ident()}"
    with _LOCK:
        if slug in _STATUS and _STATUS[slug].get("status") in ("queued", "running"):
            return _snapshot(slug)
        _STATUS[slug] = {
            "slug": slug,
            "status": "queued",
            "available": True,
            "n_claims": meta["n_selected"],
            "n_refs": min(len(refs), MAX_REFS),
            "started": time.time(),
            "_token": token,
        }

    def run():
        with _LOCK:
            if slug in _STATUS:
                _STATUS[slug]["status"] = "running"
        try:
            result = build_grid(report, view)
            with _LOCK:
                # A rerun invalidates the slug while an old worker may still be reading.  Only the
                # currently registered generation may publish a cache; otherwise an old uploaded
                # claim set could overwrite the new report's grid after invalidation.
                if _STATUS.get(slug, {}).get("_token") != token:
                    return
                _write_atomic(_path(reports, slug), result)
                _STATUS[slug] = {
                    "slug": slug,
                    "status": "done",
                    "available": bool(result.get("available")),
                    "n_claims": result.get("n_claims_shown", 0),
                    "n_refs": result.get("n_refs_shown", 0),
                }
        except Exception as exc:
            with _LOCK:
                if _STATUS.get(slug, {}).get("_token") != token:
                    return
                _STATUS[slug] = {
                    "slug": slug,
                    "status": "error",
                    "available": True,
                    "error": str(exc)[:200],
                }

    _POOL.submit(run)
    with _LOCK:
        return _snapshot(slug)


def invalidate(slug: str, reports: Path) -> None:
    """Forget a previous grid when its source report is being regenerated."""
    _path(reports, slug).unlink(missing_ok=True)
    with _LOCK:
        _STATUS.pop(slug, None)
