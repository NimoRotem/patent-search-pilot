"""Phase 0 of the rebuild: segment-aware prompts (Anthropic cache_control) + the run queue.

The cache tests assert the ONE invariant everything rests on: joined segments are byte-identical
to the single string the caller used to send, so Vertex behaviour cannot change. The queue tests
run against the real Postgres like the rest of the suite, on throwaway slugs.
"""
import json
import time
import uuid

import model_pool


# ---------------------------------------------------------------------------- segments


def test_segments_join_is_identity_for_strings():
    assert model_pool._joined("plain text") == "plain text"
    assert model_pool._segments("x") == [{"text": "x"}]


def test_segments_join_reforms_exact_payload():
    #  The exact split deep_analysis._ask performs: dict-minus-closing-brace + ", " + rest.
    shown = 'a "document" with\nnewlines and unicode ü'
    whole = {"reference": "US-1-A", "reference_text": shown,
             "subject_features": ["f1"], "subject_claims": []}
    doc_prefix = json.dumps({"reference": "US-1-A", "reference_text": shown},
                            ensure_ascii=False)[:-1]
    tail = ", " + json.dumps({"subject_features": ["f1"], "subject_claims": []},
                             ensure_ascii=False)[1:]
    segs = [{"text": doc_prefix, "cache": True}, {"text": tail}]
    assert model_pool._joined(segs) == json.dumps(whole, ensure_ascii=False)


def test_anthropic_payload_marks_cache_breakpoints(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers):
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "{}"}],
                "usage": {"input_tokens": 10, "output_tokens": 2,
                          "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}}

    monkeypatch.setattr(model_pool, "_post", fake_post)
    go = model_pool._anthropic("claude-test")
    text, pt, ct = go("SYS", [{"text": "DOC", "cache": True}, {"text": "TAIL"}], 2000)
    p = captured["payload"]
    #  System prompt always carries a breakpoint; the flagged user segment carries one; the
    #  volatile tail does not.
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    blocks = p["messages"][0]["content"]
    assert blocks[0]["text"] == "DOC" and blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "TAIL" and "cache_control" not in blocks[1]
    assert p["temperature"] == 0.2 and "thinking" not in p
    #  Reported prompt tokens include cache reads+writes so spend stays comparable with Vertex.
    assert pt == 20 and ct == 2 and text == "{}"


def test_anthropic_sonnet5_omits_sampling_and_disables_thinking(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers):
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

    monkeypatch.setattr(model_pool, "_post", fake_post)
    go = model_pool._anthropic("claude-sonnet-5", temperature=None, thinking_off=True)
    go("SYS", "u", 1000)
    p = captured["payload"]
    assert "temperature" not in p
    assert p["thinking"] == {"type": "disabled"}


def test_anthropic_caps_user_breakpoints_at_three(monkeypatch):
    captured = {}
    monkeypatch.setattr(model_pool, "_post",
                        lambda url, payload, headers: (captured.update(payload=payload)
                                                       or {"content": [{"type": "text",
                                                                        "text": "x"}],
                                                           "usage": {}}))
    segs = [{"text": f"s{i}", "cache": True} for i in range(5)]
    model_pool._anthropic("m")("SYS", segs, 1000)
    marked = [b for b in captured["payload"]["messages"][0]["content"] if "cache_control" in b]
    assert len(marked) == 3


# ---------------------------------------------------------------------------- run queue


def test_run_queue_lifecycle():
    import run_queue
    run_queue.ensure_schema()
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    try:
        pos = run_queue.enqueue(slug, {"query": "q", "mode": "novelty"})
        assert pos >= 1
        #  Idempotent: re-enqueueing an already-queued slug keeps one row and its place.
        assert run_queue.enqueue(slug, {"query": "q2", "mode": "novelty"}) >= 1
        row = None
        #  It should be findable among queued rows (other queued rows may exist ahead of it).
        import db
        with db.cursor() as cur:
            cur.execute("SELECT state, payload FROM app_run_queue WHERE slug=%s", (slug,))
            row = cur.fetchone()
        assert row and row["state"] == "queued"
        assert (row["payload"] if isinstance(row["payload"], dict)
                else json.loads(row["payload"]))["query"] == "q2"

        run_queue.mark(slug, "running")
        #  mark_finished settles only rows that are actually running.
        run_queue.mark_finished(slug, ok=True)
        with db.cursor() as cur:
            cur.execute("SELECT state FROM app_run_queue WHERE slug=%s", (slug,))
            assert cur.fetchone()["state"] == "done"
        #  A fresh ask for a finished slug re-queues it.
        run_queue.enqueue(slug, {"query": "q3"})
        with db.cursor() as cur:
            cur.execute("SELECT state FROM app_run_queue WHERE slug=%s", (slug,))
            assert cur.fetchone()["state"] == "queued"
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug=%s", (slug,))


def test_requeue_orphans_settles_and_requeues():
    import db
    import run_queue
    run_queue.ensure_schema()
    finished = f"testq-{uuid.uuid4().hex[:12]}"
    dead = f"testq-{uuid.uuid4().hex[:12]}"
    dropped = []
    try:
        for s in (finished, dead):
            run_queue.enqueue(s, {"query": "q"})
            run_queue.mark(s, "running")
        done, requeued = run_queue.requeue_orphans(
            report_finished=lambda s: s == finished,
            drop_partial=lambda s: dropped.append(s))
        assert done >= 1 and requeued >= 1
        with db.cursor() as cur:
            cur.execute("SELECT slug, state FROM app_run_queue WHERE slug IN (%s,%s)",
                        (finished, dead))
            states = {r["slug"]: r["state"] for r in cur.fetchall()}
        assert states[finished] == "done"
        assert states[dead] == "queued"
        assert dead in dropped and finished not in dropped
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug IN (%s,%s)", (finished, dead))


def test_ensure_report_queues_when_gate_full(monkeypatch):
    import webapp
    import run_queue
    import auth
    run_queue.ensure_schema()
    slug = f"testq-{uuid.uuid4().hex[:12]}"

    class FullGate:
        def try_begin(self, depth="deep"):
            return False, "full"

        def end(self, depth="deep"):
            pass

    monkeypatch.setattr(auth, "run_gate", FullGate())
    try:
        st, obj = webapp.ensure_report(slug, query="a query", mode="novelty")
        assert st == "running" and obj is None
        job = webapp._JOBS.get(slug)
        assert job and job.get("queued") and "Queued" in job["msg"]
        import db
        with db.cursor() as cur:
            cur.execute("SELECT state FROM app_run_queue WHERE slug=%s", (slug,))
            assert cur.fetchone()["state"] == "queued"
        #  The dispatcher retries while the gate is full and leaves the row queued.
        assert webapp._queue_launch(slug, {"query": "a query"}) == "busy"
    finally:
        webapp._JOBS.pop(slug, None)
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug=%s", (slug,))


def test_queued_run_starts_when_gate_frees(monkeypatch):
    import webapp
    import run_queue
    import auth
    run_queue.ensure_schema()
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    ran = []

    class OpenGate:
        def try_begin(self, depth="deep"):
            return True, ""

        def end(self, depth="deep"):
            pass

    monkeypatch.setattr(auth, "run_gate", OpenGate())
    monkeypatch.setattr(webapp, "_generate",
                        lambda *a, **k: ran.append(a[0] if a else k.get("slug")))
    try:
        run_queue.enqueue(slug, {"query": "a query", "mode": "novelty"})
        with webapp._JOB_LOCK:
            webapp._JOBS[slug] = {"status": "running", "queued": True, "msg": "Queued…",
                                  "t0": time.time(), "tok0": 0}
        assert webapp._queue_launch(slug, {"query": "a query", "mode": "novelty"}) == "started"
        for _ in range(100):                      # the launch runs _generate on a thread
            if ran:
                break
            time.sleep(0.05)
        assert ran, "queued run never started after the gate freed"
    finally:
        webapp._JOBS.pop(slug, None)
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug=%s", (slug,))
