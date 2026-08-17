"""The batched reader is held to the same bar as the per-document one, and to one extra.

Measured before it was built (offline A/B, adhoc-5972e6042dfa, same documents and requirements):
83% recall against per-document reading, 0 limitations losing their evidence, 40/40 covered at the
ledger's bar against 37/40, and 1 quote in 200 calls credited to a document that was not in its
batch. That last one is the failure mode unique to this shape, so it has a test.
"""
import batch_reader as BR
import deep_analysis


DOC_A = ("A vacuum gripper comprising a rigid base element and a bracing structure that protrudes "
         "from the second side of the base element. " * 6)
DOC_B = ("A handling system with a manipulator for moving the gripper between a pick-up area and a "
         "deposit area, controlled by a data processing device. " * 6)

ITEMS = [{"label": "claim 1[a]", "text": "a rigid base element"},
         {"label": "claim 1[b]", "text": "a manipulator for moving the vacuum gripper"}]


def _fake_loaded(monkeypatch, docs):
    def fake_full_text(pub, max_chars=None):
        text = docs[pub]
        return {"found": True, "pub": pub, "title": "", "chars": len(text),
                "n_claims": 0, "n_paragraphs": 1, "truncated": False,
                "passages": [{"kind": "paragraph", "coord": {"para": 1},
                              "label": "paragraph 1", "text": text}]}
    monkeypatch.setattr(deep_analysis, "full_text", fake_full_text)


def test_batches_are_sized_by_characters_not_by_count():
    """A fixed document count is 156k tokens at the mean and 550k at the max. Budget by chars."""
    loaded = {"p1": (None, "x" * 400_000), "p2": (None, "y" * 400_000), "p3": (None, "z" * 10)}
    b = BR._batches(["p1", "p2", "p3"], loaded, budget=600_000)
    assert [len(x) for x in b] == [1, 2], b
    assert b[0] == ["p1"] and b[1] == ["p2", "p3"]


def test_a_single_oversized_document_still_gets_its_own_batch():
    loaded = {"big": (None, "x" * 900_000)}
    assert BR._batches(["big"], loaded, budget=600_000) == [["big"]]


def test_a_quote_credited_to_a_document_not_in_the_batch_is_rejected(monkeypatch):
    """The failure mode unique to this shape: 1 in 200 calls, measured."""
    _fake_loaded(monkeypatch, {"US-A": DOC_A})
    monkeypatch.setattr(BR.llm, "chat_json", lambda *a, **k: {"references": [
        {"pub": "US-NOT-IN-BATCH", "verdict": "disclosed",
         "quote": "a rigid base element", "confidence": 0.9}]})
    out = BR.read(["US-A"], ITEMS[:1], workers=1)
    assert out["US-A"]["claim 1[a]"]["verdict"] == "absent"


def test_an_invented_quote_is_dropped_by_the_same_grounding_gate(monkeypatch):
    _fake_loaded(monkeypatch, {"US-A": DOC_A})
    monkeypatch.setattr(BR.llm, "chat_json", lambda *a, **k: {"references": [
        {"pub": "US-A", "verdict": "disclosed",
         "quote": "a hydraulic accumulator charged by a swashplate pump", "confidence": 1.0}]})
    out = BR.read(["US-A"], ITEMS[:1], workers=1)
    row = out["US-A"]["claim 1[a]"]
    assert row["verdict"] == "absent"
    assert row["grounding"] == "dropped-ungrounded-quote"


def test_a_real_quote_is_kept_and_located(monkeypatch):
    _fake_loaded(monkeypatch, {"US-A": DOC_A})
    monkeypatch.setattr(BR.llm, "chat_json", lambda *a, **k: {"references": [
        {"pub": "US-A", "verdict": "disclosed",
         "quote": "a rigid base element and a bracing structure", "confidence": 0.8}]})
    out = BR.read(["US-A"], ITEMS[:1], workers=1)
    row = out["US-A"]["claim 1[a]"]
    assert row["verdict"] == "disclosed"
    assert row["grounding"] == "verified"
    assert row["location"], "a kept cell must be citable"


def test_every_pair_gets_a_row_even_when_the_model_says_nothing(monkeypatch):
    """A missing key downstream is a hole in the ledger, not an absence."""
    _fake_loaded(monkeypatch, {"US-A": DOC_A, "US-B": DOC_B})
    monkeypatch.setattr(BR.llm, "chat_json", lambda *a, **k: {"references": []})
    out = BR.read(["US-A", "US-B"], ITEMS, workers=1)
    assert set(out) == {"US-A", "US-B"}
    for pub in out:
        assert set(out[pub]) == {"claim 1[a]", "claim 1[b]"}
        assert all(r["verdict"] == "absent" for r in out[pub].values())


def test_the_best_verdict_wins_when_a_pair_is_answered_twice(monkeypatch):
    """Documents appear in one batch per requirement, but a retry or a duplicate row must not
    downgrade a cell that was already grounded."""
    _fake_loaded(monkeypatch, {"US-A": DOC_A})
    answers = iter([
        {"references": [{"pub": "US-A", "verdict": "disclosed",
                         "quote": "a rigid base element and a bracing structure",
                         "confidence": 0.9}]},
        {"references": [{"pub": "US-A", "verdict": "absent", "quote": ""}]},
    ])
    monkeypatch.setattr(BR.llm, "chat_json", lambda *a, **k: next(answers))
    out = BR.read(["US-A"], ITEMS[:1], workers=1)
    assert out["US-A"]["claim 1[a]"]["verdict"] == "disclosed"


def test_documents_lead_the_payload_so_requirements_share_a_cache_prefix(monkeypatch):
    """83% of prompt tokens came from cache in the A/B, and only because of this ordering."""
    _fake_loaded(monkeypatch, {"US-A": DOC_A})
    seen = {}

    def capture(system, user, **kw):
        seen["user"] = user
        return {"references": []}

    monkeypatch.setattr(BR.llm, "chat_json", capture)
    BR.read(["US-A"], ITEMS[:1], workers=1)
    assert seen["user"].index('"references"') < seen["user"].index('"requirement"')
