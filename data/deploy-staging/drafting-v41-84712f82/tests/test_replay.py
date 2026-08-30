"""The replay cache is what makes a control-versus-treatment comparison mean anything.

Every assertion here is anchored on the failure it prevents, not on the happy path.
"""
import json
import os

import pytest


@pytest.fixture()
def replay_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_DIR", str(tmp_path / "replay"))
    import importlib
    import replay as r
    importlib.reload(r)
    yield r
    monkeypatch.delenv("REPLAY_MODE", raising=False)
    importlib.reload(r)


def test_off_by_default_so_production_never_serves_a_recording(replay_dir, monkeypatch):
    r = replay_dir
    monkeypatch.delenv("REPLAY_MODE", raising=False)
    assert r.mode() == r.OFF
    assert r.enabled() is False
    assert r.put("bulk_search", {"q": 1}, {"candidates": []}) == ""
    assert r.get("bulk_search", {"q": 1}) is None


def test_record_then_replay_returns_the_same_bytes(replay_dir, monkeypatch):
    r = replay_dir
    monkeypatch.setenv("REPLAY_MODE", r.RECORD)
    payload = {"queries": [{"q": "vacuum gripper", "source": "pqai"}], "timeout": 75}
    parsed = {"candidates": [{"pub": "US-1234567-A"}], "stats": {"pqai": 1}}
    p = r.put("bulk_search", payload, parsed, raw='{"candidates":[{"pub":"US-1234567-A"}]}')
    assert os.path.exists(p)
    monkeypatch.setenv("REPLAY_MODE", r.REPLAY)
    assert r.get("bulk_search", payload) == parsed
    #  the raw body is kept too, so a parser change can be re-tested without a fresh fetch
    rec = json.load(open(p))
    assert rec["raw"].startswith('{"candidates"')
    assert rec["adapter_version"] and rec["normalization_version"]


def test_a_miss_in_replay_mode_is_a_run_failure_not_a_live_call(replay_dir, monkeypatch):
    """The whole point. A silent live call here would let the treatment arm see a different
    outside world from the control arm while the comparison claims the corpus was the only
    thing that changed."""
    r = replay_dir
    monkeypatch.setenv("REPLAY_MODE", r.REPLAY)
    with pytest.raises(Exception) as e:
        r.miss("bulk_search", {"queries": [], "timeout": 1})
    assert "REPLAY MISS" in str(e.value)


def test_the_key_covers_the_whole_request_and_the_versions(replay_dir, monkeypatch):
    """A cache that ignored part of the request would serve one query's answer for another."""
    r = replay_dir
    monkeypatch.setenv("REPLAY_MODE", r.RECORD)
    a = {"queries": [{"q": "vacuum gripper"}], "timeout": 75}
    b = {"queries": [{"q": "suction cup"}], "timeout": 75}
    c = {"queries": [{"q": "vacuum gripper"}], "timeout": 30}
    assert r.key("bulk_search", a) != r.key("bulk_search", b), "query text must be in the key"
    assert r.key("bulk_search", a) != r.key("bulk_search", c), "timeout must be in the key"
    assert r.key("bulk_search", a) != r.key("federation", a), "namespace must be in the key"
    #  and a version bump must invalidate, or new code silently serves old results
    before = r.key("bulk_search", a)
    monkeypatch.setattr(r, "ADAPTER_VERSION", "9999-99-99.9")
    assert r.key("bulk_search", a) != before


def test_external_bulk_serves_the_recording_without_touching_the_network(replay_dir, monkeypatch):
    """End to end through the real call site, with the network poisoned."""
    r = replay_dir
    import external
    monkeypatch.setattr(external, "ENABLED", True)
    monkeypatch.setattr(external, "MAX_QUERIES", 10)
    queries = [{"q": "vacuum lifter", "source": "pqai"}]
    body = {"queries": queries, "timeout": 75.0}
    monkeypatch.setenv("REPLAY_MODE", r.RECORD)
    r.put("bulk_search", body, {"ok": True, "candidates": [{"pub": "US-7182148-B1"}],
                                "stats": {}, "errors": {}})

    import requests

    def _boom(*a, **k):
        raise AssertionError("replay mode must not reach the network")

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setenv("REPLAY_MODE", r.REPLAY)
    out = external.bulk(queries, timeout=75.0)
    assert out["candidates"] == [{"pub": "US-7182148-B1"}]
    assert out.get("replayed") is True
