"""Record and replay every external call, so two benchmark runs see the same outside world.

WHY THIS BLOCKS THE ACQUISITION EXPERIMENT
------------------------------------------
Measured: one benchmark subject delivered 3 of 11 gold references in an oracle control arm and 0 of
11 in a batch run of the SAME subject hours earlier, with no code change between them. That spread
is as large as the effect any of our experiments has produced, which means a control-versus-
treatment comparison currently cannot distinguish a real improvement from the weather.

The largest exogenous source is the external fan-out: seven APIs, ranked results that drift, quotas
that bite at different times of day, and one source (Lens) that has been returning 401 for weeks.
Freezing it turns "the outside world" from a variable into a constant.

MODES
    off       no cache. Live call every time. For ordinary production use.
    record    serve from cache when present, otherwise call live AND store. Use for the first
              pass over a benchmark.
    replay    cache only. A MISS IS A RUN FAILURE, never a live call. This is what makes a
              comparison honest: if the treatment arm quietly fetched something the control arm
              did not, the corpus is no longer the only thing that changed.

WHAT IS STORED. The raw response body as well as the parsed object, because a parser change should
be re-testable against identical bytes rather than requiring a fresh fetch. The key covers the full
request payload plus explicit version stamps, so changing an adapter or a normalisation rule
invalidates the cache deliberately instead of silently serving results the new code would not have
produced.

WHERE THE SEAM IS. At `external.bulk`, which is the pilot's single external dependency: all seven
sources are reached through the engine's /api/bulk_search. That means a parser change INSIDE the
engine is not independently replayable here, only the engine's output is. Recorded as a known
limit rather than pretended away.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.environ.get("REPLAY_DIR", os.path.join(ROOT, "data", "replay"))

#  Bump deliberately when the request shape, an adapter, or a normalisation rule changes. A cache
#  hit under a stale version would serve results the current code could not have produced.
ADAPTER_VERSION = os.environ.get("REPLAY_ADAPTER_VERSION", "2026-08-06.1")
NORMALIZATION_VERSION = os.environ.get("REPLAY_NORMALIZATION_VERSION", "2026-08-06.1")

OFF, RECORD, REPLAY = "off", "record", "replay"


def mode() -> str:
    m = (os.environ.get("REPLAY_MODE") or OFF).strip().lower()
    return m if m in (OFF, RECORD, REPLAY) else OFF


def enabled() -> bool:
    return mode() in (RECORD, REPLAY)


def key(namespace: str, payload) -> str:
    """Stable hash of the full request plus the version stamps."""
    blob = json.dumps({"ns": namespace, "adapter": ADAPTER_VERSION,
                       "norm": NORMALIZATION_VERSION, "payload": payload},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _path(namespace: str, k: str) -> str:
    return os.path.join(DIR, namespace, f"{k}.json")


def get(namespace: str, payload):
    """The recorded parsed response, or None. Never raises."""
    if not enabled():
        return None
    p = _path(namespace, key(namespace, payload))
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p)).get("parsed")
    except Exception:
        return None


def put(namespace: str, payload, parsed, raw: str = "") -> str:
    """Store a response. -> the path written, or '' when caching is off."""
    if not enabled():
        return ""
    k = key(namespace, payload)
    p = _path(namespace, k)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rec = {"namespace": namespace, "key": k, "adapter_version": ADAPTER_VERSION,
           "normalization_version": NORMALIZATION_VERSION,
           "recorded_at": int(time.time()), "payload": payload,
           "raw": (raw or "")[:8_000_000], "parsed": parsed}
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh, default=str)
    os.replace(tmp, p)
    return p


def miss(namespace: str, payload) -> None:
    """Called when replay mode has no recording. Always a run failure.

    A silent live call here is the whole problem: it would let the treatment arm see a different
    outside world from the control arm while the comparison claims the corpus was the only change.
    """
    import failclosed
    k = key(namespace, payload)
    msg = (f"REPLAY MISS for key {k[:16]} in {namespace}. Replay mode forbids a live call, "
           f"because a call the control arm did not make makes the comparison meaningless. "
           f"Record this run first (REPLAY_MODE=record) or fix the payload drift.")
    #  Recorded in the run's failure report AND raised. Unconditionally: failclosed.fallback only
    #  raises in benchmark mode, but replay mode being on IS the statement that this run is one
    #  arm of a controlled comparison, so a miss is fatal whether or not it is a benchmark.
    try:
        failclosed.fallback(f"replay.{namespace}", msg, None, kind="replay_miss")
    except Exception:
        raise
    raise failclosed.DegradedRun(msg)


def stats(namespace: str = "") -> dict:
    """What is on disk, for a manifest entry."""
    out = {}
    base = os.path.join(DIR, namespace) if namespace else DIR
    if not os.path.isdir(base):
        return {"entries": 0, "bytes": 0, "dir": base}
    n = size = 0
    for root, _dirs, files in os.walk(base):
        for f in files:
            if f.endswith(".json"):
                n += 1
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    out.update({"entries": n, "bytes": size, "dir": base, "mode": mode(),
                "adapter_version": ADAPTER_VERSION,
                "normalization_version": NORMALIZATION_VERSION})
    return out
