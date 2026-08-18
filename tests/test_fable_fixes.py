"""The three regressions the first rebuilt reports shipped, locked in as tests.

Reported on the production Schmalz run (adhoc-83fc52c30b62) before the revert:
  1. German verbatim quotes in the claim chart (47 cells) — the batch tail read DE-language
     corpus text the deep path would have English-enriched first;
  2. three sibling DE filings as three near-identical grid columns — the tail did not dedupe
     by family;
  3. "disclosed by 280 of 367" — teaches-bar cells were counted alongside verbatim ones.
"""
import batch_reader
import webview


GERMAN = ("Vakuum-Handverlegegerät zum Verlegen von vorzugsweise plattenförmigem Verlegegut "
          "mit zumindest einem Sauggreifer und einer Vakuumpumpe, dadurch gekennzeichnet, "
          "dass die Vorrichtung eine Dichtung und eine Platte mit einem Griff umfasst und "
          "das Gerät zum Verlegen der Platten dient und mit der Pumpe verbunden ist")
ENGLISH = ("A vacuum gripping unit for handling plate-shaped goods, the unit comprising a "
           "suction plate with a peripheral seal and a pump that is connected to the chamber "
           "for the purpose of evacuating the air in the chamber of the device")
FRENCH = ("Dispositif de préhension par le vide pour la manutention selon la revendication, "
          "dans lequel une plaque est reliée à une pompe pour les charges dans une chambre")


def test_mostly_english_heuristic():
    assert batch_reader._mostly_english(ENGLISH)
    assert not batch_reader._mostly_english(GERMAN)
    assert not batch_reader._mostly_english(FRENCH)
    assert batch_reader._mostly_english("")            # empty text is not "foreign"


def test_load_skips_non_english_documents(monkeypatch):
    import deep_analysis
    texts = {"US-1-A": ENGLISH, "DE-1-U1": GERMAN}

    def fake_full_text(pub, max_chars=None):
        return {"found": True, "chars": 10, "n_claims": 1, "n_paragraphs": 0,
                "truncated": False,
                "passages": [{"label": "claim 1", "kind": "claim", "text": texts[pub],
                              "coord": {"claim_no": 1}}]}

    monkeypatch.setattr(deep_analysis, "full_text", fake_full_text)
    monkeypatch.setattr(deep_analysis, "_rendered", lambda ref: ref["passages"][0]["text"])
    logs = []
    out = batch_reader._load(["US-1-A", "DE-1-U1"], log=logs.append)
    assert "US-1-A" in out and "DE-1-U1" not in out
    assert any("non-English" in l for l in logs)


def test_quote_language_guard_demotes_german_verbatim():
    import deep_analysis
    shown = ("ABSTRACT An apparatus for handling plates. CLAIMS Schr\u00f6pfvorrichtung mit einem "
             "mit einer Saugglocke verbundenen energetisch angetriebenen Sauger und einer Pumpe")
    ref = {"found": True, "chars": len(shown), "n_claims": 1, "n_paragraphs": 0,
           "truncated": False,
           "passages": [{"label": "claim 1", "kind": "claim", "text": shown,
                         "coord": {"claim_no": 1}}]}
    raw = {"verdict": "disclosed",
           "quote": "Schr\u00f6pfvorrichtung mit einem mit einer Saugglocke verbundenen",
           "note": "the suction bell", "confidence": 0.9}
    row = deep_analysis._row("lim", raw, ref, shown, "claim")
    #  Grounded and located, but unreadable on an English report: kept on the weaker bar.
    assert row["bar"] == "teaches" and row["grounding"] == "teaches-unquoted"
    assert row["quote"] == "" and row["verdict"] == "partial"
    assert "non-English passage" in row["note"]
    #  An English quote that merely mentions one foreign word is never demoted.
    assert deep_analysis.quote_is_english("a plate provided mit a seal for the chamber")
    assert not deep_analysis.quote_is_english(
        "Vorrichtung mit einer Dichtung und einem Griff zum Verlegen der Platten")


def _deep_fixture():
    """Two read references answering one limitation: one verbatim, one teaches-only."""
    lim = {"label": "claim 1[b]", "text": "a housing which has a grip portion", "claim_no": 1,
           "independent": True}
    verified = {"item": "claim 1[b]", "kind": "claim", "verdict": "disclosed",
                "quote": "a housing with a grip", "location": "claim 1", "coord": {},
                "passage_kind": "claim", "confidence": 0.9, "grounding": "verified",
                "refuted": None, "bar": "discloses"}
    teaches = {"item": "claim 1[b]", "kind": "claim", "verdict": "partial", "quote": "",
               "location": "", "coord": {}, "passage_kind": "", "confidence": 0.5,
               "grounding": "teaches-unquoted", "refuted": None, "bar": "teaches"}
    deep = {"references": [
        {"pub": "US-A-B2", "method": "llm", "features": [], "claims": [dict(verified)]},
        {"pub": "US-B-B2", "method": "llm", "features": [], "claims": [dict(teaches)]},
    ], "claims": [lim], "features": []}
    report = {"deep_rank": {"order": ["US-A-B2", "US-B-B2"], "feature_idf": {},
                            "feature_df": {}},
              "query_document": {"claims": [
                  {"claim_no": 1,
                   "text": "1. A grip unit comprising a housing which has a grip portion for "
                           "gripping the grip unit, and an electrically operated vacuum "
                           "generating device."}]}}
    return report, deep


def test_grid_counts_strong_bar_only_but_renders_both():
    report, deep = _deep_fixture()
    cc = webview.build_reading_chart(report, deep, axis="claims")
    assert cc and cc["rows"]
    row = next(r for r in cc["rows"] if r["element"] == "claim 1[b]")
    #  Counted: the verbatim cell only. Rendered: both, the weaker one marked by its bar.
    assert row["df"] == 1 and row["n_disclosing"] == 1
    covered = {c["pub"]: c for c in row["cells"] if c.get("covered")}
    assert covered["US-A-B2"]["bar"] == "discloses"
    assert covered["US-B-B2"]["bar"] == "teaches"


def test_grid_rows_carry_the_whole_claim():
    report, deep = _deep_fixture()
    cc = webview.build_reading_chart(report, deep, axis="claims")
    row = next(r for r in cc["rows"] if r["element"] == "claim 1[b]")
    assert row["claim_no"] == 1
    assert row["claim_whole"].startswith("1. A grip unit comprising")
