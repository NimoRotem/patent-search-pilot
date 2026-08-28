"""Read the corpus factory's one-minute pulse, for the Factory tab.

The factory runs on five machines that this app has no database connection to, and deliberately
so: a search process that can reach the staging database is a search process that can write to it.
The publisher on the discovery VM writes a small JSON snapshot to GCS every minute, and this reads
that object.

STALENESS IS COMPUTED FROM THE DATA, NEVER CLAIMED BY A PROCESS. The page says live or stale from
the age of `generated_at`. If the publisher dies, the numbers stop moving and the badge turns
stale, which is the honest failure: an empty page would read as "the factory produced nothing".
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UTC = timezone.utc

PULSE_URI = os.environ.get(
    "NICHE_PULSE_URI", "gs://nimo-patents-v3/niche_full_v1/status/pulse.json"
)
#  The page polls every 60 seconds and several readers may share one process, so the object is
#  fetched at most this often. A pulse is published once a minute; reading it more often than that
#  buys nothing.
CACHE_SECONDS = int(os.environ.get("NICHE_PULSE_CACHE_SECONDS", "20"))
#  A pulse older than this is stale. Two missed publications, so one slow collection does not
#  flip the badge.
LIVE_WITHIN_SECONDS = int(os.environ.get("NICHE_PULSE_LIVE_SECONDS", "180"))

_METADATA_TOKEN = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
_cache: dict = {"at": 0.0, "document": None, "error": ""}


def _token(timeout: float = 5.0) -> str:
    request = urllib.request.Request(_METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return str(json.loads(response.read().decode("utf-8")).get("access_token") or "")


def fetch(uri: str = "", *, timeout: float = 10.0) -> dict:
    """The published pulse object. Stdlib only: this must not add a dependency to the web tier."""
    parsed = urllib.parse.urlsplit(uri or PULSE_URI)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError("pulse URI must be a gs:// object")
    url = "https://storage.googleapis.com/storage/v1/b/{}/o/{}?alt=media".format(
        urllib.parse.quote(parsed.netloc, safe=""),
        urllib.parse.quote(parsed.path.lstrip("/"), safe=""),
    )
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _age_seconds(stamp) -> float | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - moment).total_seconds(), 0.0)


def _trend(history: list, key: str, minutes: int = 10) -> float:
    """Movement per minute over the last `minutes` entries, which is steadier than one interval."""
    points = [entry for entry in history if entry.get(key) is not None][-(minutes + 1):]
    if len(points) < 2:
        return 0.0
    first, last = points[0], points[-1]
    span = (_age_seconds(first.get("generated_at")) or 0) - (_age_seconds(last.get("generated_at")) or 0)
    if span <= 0:
        return 0.0
    return round((float(last[key]) - float(first[key])) * 60.0 / span, 1)


def _embedded(entry: dict) -> int:
    return int((entry.get("embedding") or {}).get("complete") or 0)


def view(uri: str = "", *, force: bool = False) -> dict:
    """What the Factory tab renders. Never raises: a read failure is a state, not a 500."""
    now = time.monotonic()
    if force or _cache["document"] is None or now - _cache["at"] > CACHE_SECONDS:
        try:
            _cache.update(document=fetch(uri), at=now, error="")
        except Exception as exc:  # noqa: BLE001 - the page must render without the factory
            _cache.update(at=now, error=f"{type(exc).__name__}: {str(exc)[:160]}")
    document = _cache["document"] or {}
    current = dict(document.get("now") or {})
    history = list(document.get("history") or [])
    age = _age_seconds(current.get("generated_at"))
    embedding = current.get("embedding") or {}
    embedded, pending = int(embedding.get("complete") or 0), int(embedding.get("pending") or 0)
    chunks = int(current.get("chunks") or 0)
    publications = int(current.get("publications") or 0)
    parse = current.get("parse") or {}
    pool = current.get("pool") or {}
    budget = current.get("budget") or {}
    rates = dict(current.get("rates_per_minute") or {})
    for key in ("vectors", "chunks", "publications", "fetched"):
        rates.setdefault(key, 0.0)
    trends = {
        "vectors": _trend(history, "vectors"),
        "chunks": _trend(history, "chunks"),
        "fetched": _trend(history, "fetched"),
        "claims_complete": _trend(history, "claims_complete"),
    }
    embedded_points = [
        {"generated_at": entry.get("generated_at"), "embedded": _embedded(entry)}
        for entry in history if entry.get("embedding")
    ]
    embedded_rate = _trend(
        [{"generated_at": p["generated_at"], "value": p["embedded"]} for p in embedded_points],
        "value",
    )
    #  An ETA from a rate measured over two nearly identical snapshots is not an estimate, it is a
    #  number with a unit. Embedding arrives in batches of thousands, so anything under a batch a
    #  minute cannot support a projection and the page says so instead.
    remaining_minutes = (pending / embedded_rate) if embedded_rate >= 200 else None
    return {
        "ok": bool(current),
        "error": _cache["error"],
        "generated_at": current.get("generated_at"),
        "age_seconds": None if age is None else round(age),
        "live": bool(age is not None and age <= LIVE_WITHIN_SECONDS),
        "collect_ms": current.get("collect_ms"),
        "headline": {
            "publications": publications,
            "families": int(current.get("families") or 0),
            "claims_complete": int(current.get("claims_complete") or 0),
            "description_complete": int(current.get("description_complete") or 0),
            "chunks": chunks,
            "vectors": int(current.get("vectors") or 0),
            "lexical_indexed": int(current.get("lexical_indexed") or 0),
            "embedded": embedded,
            "embedding_pending": pending,
            "embedded_pct": round(100.0 * embedded / (embedded + pending), 2)
            if (embedded + pending) else 0.0,
            "claims_pct": round(100.0 * int(current.get("claims_complete") or 0) / publications, 2)
            if publications else 0.0,
            "description_pct": round(
                100.0 * int(current.get("description_complete") or 0) / publications, 2
            ) if publications else 0.0,
        },
        "rates_per_minute": rates,
        "trend_per_minute": {**trends, "embedded": embedded_rate},
        "embedding_eta_hours": round(remaining_minutes / 60.0, 1)
        if remaining_minutes else None,
        #  A pool read that failed is NOT a pool that did nothing. The page shows the error and
        #  renders the counts as unknown, because a zero here reads as "acquisition has stopped".
        "pool_error": current.get("pool_error") or "",
        "queues": {
            "parse": parse,
            "parse_stuck": int(current.get("parse_stuck") or 0),
            "pool": pool,
            "pool_hits_5m": int(current.get("pool_hits_5m") or 0),
            "batches": current.get("batches") or {},
        },
        "budget": {
            "limit_usd": float(budget.get("limit_usd") or 0),
            "spent_usd": float(budget.get("spent_usd") or 0),
            "spent_pct": round(
                100.0 * float(budget.get("spent_usd") or 0) / float(budget.get("limit_usd") or 1), 1
            ),
        },
        "discovery_reached": int(current.get("discovery_reached") or 0),
        "history": [
            {
                "at": entry.get("generated_at"),
                "vectors": int(entry.get("vectors") or 0),
                "chunks": int(entry.get("chunks") or 0),
                "publications": int(entry.get("publications") or 0),
                "embedded": _embedded(entry),
            }
            for entry in history[-60:]
        ],
    }
