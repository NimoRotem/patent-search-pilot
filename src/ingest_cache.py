"""Remember what a document turned out to be, so the same input is never analysed twice.

WHY. Turning an input into a search is the whole of the wait before a search starts: fetch the
publication, segment it, split the claims, condense a search brief, embed every chunk. That is
model time over the whole document, and it was being paid AGAIN on every search over the same
patent. `US20260070232A1` has been the input to this bench dozens of times and was rebuilt from
scratch dozens of times, because the stash it produced was keyed on `uuid.uuid4().hex`: a fresh
random name per upload, which no later search could ever match.

The answer a document analysis gives is a pure function of the document. So it is keyed on the
document: the canonical publication number for a link, the sha256 of the bytes for an upload.
Same input, same key, same answer, no model calls.

WHAT INVALIDATES IT. `VERSION` — bump it when the shape of an analysis changes, and every entry
is a miss from that moment. And age: an entry older than `TTL_DAYS` is re-read, because a
publication that was pre-grant when we first saw it may have issued since, and its claims are then
different claims.

NEVER FAILS THE CALLER. A cache miss and a broken cache are the same thing to the caller: it does
the work. Every path here swallows its own errors and returns None.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import traceback

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "ingest_cache")
#  Bump to invalidate everything: the entries hold a whole analysis, and one whose shape has
#  changed is worse than no entry at all.
VERSION = "1"
TTL_DAYS = float(os.environ.get("INGEST_CACHE_TTL_DAYS", "30"))
#  A single analysis carries every chunk vector and the figure images. Big, but a hundredth of what
#  it costs to rebuild; the ceiling is here so one pathological 300-page grant cannot fill a disk.
MAX_BYTES = int(os.environ.get("INGEST_CACHE_MAX_BYTES", str(48 * 1024 * 1024)))
ENABLED = os.environ.get("INGEST_CACHE", "1") != "0"


def key_for_pub(pub) -> str:
    return "pub-%s" % hashlib.sha256(
        ("%s|%s" % (VERSION, str(pub or "").strip().upper())).encode()).hexdigest()[:40]


def key_for_bytes(data, filename="") -> str:
    h = hashlib.sha256()
    h.update(VERSION.encode())
    h.update(b"|")
    #  The name is part of the key because it decides how the bytes are parsed: the same payload
    #  read as a PDF and as a DOCX are two different documents.
    h.update(str(os.path.splitext(filename or "")[1]).lower().encode())
    h.update(b"|")
    h.update(data or b"")
    return "up-%s" % h.hexdigest()[:40]


def _path(key):
    return os.path.join(DIR, "%s.json" % key)


def get(key):
    """The stored analysis, or None. Never raises."""
    if not ENABLED or not key:
        return None
    p = _path(key)
    try:
        st = os.stat(p)
        if TTL_DAYS and (time.time() - st.st_mtime) > TTL_DAYS * 86400:
            return None
        with open(p) as fh:
            got = json.load(fh)
        if not isinstance(got, dict) or not got.get("ok"):
            return None
        #  A caller may mutate what it gets back, and the next caller must not see that.
        return json.loads(json.dumps(got))
    except FileNotFoundError:
        return None
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return None


def put(key, res):
    """Store one analysis. Returns `res` so it can wrap a return. Never raises."""
    if not ENABLED or not key or not isinstance(res, dict) or not res.get("ok"):
        return res
    try:
        blob = json.dumps(res)
    except (TypeError, ValueError):
        #  Something in here is not JSON. That is a miss for ever rather than a crash now, and it
        #  is worth saying once: an analysis that cannot be stored is one nobody can reuse.
        traceback.print_exc()
        return res
    if len(blob) > MAX_BYTES:
        print("[ingest_cache] %s not stored: %.1f MB over the %.0f MB ceiling"
              % (key, len(blob) / 1e6, MAX_BYTES / 1e6), flush=True)
        return res
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = _path(key) + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(blob)
        os.replace(tmp, _path(key))
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
    return res


def stats():
    """What is in here, for the settings page. -> {"entries", "bytes", "oldest_days"}"""
    out = {"entries": 0, "bytes": 0, "oldest_days": 0.0}
    try:
        now = time.time()
        oldest = now
        for name in os.listdir(DIR):
            if not name.endswith(".json"):
                continue
            st = os.stat(os.path.join(DIR, name))
            out["entries"] += 1
            out["bytes"] += st.st_size
            oldest = min(oldest, st.st_mtime)
        if out["entries"]:
            out["oldest_days"] = round((now - oldest) / 86400.0, 1)
    except FileNotFoundError:
        pass
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
    return out


def clear():
    """Drop every entry. -> how many went."""
    n = 0
    try:
        for name in os.listdir(DIR):
            if name.endswith(".json"):
                os.remove(os.path.join(DIR, name))
                n += 1
    except FileNotFoundError:
        pass
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
    return n
