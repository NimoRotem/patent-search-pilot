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


def next_queued():
    with db.cursor() as cur:
        cur.execute("SELECT slug, payload FROM app_run_queue WHERE state='queued' "
                    "ORDER BY enqueued_at LIMIT 1")
        r = cur.fetchone()
    if not r:
        return None
    slug = r["slug"] if isinstance(r, dict) else r[0]
    payload = r["payload"] if isinstance(r, dict) else r[1]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {"slug": slug, "payload": payload or {}}


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
                row = next_queued()
                if not row:
                    continue
                res = launch(row["slug"], row["payload"])
                if res == "started":
                    mark(row["slug"], "running")
                elif res == "done":
                    mark(row["slug"], "done")
                elif res == "gone":
                    #  The payload can no longer start a run (e.g. its stashed document expired).
                    mark(row["slug"], "failed", error="could not be started after restart")
                #  'busy': the gate is still full — leave it queued and try again next tick.
            except Exception:
                traceback.print_exc()

    _thread = threading.Thread(target=loop, name="run-queue-dispatch", daemon=True)
    _thread.start()
    return _thread
