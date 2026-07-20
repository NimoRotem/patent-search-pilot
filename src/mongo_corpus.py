"""Read-only client for the pre-built lemad patent corpus (39.4M docs), the same source
iptorch.com serves its detail + drawings from.

WHY
---
Our old display path recovered figures LIVE per publication (SerpApi google_patents_details ->
Google page scrape -> EPO OPS facsimile -> PDF raster). That is slow and flaky, and it returned
zero images for `US2019168875A1` purely because of the dropped-leading-zero number bug (see
pubnorm). iptorch shows everything instantly because it does NOT recover anything — it reads a
pre-built MongoDB corpus keyed by `publicationNumber`, where one `find_one` yields title,
abstract, claims, description, classifications, inventors, assignees, pdf, and figures[] as
ready-to-render Google-CDN URLs ({full, thumbnail}). We do the same, first, and fall back to the
existing live recovery only on a genuine miss.

SHARED EXTERNAL HOST — SAFETY CONTRACT
--------------------------------------
bigquery.lemad.ai:27017 is a shared box we do not control, so a slow or wedged Mongo must NEVER
stall the live web app:
  * short server-selection / connect / socket timeouts (a hung host fails fast to None);
  * ONE module-level client, reused (no per-call connection storm);
  * a bounded concurrency gate (BoundedSemaphore) so at most N requests are ever in Mongo at once
    — extra callers fail fast to None and take the live-recovery fallback instead of queueing;
  * an on-disk cache keyed by the canonical pub (hits cached forever, misses cached briefly) so a
    given publication is looked up at most once;
  * every path is wrapped: get_detail returns None on miss/timeout/error and NEVER raises into
    the request path.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import List, Optional

import pubnorm

try:
    from config import DATA
except Exception:                                   # pragma: no cover - config always present in app
    DATA = Path(os.environ.get("PATENT_DATA", "data"))

# --- connection tunables (env-overridable for ops without a redeploy) ------------------
MONGO_URI = os.environ.get(
    "LEMAD_MONGO_URI",
    "mongodb://root:fSnNdeIpmdnuimvMs1aAGLbZE@bigquery.lemad.ai:27017/",
)
MONGO_DB = os.environ.get("LEMAD_MONGO_DB", "lemad-patents-staging")
MONGO_COLL = os.environ.get("LEMAD_MONGO_COLL", "patents-datas")
SERVER_SEL_MS = int(os.environ.get("LEMAD_MONGO_SEL_MS", "2500"))
CONNECT_MS = int(os.environ.get("LEMAD_MONGO_CONNECT_MS", "2500"))
SOCKET_MS = int(os.environ.get("LEMAD_MONGO_SOCKET_MS", "4000"))
MAX_CONCURRENCY = int(os.environ.get("LEMAD_MONGO_CONCURRENCY", "8"))
ACQUIRE_TIMEOUT_S = float(os.environ.get("LEMAD_MONGO_ACQUIRE_S", "3.0"))
NEG_TTL_S = int(os.environ.get("LEMAD_MONGO_NEG_TTL", str(6 * 3600)))   # re-check misses after 6h
DISABLED = os.environ.get("LEMAD_MONGO_DISABLED", "").strip() not in ("", "0", "false", "False")

CACHE_DIR = DATA / "mongo_cache"

_client = None
_client_lock = threading.Lock()
_client_failed = False
_sem = threading.BoundedSemaphore(MAX_CONCURRENCY)


def available() -> bool:
    """True unless explicitly disabled or the driver is missing. Does NOT open a connection.

    Auto-disabled under pytest (so the suite never touches the shared external host) unless a
    test opts in with LEMAD_MONGO_TEST=1 — tests that need the client inject a fake collection."""
    if DISABLED:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("LEMAD_MONGO_TEST"):
        return False
    try:
        import pymongo  # noqa: F401
    except Exception:
        return False
    return True


def _get_collection():
    """Module-level, lazily-created collection handle, or None if the driver/connection is out.

    Reused across calls. A one-time connection failure is remembered so we do not pay the
    server-selection timeout on every request when the host is down."""
    global _client, _client_failed
    if DISABLED or _client_failed:
        return None
    if _client is not None:
        try:
            return _client[MONGO_DB][MONGO_COLL]
        except Exception:
            return None
    with _client_lock:
        if _client is not None:
            return _client[MONGO_DB][MONGO_COLL]
        if _client_failed:
            return None
        try:
            import pymongo
            _client = pymongo.MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=SERVER_SEL_MS,
                connectTimeoutMS=CONNECT_MS,
                socketTimeoutMS=SOCKET_MS,
                maxPoolSize=MAX_CONCURRENCY,
                retryReads=True,
                appname="patent-pilot-display",
            )
            return _client[MONGO_DB][MONGO_COLL]
        except Exception:
            _client_failed = True
            _client = None
            return None


# ---------------------------------------------------------------------------
# on-disk cache
# ---------------------------------------------------------------------------
def _cache_file(canon: str) -> Optional[Path]:
    # canon is CC-NUMBER[-KIND]; safe as a filename by construction. Guard length anyway.
    if not canon or len(canon) > 60 or "/" in canon or "\\" in canon:
        return None
    return CACHE_DIR / f"{canon}.json"


def _read_cache(canon: str):
    """Return the cached record dict, or None. A record is {'detail': <dict>|None, 'ts': <epoch>}.
    A negative record (detail is None) is honoured only until NEG_TTL_S has elapsed."""
    f = _cache_file(canon)
    if not f or not f.exists():
        return None
    try:
        rec = json.loads(f.read_text())
    except Exception:
        return None
    if rec.get("detail") is None:
        if time.time() - float(rec.get("ts", 0)) > NEG_TTL_S:
            return None                      # stale miss -> re-check Mongo
    return rec


def _write_cache(canon: str, detail) -> None:
    f = _cache_file(canon)
    if not f:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"detail": detail, "ts": time.time()}, default=str))
        tmp.replace(f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# doc -> normalized display detail
# ---------------------------------------------------------------------------
def _flatten_classifications(raw) -> List[dict]:
    """Mongo stores classifications as a list of CPC hierarchies (list of lists of {cpc,text}).
    Surface the most-specific (leaf) symbol of each hierarchy as a chip, de-duplicated, matching
    the {code, description, first, cpc} shape enrich_display already emits."""
    out: List[dict] = []
    seen = set()
    for group in (raw or []):
        if not isinstance(group, list) or not group:
            continue
        leaf = None
        for node in group:                              # deepest symbol = last node with a cpc
            if isinstance(node, dict) and node.get("cpc"):
                leaf = node
        if not leaf:
            continue
        code = str(leaf.get("cpc"))
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "description": leaf.get("text"),
                    "first": (len(out) == 0), "cpc": True})
    return out


def _claims(raw) -> Optional[List[str]]:
    """claims[] -> list of claim text strings. Each element is {parent, contents?}; parent is the
    claim body, contents (when present) are sub-paragraphs to append."""
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for c in raw:
        if isinstance(c, dict):
            parts = []
            if c.get("parent"):
                parts.append(str(c["parent"]).strip())
            cont = c.get("contents") or c.get("content")
            if isinstance(cont, list):
                parts.extend(str(x).strip() for x in cont if x)
            elif isinstance(cont, str) and cont.strip():
                parts.append(cont.strip())
            txt = "\n".join(p for p in parts if p)
            if txt:
                out.append(txt)
        elif isinstance(c, str) and c.strip():
            out.append(c.strip())
    return out or None


def _description(raw, cap: int = 400) -> Optional[List[str]]:
    """description[] -> flat list of paragraph strings. Each element is {title, content[]}."""
    if not isinstance(raw, list) or not raw:
        return None
    out: List[str] = []
    for sec in raw:
        if not isinstance(sec, dict):
            if isinstance(sec, str) and sec.strip():
                out.append(sec.strip())
            continue
        title = (sec.get("title") or "").strip()
        if title:
            out.append(title)
        for para in (sec.get("content") or sec.get("contents") or []):
            if isinstance(para, str) and para.strip():
                out.append(para.strip())
        if len(out) >= cap:
            break
    return out[:cap] or None


def _names(raw) -> List[str]:
    out = []
    for x in (raw or []):
        if isinstance(x, dict) and x.get("name"):
            out.append(str(x["name"]))
        elif isinstance(x, str) and x.strip():
            out.append(x.strip())
    return out


def _figures(raw) -> List[dict]:
    """figures[] -> [{full, thumbnail}] Google-CDN URLs that render directly (no download)."""
    out = []
    for f in (raw or []):
        if not isinstance(f, dict):
            continue
        full = f.get("full") or f.get("thumbnail")
        thumb = f.get("thumbnail") or f.get("full")
        if full:
            out.append({"full": full, "thumbnail": thumb})
    return out


def _pub_date(doc) -> Optional[str]:
    """Best-effort YYYY-MM-DD from `date` (ISO) or `publicationDate` (int YYYYMMDD)."""
    d = doc.get("date")
    if isinstance(d, str) and len(d) >= 10:
        return d[:10]
    pd = doc.get("publicationDate")
    if isinstance(pd, int) and pd > 10000000:
        s = str(pd)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _normalize(doc: dict, canon: str, matched_key: str) -> dict:
    """Mongo doc -> the normalized detail dict enrich_display folds into its display payload.
    Assignees live under the misspelled `asignees` field in this corpus; we read both."""
    figs = _figures(doc.get("figures"))
    return {
        "pub": canon,
        "mongo_key": matched_key,
        "title": doc.get("title"),
        "abstract": doc.get("abstract"),
        "assignees": _names(doc.get("asignees") or doc.get("assignees")),
        "inventors": _names(doc.get("inventors")),
        "country": doc.get("country") or (canon[:2] if canon else None),
        "publication_date": _pub_date(doc),
        "lang": doc.get("lang"),
        "classifications": _flatten_classifications(doc.get("classifications")),
        "claims": _claims(doc.get("claims")),
        "description": _description(doc.get("description")),
        "figures": figs,                 # [{full, thumbnail}] — ready-to-render CDN URLs
        "n_figures": len(figs),
        "pdf_url": doc.get("pdf"),
        "status": ("active" if doc.get("isFinal") else None) if doc.get("isFinal") is not None
                  else doc.get("status"),
    }


def _is_useful(detail: dict) -> bool:
    """A doc carries something worth showing. The corpus contains vector-only stubs (a
    publicationNumber + embedding, everything else empty); those must count as a MISS so the
    caller still runs live recovery rather than caching a blank as a hit. A doc with a title or
    abstract but no figures is PARTIAL, not empty — we keep its text AND let the caller recover
    figures separately (this is where we beat iptorch, which only has Mongo)."""
    return bool(detail.get("figures") or detail.get("claims") or detail.get("description")
                or detail.get("abstract") or detail.get("title"))


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def get_detail(pub, use_cache: bool = True) -> Optional[dict]:
    """Full display detail for a publication from the lemad Mongo corpus, or None on miss/timeout.

    Tries pubnorm.mongo_candidates(pub) in order (padded US pre-grant key FIRST — the dropped-zero
    fix) until one hits. Bounded, timeout-guarded, cached. NEVER raises into the request path."""
    canon = pubnorm.canonical(pub)
    if not canon:
        return None
    if use_cache:
        rec = _read_cache(canon)
        if rec is not None:
            return rec.get("detail")

    if not available():
        return None

    candidates = pubnorm.mongo_candidates(pub)
    if not candidates:
        return None

    acquired = _sem.acquire(timeout=ACQUIRE_TIMEOUT_S)
    if not acquired:
        return None                          # Mongo is saturated -> fall back, do not queue
    try:
        col = _get_collection()
        if col is None:
            return None
        detail = None
        for key in candidates:
            try:
                doc = col.find_one({"publicationNumber": key})
            except Exception:
                return None                  # timeout / driver error -> None, keep negative cache clean
            if doc:
                d = _normalize(doc, canon, key)
                if _is_useful(d):            # skip vector-only stubs; try the next spelling
                    detail = d
                    break
        _write_cache(canon, detail)          # detail (hit) or None (negative, short TTL)
        return detail
    finally:
        _sem.release()


def has_figures(pub) -> bool:
    d = get_detail(pub)
    return bool(d and d.get("figures"))


def ping(timeout_ms: int = 2000) -> bool:
    """One-shot connectivity probe for diagnostics. Never raises."""
    if not available():
        return False
    try:
        col = _get_collection()
        if col is None:
            return False
        col.database.client.admin.command("ping")
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:] or ["US-2019168875-A1", "US2019168875A1", "US20190168875A1"]:
        d = get_detail(p)
        if d:
            print(f"{p}  key={d['mongo_key']}  figs={d['n_figures']} claims={len(d.get('claims') or [])} "
                  f"desc={len(d.get('description') or [])} cls={len(d.get('classifications') or [])} "
                  f"pdf={bool(d.get('pdf_url'))}  title={d.get('title')!r}")
        else:
            print(f"{p}  -> MISS")
