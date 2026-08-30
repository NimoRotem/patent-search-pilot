"""Reading the file wrapper: numbers off a scanned form, and what the examiner did with them.

Counsel's feature request, 2026-08-20: "mine the family's prosecution history... the examiner's own
limitation-by-limitation findings, submitted as a printed publication, so his analysis does the
arguing that 1.290's no-argument rule forbids me from doing."

The OCR itself needs a paid model and a live USPTO, so it is not exercised here. Everything around
it is: the number grammar, the corpus resolution, the ordering of seeds, and the refusal to invent.
"""
import prosecution


# --------------------------------------------------------------------------- numbers


def test_a_us_patent_number_survives_its_commas():
    assert prosecution.normalise("US 11,413,727") == "US-11413727"
    assert prosecution.normalise("11,413,727") == "US-11413727"
    assert prosecution.normalise("7,690,610 B2") == "US-7690610"


def test_a_pre_grant_publication_keeps_its_leading_zero():
    """"2020/0338695" is year 2020 + serial 0338695. Dropping the zero here is how a reference the
    corpus holds looks absent — the exact failure that lost three of nineteen numbers."""
    assert prosecution.normalise("2020/0338695") == "US-20200338695"
    assert prosecution.normalise("US 2004/0050205 A1") == "US-20040050205"


def test_a_foreign_number_keeps_its_office():
    assert prosecution.normalise("DE 10 2013 106 004") == "DE-102013106004"
    assert prosecution.normalise("EP 3707092 A1") == "EP-3707092"


def test_junk_is_dropped_rather_than_guessed():
    """A number the model could not read must not become a lookup. Every one of these appeared in
    the `all_numbers` of a real OCR pass: dates, art units, page numbers, claim ranges."""
    for junk in ("", None, "1-20", "11/19/2023", "7590", "see above", "Fig. 3", "para. [0012]"):
        assert prosecution.normalise(junk) == "", junk


def test_an_application_number_is_not_a_publication():
    #  "17/724,791" is an application serial, not a pre-grant publication: the year field would be
    #  17, which is not a year. It must not resolve to a document.
    assert prosecution.normalise("17/724,791") == ""


# --------------------------------------------------------------------------- resolution


def test_resolution_tries_every_spelling_the_corpus_might_hold(monkeypatch):
    """The corpus drops SOME leading zeros. pubnorm owns that ladder; resolve must use it."""
    asked = {}

    class _Cur:
        def execute(self, sql, params=None):
            asked["exact"], asked["like"] = params

        def fetchall(self):
            return [{"publication_number": "US-2020338695-A1",
                     "bare": "US2020338695A1", "n_chunks": 138}]

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    import db
    monkeypatch.setattr(db, "cursor", lambda *a, **k: _Ctx())
    found, missing = resolve_quiet(["2020/0338695"])
    assert found == {"US-20200338695": "US-2020338695-A1"}, found
    assert missing == []
    #  the one-zero-lighter spelling the corpus actually stores has to be among the keys tried
    assert any(k.startswith("US2020338695") for k in asked["exact"]), asked["exact"]


def test_a_number_the_corpus_does_not_hold_is_reported_not_silently_dropped(monkeypatch):
    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return []

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    import db
    monkeypatch.setattr(db, "cursor", lambda *a, **k: _Ctx())
    found, missing = resolve_quiet(["US 1,234,567"])
    assert found == {} and missing == ["US-1234567"]


def test_a_stub_row_loses_to_the_one_that_can_be_read(monkeypatch):
    """The corpus holds both a bare `US-12115659` with nothing in it and the real
    `US-12115659-B1`. Returning the stub costs the reading."""
    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [{"publication_number": "US-12115659", "bare": "US12115659", "n_chunks": 0},
                    {"publication_number": "US-12115659-B1", "bare": "US12115659B1",
                     "n_chunks": 91}]

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *a):
            return False

    import db
    monkeypatch.setattr(db, "cursor", lambda *a, **k: _Ctx())
    found, _ = resolve_quiet(["US 12,115,659"])
    assert found["US-12115659"] == "US-12115659-B1"


def resolve_quiet(nums):
    return prosecution.resolve(nums, log=None)


# --------------------------------------------------------------------------- mining


def _fake_dossier():
    return {"rejections": [{"app": "17724791", "code": "CTNF", "date": "2025-09-16",
                            "description": "Non-Final Rejection", "pdf": "https://x/1.pdf",
                            "id": "a"}],
            "citation_lists": [{"app": "17724791", "code": "1449", "date": "2025-09-16",
                                "description": "List of References", "pdf": "https://x/2.pdf",
                                "id": "b"}],
            "family": [], "siblings_granted": [], "error": ""}


def test_the_applied_references_lead_the_seed_order(monkeypatch):
    """Seed order is the order the reading budget is spent in. A reference an examiner APPLIED in
    a rejection outranks one that merely sat in an information disclosure statement."""
    reads = {
        "https://x/1.pdf": {"applied": [{"number": "US 11,413,727", "statute": "102(a)(2)",
                                         "claims": "1-3"}],
                            "considered": [], "summary": "s"},
        "https://x/2.pdf": {"applied": [], "considered": ["US 6,419,291", "US 7,240,935"],
                            "summary": "s"},
    }
    monkeypatch.setattr(prosecution, "read_document",
                        lambda rec, log=print, use_cache=True: dict(
                            reads[rec["pdf"]], app=rec["app"], code=rec["code"],
                            date=rec["date"], description=rec["description"], pdf=rec["pdf"]))
    monkeypatch.setattr(prosecution, "resolve", lambda nums, log=print: (
        {"US-11413727": "US-11413727-B2", "US-6419291": "US-6419291-B1",
         "US-7240935": "US-7240935-B2"}, []))
    out = prosecution.mine(_fake_dossier(), log=None)
    assert out["seeds"][0] == "US-11413727-B2", out["seeds"]
    assert set(out["seeds"]) == {"US-11413727-B2", "US-6419291-B1", "US-7240935-B2"}
    assert out["applied"][0]["pub"] == "US-11413727-B2"
    assert out["applied"][0]["statute"] == "102(a)(2)"
    assert "17724791 CTNF 2025-09-16" in out["applied"][0]["source"]


def test_an_unreadable_wrapper_costs_its_own_findings_and_nothing_else(monkeypatch):
    monkeypatch.setattr(prosecution, "read_document",
                        lambda rec, log=print, use_cache=True: {})
    out = prosecution.mine(_fake_dossier(), log=None)
    assert out["seeds"] == [] and out["applied"] == [] and not out["error"]


def test_a_missing_dossier_never_raises():
    assert prosecution.mine(None, log=None)["error"]
    assert prosecution.mine({"error": "no USPTO_ODP_KEY"}, log=None)["seeds"] == []


def test_summarise_says_nothing_when_there_is_nothing_to_say():
    assert prosecution.summarise({}) == ""
    assert prosecution.summarise({"documents": [], "applied": []}) == ""
