"""Phase 1 of the rebuild: the two evidence bars and the permanent evidence store.

Invariants these lock in:
  * a teaches-bar cell can NEVER be better than "partial" and can never anticipate;
  * fabricated/unlocatable quotes still never render — the teaches bar keeps the finding, not
    the quote;
  * the evidence store round-trips a chart and serves it only for the exact same checklist,
    read pool and TTL;
  * a reference charted once is not charted again (the flywheel).
"""
import uuid

import pytest

import deep_analysis
import evidence
import limitations as limmod


def _ref(shown="the vacuum pump draws air from the suction chamber"):
    return {"found": True, "chars": len(shown), "n_claims": 1, "n_paragraphs": 1,
            "truncated": False, "title": "t",
            "passages": [{"label": "para 1", "kind": "paragraph", "text": shown,
                          "coord": {"paragraph": 1}}]}


def test_row_verified_carries_discloses_bar():
    shown = "the vacuum pump draws air from the suction chamber"
    raw = {"verdict": "disclosed", "quote": "vacuum pump draws air", "note": "n",
           "confidence": 0.9}
    row = deep_analysis._row("lim", raw, _ref(shown), shown, "claim")
    assert row["grounding"] == "verified" and row["bar"] == "discloses"
    assert row["verdict"] == "disclosed"


def test_row_teaches_without_quote_survives_on_weaker_bar():
    shown = "the vacuum pump draws air from the suction chamber"
    raw = {"verdict": "disclosed", "quote": "", "teaches": True,
           "note": "figure 3 shows the sealing lip", "confidence": 0.95}
    row = deep_analysis._row("lim", raw, _ref(shown), shown, "claim")
    assert row["bar"] == "teaches" and row["grounding"] == "teaches-unquoted"
    assert row["verdict"] == "partial"          # never better than partial on the weak bar
    assert row["quote"] == ""                    # nothing unverifiable ever renders as a quote
    assert row["confidence"] <= 0.6


def test_row_fabricated_quote_with_teaches_keeps_finding_not_quote():
    shown = "the vacuum pump draws air from the suction chamber"
    raw = {"verdict": "disclosed", "quote": "an invented passage", "teaches": True,
           "note": "spread across the description", "confidence": 0.8}
    row = deep_analysis._row("lim", raw, _ref(shown), shown, "claim")
    assert row["bar"] == "teaches" and row["quote"] == ""


def test_row_without_teaches_flag_behaves_exactly_as_before():
    shown = "the vacuum pump draws air from the suction chamber"
    row = deep_analysis._row("lim", {"verdict": "disclosed", "quote": "not in the text"},
                             _ref(shown), shown, "claim")
    assert row["verdict"] == "absent" and row["grounding"] == "dropped-ungrounded-quote"


def test_teaches_cells_enter_ledger_but_never_anticipate():
    lims = [{"id": "1a", "claim_label": "claim 1", "text": "a vacuum pump",
             "independent": True, "claim_no": 1, "index": 0}]
    led = limmod.Ledger(lims, cover_min=2)
    chart = {"method": "llm", "pub": "US-X-A1", "claims": [
        {"item": "1a", "verdict": "partial", "quote": "", "location": "",
         "grounding": "teaches-unquoted", "bar": "teaches", "confidence": 0.5}]}
    chart2 = {"method": "llm", "pub": "US-Y-A1", "claims": [
        {"item": "1a", "verdict": "partial", "quote": "", "location": "",
         "grounding": "teaches-unquoted", "bar": "teaches", "confidence": 0.5}]}
    assert led.ingest_charts([chart, chart2]) == 2
    assert led.status("1a") == "partial"                      # evidence, not coverage
    st, ants = led.claim_status("claim 1")
    assert st != "anticipated" and not ants                   # the strong bar stays verbatim-only

    verbatim = {"method": "llm", "pub": "US-Z-A1", "claims": [
        {"item": "1a", "verdict": "disclosed", "quote": "a vacuum pump", "location": "para 1",
         "grounding": "verified", "bar": "discloses", "confidence": 0.9}]}
    led.ingest_charts([verbatim])
    st, ants = led.claim_status("claim 1")
    assert st == "anticipated" and ants == ["US-Z-A1"]


def test_evidence_chart_roundtrip_and_gates():
    evidence.ensure_schema()
    pub = f"US-TEST{uuid.uuid4().hex[:8]}-A1"
    fp = evidence.subject_fp(["feat one"], [{"label": "claim 1", "text": "a pump"}])
    chart = {"pub": pub, "method": "llm", "features": [], "claims": [], "counts": {}}
    try:
        evidence.save_chart(pub, fp, chart, run_slug="test")
        got = evidence.load_chart(pub, fp)
        assert got and got["pub"] == pub
        #  A different checklist is a different question: no hit.
        other = evidence.subject_fp(["feat one"], [{"label": "claim 1", "text": "a DIFFERENT"}])
        assert evidence.load_chart(pub, other) is None
        #  A different read pool is different evidence: no hit.
        import db
        with db.cursor() as cur:
            cur.execute("UPDATE evidence_charts SET read_pool='someone-else' "
                        "WHERE publication_number=%s", (pub,))
        assert evidence.load_chart(pub, fp) is None
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM evidence_charts WHERE publication_number=%s", (pub,))


def test_evidence_cells_roundtrip_and_known_disclosers():
    evidence.ensure_schema()
    pub = f"US-TEST{uuid.uuid4().hex[:8]}-A1"
    lim_text = f"a peripheral sealing lip {uuid.uuid4().hex[:6]}"
    chart = {"pub": pub, "method": "llm", "claims": [
        {"item": "claim 2", "verdict": "disclosed", "quote": "sealing lip", "location": "para 3",
         "grounding": "verified", "bar": "discloses", "confidence": 0.8}]}
    try:
        n = evidence.save_cells_from_chart(chart, {"claim 2": lim_text}, run_slug="test")
        assert n == 1
        got = evidence.known_disclosers(lim_text)
        assert got and got[0]["publication_number"] == pub and got[0]["bar"] == "discloses"
        #  Same normalized text, different casing/whitespace -> same fingerprint.
        assert evidence.known_disclosers("  " + lim_text.upper() + "  ")
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM evidence_cells WHERE publication_number=%s", (pub,))


def test_analyse_reference_reuses_stored_chart(monkeypatch):
    evidence.ensure_schema()
    pub = f"US-TEST{uuid.uuid4().hex[:8]}-A1"
    shown = "a suction plate with a peripheral sealing lip surrounds the chamber"
    calls = []

    monkeypatch.setattr(deep_analysis, "full_text", lambda p, max_chars=None: _ref(shown))
    import llm

    def fake_chat(system, user, **k):
        calls.append(1)
        return {"features": [], "claims": [{"item": "claim 1", "verdict": "disclosed",
                                            "quote": "peripheral sealing lip",
                                            "note": "", "confidence": 0.9}],
                "overall": {"score": 80, "why": "w"}}

    monkeypatch.setattr(llm, "chat_json", fake_chat)
    monkeypatch.setattr(deep_analysis, "_refute", lambda rows, pub, texts=None: 0)
    claims = [{"label": "claim 1", "text": "a peripheral sealing lip"}]
    try:
        first = deep_analysis.analyse_reference(pub, ["a sealing lip"], claims)
        assert first["method"] == "llm" and not first.get("cached")
        n_first = len(calls)
        assert n_first > 0
        second = deep_analysis.analyse_reference(pub, ["a sealing lip"], claims)
        assert second.get("cached") and second["method"] == "llm"
        assert len(calls) == n_first, "cached chart must cost zero model calls"
        #  A different checklist misses on purpose.
        deep_analysis.analyse_reference(pub, ["something else entirely"], claims)
        assert len(calls) > n_first
    finally:
        import db
        with db.cursor() as cur:
            cur.execute("DELETE FROM evidence_charts WHERE publication_number=%s", (pub,))
            cur.execute("DELETE FROM evidence_cells WHERE publication_number=%s", (pub,))
