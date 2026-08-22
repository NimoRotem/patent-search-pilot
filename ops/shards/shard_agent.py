#!/usr/bin/env python3
"""The shard's own answer to "are you serving?", on :8639. Runs as `postgres`, stdlib only.

WHY THE SHARD ANSWERS AND NOT THE CONTROLLER. The controller can see that an instance is RUNNING.
RUNNING is not serving: the kernel is up ninety seconds before Postgres accepts, and Postgres
accepts long before the index it needs is in memory or, on a first boot after a load, before the
index exists at all. Only the box knows which of those it is in, so only the box may say.

THE THREE HONEST ANSWERS.

    waking   the instance is up and something between here and serving is not finished:
             Postgres is not accepting, or shard_status says the corpus is still being built,
             or the blocking half of prewarm has not returned.
    hot      Postgres accepts, shard_status says `ready`, and prewarm's blocking phase is done.
    (cold)   never said from here. A cold shard has no process to ask; the controller infers it
             from a TERMINATED instance plus an unreachable agent.

`available` is `state == "hot"`, and it is deliberately False for a shard that is up and empty.
An empty result set and a genuine miss are indistinguishable to fusion and a miss scores as a
recall failure, so a shard that cannot answer must say so rather than answer nothing.

Endpoints:
    GET /health    the whole picture, always 200
    GET /ready     200 when hot, 503 otherwise, for anything that wants a plain check
    GET /activity  the in flight query count on its own, which is what the idle reaper asks
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHARD_ID = os.environ.get("SHARD_ID", "")
PGPORT = os.environ.get("SHARD_PGPORT", "5432")
PGDATABASE = os.environ.get("SHARD_PGDATABASE", "patents")
AGENT_PORT = int(os.environ.get("SHARD_AGENT_PORT", "8639"))
TANTIVY_PORT = int(os.environ.get("SHARD_TANTIVY_PORT", "8635"))
PREWARM_STATE = os.environ.get("SHARD_PREWARM_STATE", "/run/patents-shard/prewarm.json")
#  A probe is not free and the manager polls this once a second during a wake. Half a second of
#  cache is invisible against a wake measured in tens of seconds and keeps the poll off the
#  database.
CACHE_SECONDS = float(os.environ.get("SHARD_AGENT_CACHE_SECONDS", "0.5"))
BOOTED_AT = time.time()

_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "payload": None}


def _psql(sql, timeout=3.0):
    """One value out of the shard database, or None. Peer auth on the unix socket: the agent runs
    as `postgres`, so there is no password on this path and no secret in this process."""
    try:
        out = subprocess.run(
            ["psql", "-p", str(PGPORT), "-d", PGDATABASE, "-tAX", "-c", sql],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PGCONNECT_TIMEOUT": "2"})
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def _pg_accepting():
    try:
        out = subprocess.run(["pg_isready", "-p", str(PGPORT), "-d", PGDATABASE],
                             capture_output=True, text=True, timeout=3.0)
        return out.returncode == 0
    except Exception:
        return False


def _prewarm():
    try:
        with open(PREWARM_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {"state": "unknown"}


def _tantivy():
    """Workstream C owns what Tantivy indexes. This owns that a process is listening and that its
    own opinion of itself is passed through unedited."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{TANTIVY_PORT}/health", timeout=2.0) as r:
            body = json.loads(r.read().decode("utf-8", "replace") or "{}")
        body.setdefault("state", "running")
        body.setdefault("available", False)
        body["listening"] = True
        return body
    except Exception as e:
        return {"state": "down", "available": False, "listening": False, "note": str(e)[:120]}


def _active_queries():
    n = _psql("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
              "AND pid <> pg_backend_pid() AND state <> 'idle'")
    try:
        return int(n)
    except Exception:
        return None


def snapshot():
    with _LOCK:
        if _CACHE["payload"] is not None and (time.time() - _CACHE["at"]) < CACHE_SECONDS:
            return _CACHE["payload"]

    accepting = _pg_accepting()
    index = {"state": "unknown", "generation": "", "n_chunks": 0}
    active = None
    if accepting:
        row = _psql("SELECT state, generation, n_chunks "
                    f"FROM shard_status WHERE shard = '{SHARD_ID}'")
        if row:
            parts = row.split("|")
            index = {"state": parts[0],
                     "generation": parts[1] if len(parts) > 1 else "",
                     "n_chunks": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0}
        elif row == "":
            index = {"state": "absent", "generation": "", "n_chunks": 0}
        active = _active_queries()

    pw = _prewarm()
    reason = ""
    if not accepting:
        state, reason = "waking", "postgres is not accepting connections"
    elif index["state"] != "ready":
        state = "waking"
        reason = f"index state is {index['state']!r}, not 'ready'"
    elif pw.get("state") not in ("done", "background", "skipped"):
        state = "waking"
        reason = f"prewarm is {pw.get('state')!r}"
    else:
        state, reason = "hot", ""

    payload = {
        "shard": SHARD_ID,
        "state": state,
        "available": state == "hot",
        "reason": reason,
        "postgres": {"accepting": accepting, "port": int(PGPORT), "database": PGDATABASE,
                     "active_queries": active},
        "index": index,
        "prewarm": pw,
        "tantivy": _tantivy(),
        "uptime_s": round(time.time() - BOOTED_AT, 1),
        "at": time.time(),
    }
    with _LOCK:
        _CACHE["at"], _CACHE["payload"] = time.time(), payload
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                                   # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/health", "/"):
            self._send(200, snapshot())
        elif path == "/ready":
            snap = snapshot()
            self._send(200 if snap["available"] else 503, snap)
        elif path == "/activity":
            n = _active_queries()
            self._send(200, {"shard": SHARD_ID, "active_queries": n})
        else:
            self._send(404, {"error": "no such endpoint"})

    def log_message(self, *_a):                          # noqa: A003
        return                                           # a health poll per second is not news


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), Handler).serve_forever()
