"""The query plan must be frozen too, or the external cache can never hit."""
import importlib
import pytest


@pytest.fixture()
def r(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_DIR", str(tmp_path / "replay"))
    import replay as _r
    importlib.reload(_r)
    yield _r
    monkeypatch.delenv("REPLAY_MODE", raising=False)
    importlib.reload(_r)


def test_plan_is_recorded_then_replayed_without_calling_the_llm(r, monkeypatch):
    """Decomposition is an LLM call. Two arms of one experiment would word the aspects slightly
    differently, miss the bulk_search cache (which is keyed on those very queries), and either
    fail the run or quietly fetch a different external world. Then the corpus would not be the
    only thing that differed between arms, which is the whole reason the database was cloned."""
    import external
    monkeypatch.setenv("REPLAY_MODE", r.RECORD)

    calls = []

    def fake_plan_llm(brief, claims_text):
        calls.append((brief, claims_text))
        #  the shape _plan_llm really returns; plan() reads a["blurb"] and a["devices"] directly
        return [{"name": "sealing lip", "problem": "sealing against a rough surface",
                 "keywords": ["sealing lip vacuum cup"], "cpc": ["B25J"],
                 "devices": ["vacuum cup"], "blurb": "a sealing lip that deflects under vacuum"}]

    monkeypatch.setattr(external, "_plan_llm", fake_plan_llm)

    class Spec:
        kind = "brief"
        text = "a vacuum gripper with a deflecting sealing lip"

    out1 = external.plan([Spec()], brief=Spec.text, claims=[{"text": "claim one"}])
    assert len(calls) == 1, "first pass must call the model"
    assert out1["queries"], "expected queries to be built"

    #  second pass, strict replay, and the model is poisoned
    def boom(*a, **k):
        raise AssertionError("replay mode must not call the model again")

    monkeypatch.setattr(external, "_plan_llm", boom)
    monkeypatch.setenv("REPLAY_MODE", r.REPLAY)
    out2 = external.plan([Spec()], brief=Spec.text, claims=[{"text": "claim one"}])
    assert out2["queries"] == out1["queries"], (
        "the same subject must produce byte-identical queries across arms, or the "
        "bulk_search cache cannot hit")


def test_a_plan_miss_in_replay_mode_fails_the_run(r, monkeypatch):
    import external
    monkeypatch.setenv("REPLAY_MODE", r.REPLAY)
    monkeypatch.setattr(external, "_plan_llm", lambda *a, **k: [])

    class Spec:
        kind = "brief"
        text = "a subject never recorded before"

    with pytest.raises(Exception) as e:
        external.plan([Spec()], brief=Spec.text, claims=[])
    assert "REPLAY MISS" in str(e.value)
