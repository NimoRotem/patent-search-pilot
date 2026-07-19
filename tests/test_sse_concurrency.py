"""SSE progress streaming + the removal of the global generation lock.

These guard the two serving-layer fixes:
  * /events/<slug> pushes progress instead of the frontend polling /status every 1.5 s;
  * report generations run CONCURRENTLY — the old module-level _GEN_LOCK (which serialized every
    ~3 minute run to protect a ~3 second reranker step) is gone, and the cross-encoder is isolated
    in its own child process instead.
"""
import json
import queue
import threading
import time
import pytest
import auth
import rerank_pool
import webapp

GOLD = "grabo_gripper_novelty"


# ---- helpers --------------------------------------------------------------------------------
def _drain(resp, deadline=10.0):
    """Consume an SSE response in a background thread; return the parsed data frames."""
    out, q = [], queue.Queue()

    def reader():
        try:
            for chunk in resp.response:
                q.put(chunk)
        except Exception:
            pass
        q.put(None)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    buf, end = b"", time.time() + deadline
    while time.time() < end:
        try:
            chunk = q.get(timeout=0.25)
        except queue.Empty:
            continue
        if chunk is None:
            break
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            frame = frame.decode("utf-8", "replace").strip()
            if frame.startswith("data:"):
                out.append(json.loads(frame[5:].strip()))
    return out


# ---- SSE ------------------------------------------------------------------------------------
def test_sse_headers_are_streaming_safe(app_client):
    r = app_client.get(f"/events/{GOLD}")
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    # nginx here already has proxy_buffering off, but any other hop must not buffer us either
    assert r.headers["X-Accel-Buffering"] == "no"
    assert "no-cache" in r.headers["Cache-Control"]
    r.close()


def test_sse_emits_current_state_immediately_for_finished_report(app_client):
    """A client attaching after the run finished must still get a terminal frame, not hang."""
    frames = _drain(app_client.get(f"/events/{GOLD}"))
    assert frames, "expected at least one frame"
    assert frames[0]["ready"] is True and frames[0]["done"] is True


def test_sse_streams_progress_then_terminates(app_client, monkeypatch, tmp_path):
    """Progress published by the generation thread must reach a connected SSE listener in order,
    and the stream must close itself on the terminal event."""
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = "sse-live"
    with webapp._JOB_LOCK:
        webapp._JOBS[slug] = {"status": "running", "msg": "Queued…"}

    resp = app_client.get(f"/events/{slug}")
    got = []
    done = threading.Event()

    def reader():
        got.extend(_drain(resp, deadline=8.0))
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.4)                                   # let the listener subscribe
    webapp._set_job(slug, kind="elements", msg="Decomposed into 5 elements…")
    time.sleep(0.2)
    (tmp_path / f"{slug}.json").write_text("{}")      # the report now exists on disk
    webapp._set_job(slug, kind="done", status="done", msg="done")
    done.wait(timeout=9)

    msgs = [f["msg"] for f in got]
    assert any("Decomposed into 5 elements" in m for m in msgs), msgs
    assert got[-1]["status"] == "done" and got[-1]["done"] is True
    webapp._JOBS.pop(slug, None)


def test_sse_and_status_report_the_same_state(app_client, monkeypatch, tmp_path):
    """The SSE payload and the polling fallback must never disagree — they share _job_event."""
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    slug = "agree"
    (tmp_path / f"{slug}.json").write_text("{}")
    with webapp._JOB_LOCK:
        webapp._JOBS[slug] = {"status": "partial", "msg": "refining…"}
    poll = app_client.get(f"/status/{slug}").get_json()
    frames = _drain(app_client.get(f"/events/{slug}"))
    for k in ("ready", "done", "status", "msg"):
        assert frames[0][k] == poll[k]
    webapp._JOBS.pop(slug, None)


def test_slow_listener_never_blocks_the_generator():
    """A stalled browser must not be able to wedge a generation thread: queues are bounded and
    drop the oldest frame."""
    slug = "slowsub"
    q = webapp._subscribe(slug)
    try:
        for i in range(500):                       # far beyond the queue's maxsize
            webapp._publish(slug, {"n": i})
        assert q.qsize() <= 64
        assert not q.empty()
    finally:
        webapp._unsubscribe(slug, q)
    assert slug not in webapp._SUBS             # cleaned up on disconnect


# ---- the global lock is gone -----------------------------------------------------------------
def test_gen_lock_no_longer_exists():
    assert not hasattr(webapp, "_GEN_LOCK"), \
        "_GEN_LOCK is back — report generation is serialized again"


def test_two_generations_run_concurrently(monkeypatch, tmp_path):
    """DEFINITIVE proof that two searches overlap.

    Both fake agent runs must meet at a Barrier. If anything still serialized generation, the
    second run could not start until the first returned, the first would wait at the barrier
    forever, and this would raise BrokenBarrierError.
    """
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(auth, "run_gate", auth.RunGate(max_concurrent=2, daily_cap=100,
                                                       state_path=tmp_path / "b.json"))
    barrier = threading.Barrier(2, timeout=10)
    overlapped = []

    class FakeAgent:
        def __init__(self, r):
            pass

        def run(self, query, subject=None, mode=None, cfg=None, on_event=None):
            if on_event:
                on_event("elements", {"n": 3})
            barrier.wait()                 # <- only passes if BOTH runs are in flight at once
            overlapped.append(query)
            return {"query": query, "elements": [], "ranked_families": [], "cards": []}

    monkeypatch.setattr(webapp, "CoverageAgent", FakeAgent)
    monkeypatch.setattr(webapp, "retriever", lambda: None)

    for slug, q in (("conc-a", "query a"), ("conc-b", "query b")):
        st, _ = webapp.ensure_report(slug, query=q, mode="novelty")
        assert st == "running"

    deadline = time.time() + 12
    while time.time() < deadline and len(overlapped) < 2:
        time.sleep(0.1)
    assert len(overlapped) == 2, "generations did not overlap — something is serializing them"

    for slug in ("conc-a", "conc-b"):
        while webapp._JOBS.get(slug, {}).get("status") == "running":
            time.sleep(0.05)
        assert webapp._JOBS[slug]["status"] == "done"
        webapp._JOBS.pop(slug, None)
    assert auth.run_gate.stats()["active"] == 0      # slots released


def test_concurrency_cap_is_enforced_and_released(monkeypatch, tmp_path):
    """Concurrency is now bounded by an explicit budget rather than a mutex."""
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(auth, "run_gate", auth.RunGate(max_concurrent=1, daily_cap=100,
                                                       state_path=tmp_path / "b.json"))
    release = threading.Event()

    class SlowAgent:
        def __init__(self, r):
            pass

        def run(self, query, subject=None, mode=None, cfg=None, on_event=None):
            release.wait(timeout=10)
            return {"query": query, "elements": [], "ranked_families": []}

    monkeypatch.setattr(webapp, "CoverageAgent", SlowAgent)
    monkeypatch.setattr(webapp, "retriever", lambda: None)

    assert webapp.ensure_report("cap-a", query="a", mode="novelty")[0] == "running"
    time.sleep(0.3)
    st, why = webapp.ensure_report("cap-b", query="b", mode="novelty")
    assert st == "busy" and "already running" in why
    assert "cap-b" not in webapp._JOBS          # the rejected claim was released, not stuck
    release.set()
    deadline = time.time() + 12
    while time.time() < deadline and webapp._JOBS.get("cap-a", {}).get("status") == "running":
        time.sleep(0.05)
    assert auth.run_gate.stats()["active"] == 0
    webapp._JOBS.pop("cap-a", None)


# ---- reranker isolation ----------------------------------------------------------------------
def test_reranker_is_routed_through_the_child_process():
    import rerank
    assert getattr(rerank, "_pool_installed", False) is True
    assert rerank.rerank is rerank_pool.rerank
    assert callable(rerank._inprocess_rerank)


def test_rerank_falls_back_to_identity_when_child_is_broken(monkeypatch):
    """The graceful-degradation contract: a dead reranker must never crash a report."""
    def boom(*a, **k):
        raise RuntimeError("child is gone")
    monkeypatch.setattr(rerank_pool, "_get_pool_locked", boom)
    out = rerank_pool.rerank("q", ["a", "b", "c"])
    assert out == [(0, 0.0), (1, 0.0), (2, 0.0)]        # identity order, no exception


def test_rerank_empty_input_is_noop():
    assert rerank_pool.rerank("q", []) == []
