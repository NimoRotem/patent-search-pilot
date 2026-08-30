"""The public two-tier split: quick vs deep.

Invariants locked in:
  * every pre-existing deep report keeps its slug — depth joins the identity ONLY when quick;
  * the two gate lanes are independent in concurrency and in daily budget;
  * a queued quick run carries its depth through the queue payload to the dispatcher.
"""
import time
import uuid

import auth
import webapp


def test_deep_slug_is_unchanged_and_quick_differs():
    deep_old_shape = webapp.search_slug("q", "novelty", wide=True, search_focus="all_text",
                                        subject=None, doc_token="tok")
    deep_explicit = webapp.search_slug("q", "novelty", wide=True, search_focus="all_text",
                                       subject=None, doc_token="tok", depth="deep")
    quick = webapp.search_slug("q", "novelty", wide=False, search_focus="all_text",
                               subject=None, doc_token="tok", depth="quick")
    assert deep_old_shape == deep_explicit          # backward compatible: old reports resolve
    assert quick != deep_explicit


def test_run_gate_lanes_are_independent(tmp_path):
    g = auth.RunGate(max_concurrent=1, daily_cap=2, state_path=tmp_path / "g.json",
                     quick_max=2, quick_daily_cap=3)
    ok, _ = g.try_begin()
    assert ok
    ok, why = g.try_begin()
    assert not ok and "already running" in why      # deep lane full...
    ok, _ = g.try_begin(depth="quick")              # ...quick lane unaffected
    assert ok
    ok, _ = g.try_begin(depth="quick")
    assert ok
    ok, why = g.try_begin(depth="quick")
    assert not ok and "quick" in why
    g.end(depth="quick")
    ok, _ = g.try_begin(depth="quick")
    assert ok
    st = g.stats()
    assert st["active"] == 1 and st["active_quick"] == 2
    assert st["today"] == 1 and st["quick_today"] == 3


def test_quick_daily_budget_is_its_own_meter(tmp_path):
    g = auth.RunGate(max_concurrent=5, daily_cap=1, state_path=tmp_path / "g.json",
                     quick_max=5, quick_daily_cap=2)
    assert g.try_begin()[0]
    assert not g.try_begin()[0]                     # deep daily cap reached
    assert g.try_begin(depth="quick")[0]            # quick meter untouched by the deep cap
    assert g.try_begin(depth="quick")[0]
    ok, why = g.try_begin(depth="quick")
    assert not ok and "budget" in why
    #  The meters persist together and reload together.
    g2 = auth.RunGate(max_concurrent=5, daily_cap=1, state_path=tmp_path / "g.json",
                      quick_max=5, quick_daily_cap=2)
    assert g2.count == 1 and g2.quick_count == 2


def test_queued_quick_run_keeps_its_depth(monkeypatch):
    import run_queue
    run_queue.ensure_schema()
    slug = f"testq-{uuid.uuid4().hex[:12]}"
    seen = {}

    class FullGate:
        def try_begin(self, depth="deep"):
            return False, "full"

        def end(self, depth="deep"):
            pass

    monkeypatch.setattr(auth, "run_gate", FullGate())
    try:
        st, _ = webapp.ensure_report(slug, query="a query", mode="novelty", depth="quick")
        assert st == "running"
        import db
        with db.cursor() as cur:
            cur.execute("SELECT payload FROM app_run_queue WHERE slug=%s", (slug,))
            payload = cur.fetchone()["payload"]
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        assert payload["depth"] == "quick"

        #  The dispatcher hands the depth on: once the gate opens, _generate receives it.
        class OpenGate:
            def try_begin(self, depth="deep"):
                seen["begin_depth"] = depth
                return True, ""

            def end(self, depth="deep"):
                seen["end_depth"] = depth

        monkeypatch.setattr(auth, "run_gate", OpenGate())
        monkeypatch.setattr(webapp, "_generate",
                            lambda *a, **k: seen.update(gen_depth=k.get("depth", "deep")))
        with webapp._JOB_LOCK:
            webapp._JOBS[slug] = {"status": "running", "queued": True, "msg": "Queued…",
                                  "t0": time.time(), "tok0": 0}
        assert webapp._queue_launch(slug, payload) == "started"
        for _ in range(100):
            if "gen_depth" in seen:
                break
            time.sleep(0.05)
        assert seen.get("begin_depth") == "quick"
        assert seen.get("gen_depth") == "quick"
        assert seen.get("end_depth") == "quick"
    finally:
        webapp._JOBS.pop(slug, None)
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_run_queue WHERE slug=%s", (slug,))


def test_deep_rank_accepts_depth():
    import inspect
    import deep_rank
    assert "depth" in inspect.signature(deep_rank.run).parameters
