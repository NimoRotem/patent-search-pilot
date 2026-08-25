"""A search that arrives while the gate is full WAITS instead of bouncing.

Before this module, `auth.RunGate` rejected the third concurrent search with "try again in a
minute" and a deploy mid-search lost the run entirely (adhoc-ed62f27d3c2a died to the 2026-08-17
22:38 restart with nothing to show). Both are the same defect: run state lived only in the web
process.

The queue is one Postgres table. `ensure_report` enqueues when the gate is full; a dispatcher
thread starts queued runs as slots free; at boot every row still marked `running` belonged to a
dead process and is either settled (finished report on disk) or re-queued. A re-queued run starts
over — stages are idempotent and the retrieval/evidence caches make the second pass cheaper — which
is honest recovery, not checkpoint theatre.

Single-process by design (the app is one gunicorn worker); the table exists so state survives the
process, not to coordinate several of them.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback

import db

POLL_SECONDS = 4.0

_thread = None


def ensure_schema():
    with db.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_run_queue (
                slug        text PRIMARY KEY,
                payload     jsonb NOT NULL,
                state       text NOT NULL DEFAULT 'queued',
                enqueued_at timestamptz NOT NULL DEFAULT now(),
                started_at  timestamptz,
                finished_at timestamptz,
                error       text
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_run_queue_state "
                    "ON app_run_queue (state, enqueued_at)")
        #  How many times this run has actually STARTED. Attempt 2+ means a restart interrupted
        #  it and the queue brought it back; the page says so instead of looking like a loop.
        cur.execute("ALTER TABLE app_run_queue ADD COLUMN IF NOT EXISTS "
                    "attempts int NOT NULL DEFAULT 0")


def enqueue(slug, payload) -> int:
    """Add a run to the queue (idempotent on slug). -> position in line, 1-based."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO app_run_queue (slug, payload, state) VALUES (%s, %s, 'queued') "
                    "ON CONFLICT (slug) DO UPDATE SET "
                    "  payload = EXCLUDED.payload, "
                    #  A re-request for a finished/failed slug is a fresh ask; one already queued
                    #  or running keeps its place and its state.
                    "  state = CASE WHEN app_run_queue.state IN ('done','failed') "
                    "               THEN 'queued' ELSE app_run_queue.state END, "
                    "  enqueued_at = CASE WHEN app_run_queue.state IN ('done','failed') "
                    "               THEN now() ELSE app_run_queue.enqueued_at END",
                    (slug, json.dumps(payload)))
        cur.execute("SELECT count(*) AS n FROM app_run_queue WHERE state='queued' AND "
                    "enqueued_at <= (SELECT enqueued_at FROM app_run_queue WHERE slug=%s)",
                    (slug,))
        r = cur.fetchone()
    return int(r["n"] if isinstance(r, dict) else r[0]) or 1


def record_started(slug, payload):
    """EVERY run start lands here, queued or direct. The first production loss was exactly the
    gap this closes: a run started while the gate had a free slot never entered the queue, a
    deploy killed it, `requeue_orphans` had no row to requeue, and the page looped on the
    partial report forever. The row doubles as the run's overall clock (enqueued_at survives
    restarts) and its attempt counter."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO app_run_queue (slug, payload, state, started_at, attempts) "
                "VALUES (%s, %s, 'running', now(), 1) "
                "ON CONFLICT (slug) DO UPDATE SET "
                "  payload = EXCLUDED.payload, state = 'running', started_at = now(), "
                "  attempts = app_run_queue.attempts + 1, "
                #  A re-run of a slug that finished earlier is a NEW run: its clock restarts.
                "  enqueued_at = CASE WHEN app_run_queue.state IN ('done','failed') "
                "                THEN now() ELSE app_run_queue.enqueued_at END, "
                "  finished_at = NULL, error = NULL",
                (slug, json.dumps(payload)))
    except Exception:
        traceback.print_exc()


def get_row(slug):
    """state / attempts / overall start for one slug, or None. Cheap single-row read."""
    try:
        with db.cursor() as cur:
            cur.execute("SELECT state, attempts, "
                        "extract(epoch FROM enqueued_at) AS t0_overall "
                        "FROM app_run_queue WHERE slug=%s", (slug,))
            r = cur.fetchone()
        return dict(r) if r else None
    except Exception:
        return None


#  How far down the queue the dispatcher may look past a row it cannot start. See
#  `next_queued_batch` for why looking past it at all is the whole point.
LOOKAHEAD = int(os.environ.get("RUN_QUEUE_LOOKAHEAD", "12"))


def _rows(limit):
    with db.cursor() as cur:
        cur.execute("SELECT slug, payload FROM app_run_queue WHERE state='queued' "
                    "ORDER BY enqueued_at LIMIT %s", (int(limit),))
        rows = cur.fetchall() or []
    out = []
    for r in rows:
        slug = r["slug"] if isinstance(r, dict) else r[0]
        payload = r["payload"] if isinstance(r, dict) else r[1]
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append({"slug": slug, "payload": payload or {}})
    return out


def next_queued():
    """The oldest queued run, or None. Kept for callers that only want the head."""
    rows = _rows(1)
    return rows[0] if rows else None


def next_queued_batch(limit=None):
    """The oldest queued runs, in order, so the dispatcher can look PAST one it cannot start.

    THE GATE HAS TWO LANES AND THE QUEUE USED TO UNDO THEM. Quick and deep have separate
    concurrency and separate daily budgets precisely so that a cents-class interactive search never
    waits behind two multi-hour attacks. But the dispatcher only ever fetched the single oldest
    queued row, so a queued DEEP run — one that cannot start because the deep lane is full — was
    retried every tick for ever while every quick run behind it sat untouched with its own lane
    completely free. Measured 2026-08-19: deep lane full, quick lane empty, a quick run queued
    behind one deep run never started at all.

    FIFO still holds WITHIN a lane, because the rows come back in enqueue order and the dispatcher
    tries them in that order. A later quick run overtaking an earlier deep one is not unfairness,
    it is what having two lanes means.
    """
    return _rows(limit or LOOKAHEAD)


def mark(slug, state, error=None):
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE app_run_queue SET state=%s, error=%s, "
                        "started_at = CASE WHEN %s='running' THEN now() ELSE started_at END, "
                        "finished_at = CASE WHEN %s IN ('done','failed') THEN now() "
                        "              ELSE finished_at END WHERE slug=%s",
                        (state, error, state, state, slug))
    except Exception:
        traceback.print_exc()


def mark_finished(slug, ok=True, error=None):
    """Best-effort completion hook — a run started directly (never queued) has no row, and that
    is fine; only rows that exist are settled."""
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE app_run_queue SET state=%s, error=%s, finished_at=now() "
                        "WHERE slug=%s AND state='running'",
                        ("done" if ok else "failed", error, slug))
    except Exception:
        traceback.print_exc()


def requeue_orphans(report_finished, drop_partial):
    """At boot: settle rows a dead process left `running`.

    `report_finished(slug)` -> True when a complete (non-partial) report exists on disk;
    `drop_partial(slug)` removes a half-written report so the re-run starts clean.
    Returns (settled_done, requeued).
    """
    done = requeued = 0
    try:
        with db.cursor() as cur:
            cur.execute("SELECT slug FROM app_run_queue WHERE state='running'")
            rows = [r["slug"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    except Exception:
        traceback.print_exc()
        return 0, 0
    for slug in rows:
        try:
            if report_finished(slug):
                mark(slug, "done")
                done += 1
            else:
                drop_partial(slug)
                mark(slug, "queued")
                requeued += 1
        except Exception:
            traceback.print_exc()
    if done or requeued:
        print(f"[run_queue] boot: {done} orphaned runs were already finished, "
              f"{requeued} re-queued to run again", flush=True)
    return done, requeued


def queued_position(slug):
    try:
        with db.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM app_run_queue WHERE state='queued' AND "
                        "enqueued_at <= (SELECT enqueued_at FROM app_run_queue WHERE slug=%s "
                        "AND state='queued')", (slug,))
            r = cur.fetchone()
        n = int(r["n"] if isinstance(r, dict) else r[0])
        return n or None
    except Exception:
        return None


def start_dispatcher(launch):
    """`launch(slug, payload)` -> 'started' | 'busy' | 'gone'. One daemon thread, started once."""
    global _thread
    if _thread and _thread.is_alive():
        return _thread

    def loop():
        while True:
            time.sleep(POLL_SECONDS)
            try:
                #  Walk the queue rather than staring at its head: a row that is 'busy' is busy in
                #  ITS OWN lane, and the next row may be in a lane that is free. Stop at the first
                #  one that starts, so one tick starts one run and the loop stays predictable.
                for row in next_queued_batch():
                    res = launch(row["slug"], row["payload"])
                    if res == "started":
                        mark(row["slug"], "running")
                        break
                    if res == "done":
                        mark(row["slug"], "done")
                        break
                    if res == "gone":
                        #  The payload can no longer start a run (e.g. its stashed document
                        #  expired).
                        mark(row["slug"], "failed",
                             error="could not be started after restart")
                        break
                    #  'busy': this lane is full. Leave the row queued and try the next one.
            except Exception:
                traceback.print_exc()

    _thread = threading.Thread(target=loop, name="run-queue-dispatch", daemon=True)
    _thread.start()
    return _thread
