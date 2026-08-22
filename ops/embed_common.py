"""Machinery shared by every staging embedder: the retry policy, the lease, the vector format.

Factored out of `ops/desc_backfill.py` after it earned each of these the hard way on 2026-08-22:
a hung socket with no client deadline wedged a worker in `futex_wait_queue` while looking alive,
an unjittered backoff had six workers reproduce the 429 that caused it, and `hash()` being
randomised per process handed two workers different lease keys for the same shard so both ran.
Copying that into a second worker would mean the next fix lands in one of the two copies.

Nothing here talks to a specific table. The callers own their schema; this owns the parts that
were bugs.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import zlib

MAX_ATTEMPTS_DEFAULT = 3
CALL_TIMEOUT_MS_DEFAULT = 120000

_local = threading.local()


# --------------------------------------------------------------------------- logging

def log(event, **kv):
    """One structured line per event. Greppable, and parseable without a log shipper."""
    kv["event"] = event
    kv["ts"] = round(time.time(), 3)
    print(json.dumps(kv, sort_keys=True, default=str), flush=True)


# --------------------------------------------------------------------------- text

def clip(s, n):
    s = (s or "").strip()
    return s[:n] if len(s) > n else s


def tok(s):
    return max(1, len(s) // 4)


def vec(e):
    """pgvector literal. Six decimals is what the live corpus and the description backfill both
    store, and matching it is what keeps a re-embedded chunk bit-comparable with a stored one."""
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


# --------------------------------------------------------------------------- retry policy

def retryable(exc):
    """Transport and capacity failures are worth another attempt; a bad request is not.

    Split out so it can be tested without a network."""
    s = str(exc).lower()
    return any(t in s for t in ("429", "resource_exhausted", "quota", "503", "500",
                                "deadline", "timeout", "timed out", "unavailable",
                                "connection reset", "broken pipe"))


def backoff_delay(attempt, base=0.7, cap=20.0, rand=None):
    """Exponential with full jitter. Without jitter every worker that hit the same 429 retries in
    the same millisecond and reproduces the burst that caused it."""
    rand = rand if rand is not None else random.random
    return min(base * (2 ** attempt), cap) * (0.5 + 0.5 * rand())


# --------------------------------------------------------------------------- leases

def lease_key(pass_name, shard):
    """Stable 32 bit key for one (pass, shard). zlib.crc32 rather than hash(), because hash() is
    randomised per process and would hand two workers different keys for the same work."""
    return zlib.crc32(f"{pass_name}:{shard}".encode()) & 0x7FFFFFFF


def take_lease(conn, namespace, pass_name, shard):
    """Session level advisory lock, held for the life of this process on its own connection.

    Two workers on the same (pass, shard) is not a data hazard, the unique indexes see to that,
    but it is duplicate paid embedding calls and two writers fighting over one watermark row. The
    lock is released automatically when the connection drops, so a killed worker never leaves the
    lease stuck.

    `namespace` separates unrelated jobs: two workers over different populations must never be
    able to take each other's lock by accident, which a single shared namespace plus a colliding
    shard number would allow.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s, %s) AS got",
                    (namespace, lease_key(pass_name, shard)))
        row = cur.fetchone()
        return bool(row["got"] if isinstance(row, dict) else row[0])


# --------------------------------------------------------------------------- Vertex

def vertex_client(timeout_ms=CALL_TIMEOUT_MS_DEFAULT, project="nimo-gpt", location="us-central1"):
    """A timeout here is not optional, it is the difference between a retry and a wedge.

    Measured 2026-08-22: run 58597 stopped producing rows at 08:00:29 and sat in
    `futex_wait_queue` with two of its seven threads in `wait_woken` on ESTAB sockets to
    172.217.118.4:443. A retry loop only catches EXCEPTIONS, and a hung socket never raises one,
    so `ThreadPoolExecutor.map` blocked for ever and the whole process stalled while looking
    perfectly alive. Bounding the call turns that silent wedge into an ordinary retry.

    Thread-local because the client is not documented as thread safe and a pool reuses threads.
    """
    key = f"c{timeout_ms}:{project}:{location}"
    if not hasattr(_local, key):
        from google import genai
        from google.genai.types import HttpOptions
        setattr(_local, key, genai.Client(vertexai=True, project=project, location=location,
                                          http_options=HttpOptions(timeout=timeout_ms)))
    return getattr(_local, key)


def embed_texts(texts, model, dim, task_type, max_attempts=MAX_ATTEMPTS_DEFAULT,
                timeout_ms=CALL_TIMEOUT_MS_DEFAULT, client=None):
    """One bounded, retried Vertex call. -> a list of vectors, one per input, in order."""
    from google.genai.types import EmbedContentConfig
    texts = list(texts)
    for attempt in range(max_attempts):
        t0 = time.time()
        try:
            c = client or vertex_client(timeout_ms)
            r = c.models.embed_content(
                model=model, contents=texts,
                config=EmbedContentConfig(output_dimensionality=dim, task_type=task_type))
            out = [list(e.values) for e in r.embeddings]
            log("embed_call", n=len(texts), attempt=attempt, ms=int((time.time() - t0) * 1000),
                ok=True)
            return out
        except Exception as exc:                                     # noqa: BLE001
            ms = int((time.time() - t0) * 1000)
            last = attempt == max_attempts - 1
            log("embed_call", n=len(texts), attempt=attempt, ms=ms, ok=False,
                retryable=retryable(exc), err=str(exc)[:200])
            if last or not retryable(exc):
                raise
            time.sleep(backoff_delay(attempt))
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- connection

def pg_params(env=None):
    """Postgres settings from the environment. There is deliberately no default password: a
    literal in the tree outlives every rotation and is the thing migrate.py already refuses."""
    env = env if env is not None else os.environ
    return dict(host=env.get("PGHOST", "10.128.0.53"),
                port=int(env.get("PGPORT", "5433")),
                dbname=env.get("PGDATABASE", "patents"),
                user=env.get("PGUSER", "patents"),
                password=env.get("PGPASSWORD"))
