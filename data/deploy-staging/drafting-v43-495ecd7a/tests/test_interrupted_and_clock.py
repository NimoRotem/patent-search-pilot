"""An interrupted run must never read as done, and every start must be requeue-able.

The production failure: a run started while the gate had a free slot never entered the queue;
a deploy killed it; requeue_orphans had no row; the page treated the partial file as done and
reloaded onto it forever ("seems to restart over and over"). These lock in the three parts of
the fix: record_started rows for every start, interrupted status for partial-without-job, and
the attempt/overall-clock fields the progress UI renders.
"""
import json
import uuid

import run_queue
import webapp


def _fresh(slug):
    webapp._PARTIAL_CACHE.pop(slug, None)
    webapp._QROW_CACHE.pop(slug, None)


def test_record_started_counts_attempts_and_keeps_the_clock():
    run_queue.ensure_schema()
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    try:
        run_queue.record_started(slug, {"query": "q"})
        r1 = run_queue.get_row(slug)
        assert r1["state"] == "running" and r1["attempts"] == 1
        run_queue.record_started(slug, {"query": "q"})       # a restart: same clock, attempt 2
        r2 = run_queue.get_row(slug)
        assert r2["attempts"] == 2
        assert abs(r2["t0_overall"] - r1["t0_overall"]) < 0.001
        run_queue.mark_finished(slug, ok=True)
        run_queue.record_started(slug, {"query": "q"})       # a NEW run after done: clock resets
        r3 = run_queue.get_row(slug)
        assert r3["state"] == "running" and r3["t0_overall"] >= r2["t0_overall"]
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug=%s", (slug,))


def test_partial_without_job_is_interrupted_not_done(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    (tmp_path / f"{slug}.json").write_text(json.dumps({"partial": True, "query": "q"}))
    _fresh(slug)
    ev = webapp._job_event(slug, {})
    assert ev["done"] is False                       # the reload-loop guarantee
    assert ev["status"] == "interrupted"
    assert "interrupted" in ev["msg"] and "Re-run" in ev["msg"]
    assert ev["ready"] is True                       # the partial page is still renderable


def test_interrupted_with_queue_row_promises_the_restart(tmp_path, monkeypatch):
    run_queue.ensure_schema()
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    (tmp_path / f"{slug}.json").write_text(json.dumps({"partial": True}))
    try:
        run_queue.enqueue(slug, {"query": "q"})
        _fresh(slug)
        ev = webapp._job_event(slug, {})
        assert ev["done"] is False and ev["status"] == "interrupted"
        assert "restarting automatically" in ev["msg"]
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug=%s", (slug,))


def test_finished_file_without_job_is_done(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    (tmp_path / f"{slug}.json").write_text(json.dumps({"partial": False}))
    _fresh(slug)
    ev = webapp._job_event(slug, {})
    assert ev["done"] is True and ev["ready"] is True


def test_event_carries_attempt_and_overall_clock(tmp_path, monkeypatch):
    run_queue.ensure_schema()
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    try:
        run_queue.record_started(slug, {"query": "q"})
        run_queue.record_started(slug, {"query": "q"})
        _fresh(slug)
        ev = webapp._job_event(slug, {"status": "running", "msg": "working", "t0": 0})
        assert ev["attempt"] == 2
        assert isinstance(ev["elapsed_total_sec"], int) and ev["elapsed_total_sec"] >= 0
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug=%s", (slug,))


def test_dispatcher_restarts_an_interrupted_partial(tmp_path, monkeypatch):
    """The final link of the loop: ensure_report served a PARTIAL file as 'ready', so the
    dispatcher marked a re-queued interrupted run done without ever running it."""
    import time as _t
    import auth
    run_queue.ensure_schema()
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    (tmp_path / f"{slug}.json").write_text(json.dumps({"partial": True}))
    ran = []

    class OpenGate:
        def try_begin(self, depth="deep"):
            return True, ""

        def end(self, depth="deep"):
            pass

    monkeypatch.setattr(auth, "run_gate", OpenGate())
    monkeypatch.setattr(webapp, "_generate", lambda *a, **k: ran.append(a[0] if a else k))
    try:
        #  A plain viewer call still renders the partial (no restart, no drop).
        st, rep = webapp.ensure_report(slug)
        assert st == "ready" and rep.get("partial")
        assert (tmp_path / f"{slug}.json").exists()
        #  The dispatcher call restarts it: partial dropped, generation launched.
        run_queue.enqueue(slug, {"query": "q"})
        st, rep = webapp.ensure_report(slug, query="q", mode="novelty",
                                       from_queue=True, restart_partial=True)
        assert st == "running" and rep is None
        assert not (tmp_path / f"{slug}.json").exists()
        for _ in range(100):
            if ran:
                break
            _t.sleep(0.05)
        assert ran, "the interrupted run never restarted"
        row = run_queue.get_row(slug)
        assert row and row["attempts"] >= 1
    finally:
        webapp._JOBS.pop(slug, None)
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug=%s", (slug,))
