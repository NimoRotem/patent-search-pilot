#!/usr/bin/env python3
"""The shard's Tantivy lexical server on :8635. Stdlib plus the `tantivy` extension module.

WHAT IT OWNS AND WHAT IT DOES NOT. It owns that a real Tantivy is installed on the shard, that a
process is listening, that the index on disk is opened rather than assumed, and that the answer to
"can you serve a lexical query?" is the truth. It does NOT own the schema, the analyzer chain or
what gets indexed: `docs/lexical_interface.md` gives that to workstream C, and the index directory
is filled by whoever builds the shard's corpus release.

`available` IS FALSE UNLESS A REAL INDEX WITH DOCUMENTS IN IT OPENS. That is the whole point and it
is not caution. An empty result set and a genuine miss are indistinguishable to fusion, and a miss
is scored as a recall failure, so a backend that is mid build, out of disk or pointed at an empty
directory must say `available: false` and let the lexical channel fall back to Postgres, which is
the existing fail-soft contract. A server that answered `[]` and called itself available would
silently delete the lexical channel from every cold query.

THE FIVE STATES IT REPORTS, in `state`:

    missing     the `tantivy` module is not installed on this shard at all
    absent      Tantivy is installed and there is no index directory to open
    building    the directory exists but Tantivy will not open it as an index yet
    empty       the index opens and holds zero documents
    ready       the index opens and holds documents. The only state with available: true

Endpoints:
    GET /health              the whole picture, always 200
    GET /ready               200 when available, 503 otherwise
    GET /search?q=&limit=    a real query against the real index, so "running" is checkable
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SHARD_TANTIVY_PORT", "8635"))
SHARD_ID = os.environ.get("SHARD_ID", "")
INDEX_DIR = os.environ.get("SHARD_TANTIVY_INDEX", "/opt/patents-shard/tantivy/index")
#  The index is opened once and reopened only when the directory changes underneath us, because
#  opening an index is not free and the manager polls health once a second during a wake.
RELOAD_SECONDS = float(os.environ.get("SHARD_TANTIVY_RELOAD_SECONDS", "10"))
#  The fields /search falls back to when the index's schema names no default search field. These
#  are the `chunks.kind` values from docs/lexical_interface.md; a field the index does not have is
#  ignored by the parser, so naming more than exist is safe and naming none is not.
FIELDS = [f for f in os.environ.get(
    "SHARD_TANTIVY_FIELDS",
    "text,abstract,claim_own,claim_resolved,whole,paragraph,title,figure_caption").split(",") if f]

try:
    import tantivy                                                     # noqa: F401
    _IMPORT_ERROR = ""
except Exception as e:                                                 # noqa: BLE001
    tantivy = None
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"[:200]

_LOCK = threading.Lock()
_STATE = {"at": 0.0, "index": None, "payload": None}


def _engine_version():
    if tantivy is None:
        return ""
    for attr in ("__version__", "version"):
        v = getattr(tantivy, attr, None)
        if isinstance(v, str):
            return v
    try:
        from importlib.metadata import version
        return version("tantivy")
    except Exception:
        return "unknown"


def _dir_files():
    try:
        return len([f for f in os.listdir(INDEX_DIR) if not f.startswith(".")])
    except Exception:
        return 0


def _probe():
    """-> (payload, index or None). The one place a state is decided."""
    base = {"shard": SHARD_ID, "engine": "tantivy" if tantivy is not None else "none",
            "engine_version": _engine_version(), "index_dir": INDEX_DIR,
            "index_files": _dir_files(), "listening": True, "num_docs": 0}
    if tantivy is None:
        base.update(state="missing", available=False,
                    note=f"the tantivy module is not installed on this shard: {_IMPORT_ERROR}")
        return base, None
    if not os.path.isdir(INDEX_DIR) or base["index_files"] == 0:
        base.update(state="absent", available=False,
                    note="no index has been built here yet; the lexical channel falls back to "
                         "Postgres")
        return base, None
    try:
        index = tantivy.Index.open(INDEX_DIR)
        searcher = index.searcher()
        n = int(searcher.num_docs)
    except Exception as e:                                             # noqa: BLE001
        base.update(state="building", available=False,
                    note=f"the index directory will not open as a Tantivy index: "
                         f"{type(e).__name__}: {str(e)[:160]}")
        return base, None
    base["num_docs"] = n
    if n == 0:
        base.update(state="empty", available=False,
                    note="the index opens and holds no documents, which is not the same thing as "
                         "a query that matched nothing")
        return base, None
    base.update(state="ready", available=True, note="")
    return base, index


def snapshot(force=False):
    with _LOCK:
        fresh = _STATE["payload"] is not None and (time.time() - _STATE["at"]) < RELOAD_SECONDS
        if fresh and not force:
            return dict(_STATE["payload"]), _STATE["index"]
    payload, index = _probe()
    payload["at"] = time.time()
    with _LOCK:
        _STATE["at"], _STATE["payload"], _STATE["index"] = time.time(), payload, index
    return dict(payload), index


def search(query, limit=20):
    """A real query against the real index. -> {"hits": [...]} or {"error": ...}."""
    payload, index = snapshot()
    if not payload.get("available") or index is None:
        return {"error": payload.get("note") or payload.get("state"), "state": payload["state"],
                "hits": []}
    try:
        searcher = index.searcher()
        try:
            q = index.parse_query(query)
        except Exception:
            #  A schema with no default search fields needs them named, and tantivy's Schema does
            #  not expose its field names, so they cannot be discovered. SHARD_TANTIVY_FIELDS is
            #  how whoever built the index says what they are; the default is the `chunks.kind`
            #  values docs/lexical_interface.md lists, which is what a shard index should hold.
            q = index.parse_query(query, FIELDS)
        hits = []
        for score, address in searcher.search(q, int(limit)).hits:
            try:
                doc = searcher.doc(address).to_dict()
            except Exception:                                          # noqa: BLE001
                doc = {}
            hits.append({"score": float(score), "doc": doc})
        return {"hits": hits, "state": payload["state"], "num_docs": payload["num_docs"]}
    except Exception as e:                                             # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "state": payload["state"],
                "hits": []}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                                                  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        args = urllib.parse.parse_qs(parts.query)
        if path in ("/health", "/"):
            self._send(200, snapshot()[0])
        elif path == "/ready":
            snap = snapshot()[0]
            self._send(200 if snap["available"] else 503, snap)
        elif path == "/search":
            q = (args.get("q") or [""])[0]
            limit = int((args.get("limit") or ["20"])[0])
            if not q:
                self._send(400, {"error": "q is required"})
            else:
                self._send(200, search(q, limit))
        else:
            self._send(404, {"error": "no such endpoint"})

    def log_message(self, *_a):                                        # noqa: A003
        return


if __name__ == "__main__":
    #  The VPC, not localhost: the lexical channel runs on the search worker, not on the shard, and
    #  a port it cannot reach proves nothing. There is no external address on a shard.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
