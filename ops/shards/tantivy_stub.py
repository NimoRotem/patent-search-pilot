#!/usr/bin/env python3
"""Holds the Tantivy port until workstream C's server takes it, and tells the truth meanwhile.

It reports `available: false` always. That is not a placeholder being lazy: a lexical backend that
is mid build, out of disk, or serving a partial index MUST return False rather than an empty list,
because an empty result set and a genuine miss are indistinguishable to fusion and a miss is
scored as a recall failure (docs/lexical_interface.md). A stub that claimed to be available would
be the single worst thing this file could do.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SHARD_TANTIVY_PORT", "8635"))
SHARD_ID = os.environ.get("SHARD_ID", "")
INDEX_DIR = os.environ.get("SHARD_TANTIVY_INDEX", "/opt/patents-shard/tantivy/index")


def state():
    try:
        n = len([f for f in os.listdir(INDEX_DIR) if not f.startswith(".")])
    except Exception:
        n = 0
    return {"shard": SHARD_ID, "engine": "stub", "state": "absent" if not n else "unmanaged",
            "available": False, "index_dir": INDEX_DIR, "index_files": n,
            "note": "no Tantivy server installed; the lexical channel falls back to Postgres"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):                                   # noqa: N802
        raw = json.dumps(state()).encode()
        self.send_response(200 if self.path.rstrip("/") in ("/health", "") else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_a):                          # noqa: A003
        return


if __name__ == "__main__":
    #  The VPC, not localhost: the lexical channel runs on the search worker, not on the shard,
    #  and a port it cannot reach proves nothing. There is no external address on a shard.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
