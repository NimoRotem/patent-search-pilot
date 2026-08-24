"""The choices a person makes before a submission is built, and what they cost.

Three things used to be decided for the user and discovered afterwards: whether a document is only
secret prior art, whether it looks commonly owned, and what the fee would be. All three were on the
compliance pass, AFTER a model call had been spent per document, and the fee was a unit count with
no money in it. They are now on the picker, priced, with the rule behind each one written out.

The fee is a step function, which is the whole reason the budget is the thing you choose: 1.290(f)
charges the 1.17(o) fee per ten items OR FRACTION THEREOF, so an eleventh document doubles the bill.
"""
import datetime
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import submission as S                                                   # noqa: E402


def _pdf_text(blob):
    try:
        from pypdf import PdfReader
    except Exception:                                                     # noqa: BLE001
        pytest.skip("no PDF reader available")
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)


# ------------------------------------------------------------------ the fee, with money in it

@pytest.mark.parametrize("n,units", [(1, 1), (10, 1), (11, 2), (13, 2), (20, 2), (21, 3)])
def test_the_fee_steps_every_ten_items(n, units):
    assert S.fee_units(n) == units


def test_small_entity_is_the_default_and_large_costs_two_and_a_half_times():
    assert S.fee_amount(13) == S.fee_amount(13, "small")
    u_s, d_s, p_s = S.fee_amount(13, "small")
    u_l, d_l, p_l = S.fee_amount(13, "large")
    assert (u_s, u_l) == (2, 2)
    assert d_s == 2 * p_s and d_l == 2 * p_l
    assert p_l > p_s, "the undiscounted rate must be the higher one"


def test_the_budget_choices_say_what_each_unit_buys():
    ch = S.fee_choices("small")
    assert [c["units"] for c in ch] == [1, 2, 3, 4, 5]
    assert [c["max_documents"] for c in ch] == [10, 20, 30, 40, 50]
    #  the label is what a person reads in the dropdown, so it carries all three facts
    assert "up to 10 documents" in ch[0]["label"] and "$" in ch[0]["label"]
    assert ch[1]["dollars"] == 2 * ch[0]["dollars"]


def test_the_fee_paragraph_prints_the_money_and_the_schedule_date():
    docs = [{"n": i, "pub": "US-%d-A1" % i,
             "biblio": {"pub": "US-%d-A1" % i, "label": "US %d" % i, "kind": "publication",
                        "country": "US", "inventor": "A Inventor",
                        "issue_date_pretty": "February 4, 2021"},
             "rows": [], "compliance": {}} for i in range(1, 14)]
    subject = {"app_no": "19/318,450", "pub_no": "US 2026/0070232 A1"}
    win = S.window("2026-03-12", today=datetime.date(2026, 8, 24))
    text = _pdf_text(S.document_list_and_statements(docs, subject, {}, {}, win,
                                                    entity_size="small"))
    assert "13 items" in text and "2 unit" in text
    assert "$%s" % S._money(S.fee_amount(13, "small")[1]) in text
    assert S.FEE_SCHEDULE_DATE in text
    assert "micro-entity" in text, "a third party cannot use it and the paper should say so"


# ------------------------------------------------------------------ classifying a candidate

def _cands(*pubs):
    return [{"pub": p, "title": "t"} for p in pubs]


def test_a_document_published_before_the_filing_date_is_public_art(monkeypatch):
    monkeypatch.setattr(S, "_as_date", S._as_date)
    rows = {"A": {"publication_number": "A", "publication_date": datetime.date(2020, 1, 1),
                  "filing_date": datetime.date(2019, 1, 1),
                  "earliest_priority_date": datetime.date(2019, 1, 1), "owners": []}}
    _patch_db(monkeypatch, rows)
    out = S.classify_candidates(_cands("A"), "2024-09-09")
    assert out[0]["basis"] == S.PUBLIC and out[0]["default_include"] is True


def test_filed_before_but_published_after_is_secret_art(monkeypatch):
    rows = {"B": {"publication_number": "B", "publication_date": datetime.date(2025, 10, 16),
                  "filing_date": datetime.date(2025, 6, 26),
                  "earliest_priority_date": datetime.date(2017, 4, 27), "owners": []}}
    _patch_db(monkeypatch, rows)
    out = S.classify_candidates(_cands("B"), "2024-09-09")
    assert out[0]["basis"] == S.SECRET
    #  still offered, because it IS citable; it is flagged, not withheld
    assert out[0]["default_include"] is True


def test_filed_and_published_after_is_not_prior_art(monkeypatch):
    rows = {"C": {"publication_number": "C", "publication_date": datetime.date(2026, 1, 1),
                  "filing_date": datetime.date(2025, 1, 1),
                  "earliest_priority_date": datetime.date(2025, 1, 1), "owners": []}}
    _patch_db(monkeypatch, rows)
    out = S.classify_candidates(_cands("C"), "2024-09-09")
    assert out[0]["basis"] == S.NOT_ART
    assert out[0]["default_include"] is False, "listing it invites the examiner to disregard it"


def test_a_shared_owner_is_flagged_and_not_pre_selected(monkeypatch):
    """102(b)(2)(C) removes commonly owned art under 102(a)(2) entirely, so this one has to be a
    deliberate click even though the document is otherwise citable."""
    rows = {"D": {"publication_number": "D", "publication_date": datetime.date(2025, 10, 16),
                  "filing_date": datetime.date(2020, 1, 1),
                  "earliest_priority_date": datetime.date(2020, 1, 1),
                  "owners": ["Magswitch Automation Company"]}}
    _patch_db(monkeypatch, rows)
    out = S.classify_candidates(_cands("D"), "2024-09-09", ["MAGSWITCH AUTOMATION CO LTD"])
    assert out[0]["co_owned"] is True
    assert out[0]["co_owned_with"] == ["Magswitch Automation Company"]
    assert out[0]["default_include"] is False


def test_owner_matching_ignores_the_corporate_suffix_the_punctuation_and_the_case():
    assert S._norm_owner("MAGSWITCH AUTOMATION COMPANY") == S._norm_owner("Magswitch Automation")
    assert S._norm_owner("J. Schmalz GmbH") == S._norm_owner("j schmalz")
    assert S._norm_owner("Nikon Corp.") == S._norm_owner("NIKON CORPORATION")
    #  and it does not collapse two genuinely different owners
    assert S._norm_owner("Schmalz") != S._norm_owner("Magswitch")
    #  a known limit, stated rather than papered over: word order is not normalised, so a register
    #  that inverts the name reads as a different owner and the flag stays off. That is the safe
    #  direction, because the flag suppresses a document and a false positive would hide art.
    assert S._norm_owner("J. Schmalz GmbH") != S._norm_owner("Schmalz J GmbH")


def test_an_unknown_date_is_not_guessed(monkeypatch):
    _patch_db(monkeypatch, {})
    out = S.classify_candidates(_cands("E"), "2024-09-09")
    assert out[0]["basis"] == S.UNKNOWN and out[0]["default_include"] is False


def _patch_db(monkeypatch, rows):
    """Stand in for the one corpus query classify_candidates makes."""
    class Cur:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return list(rows.values())

    class DB:
        @staticmethod
        def cursor():
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield Cur()
            return cm()

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "db", DB)


# ------------------------------------------------------------------ the explanations

@pytest.mark.parametrize("text,must", [
    (S.SECRET_HELP, ["102(a)(2)", "103", "54(3)", "102(b)(2)(C)", "When to include"]),
    (S.CO_OWNED_HELP, ["102(b)(2)(C)", "54(3)", "102(a)(1)", "When to include"]),
])
def test_each_explanation_covers_both_offices_and_says_when_not_to(text, must):
    for m in must:
        assert m in text, "the help text never mentions %s" % m
    assert "When not to" in text
    #  the US and EU answers genuinely differ and the text has to say so rather than blur them
    assert "Europe" in text and "United States" in text


def test_the_picker_shows_the_flags_and_the_budget():
    body = open(os.path.join(ROOT, "templates", "concise.html"), encoding="utf-8").read()
    assert "data-basis" in body and "data-eligible" in body
    assert "secret prior art" in body and "same owner as the application" in body
    assert 'name="fee_units"' in body, "there is no fee budget to choose"
    assert "qhelp" in body, "the flags carry no explanation"
    #  and the two explanations are rendered from the Python constants, not retyped in the template
    assert "{{ secret_help }}" in body and "{{ co_owned_help }}" in body


# ------------------------------------------------------------------ the signature

def test_an_unsigned_packet_says_so_and_a_signed_one_carries_the_s_signature():
    docs = [{"n": 1, "pub": "US-1-A1",
             "biblio": {"pub": "US-1-A1", "label": "US 1", "kind": "publication", "country": "US",
                        "inventor": "A Inventor", "issue_date_pretty": "February 4, 2021"},
             "rows": [{"claim_no": "1"}], "compliance": {}}]
    subject = {"app_no": "19/318,450", "pub_no": "US 2026/0070232 A1"}
    win = S.window("2026-03-12", today=datetime.date(2026, 8, 24))

    blank = _pdf_text(S.document_list_and_statements(docs, subject, {}, {}, win, identity={}))
    assert "NOT SIGNED" in blank and "37 CFR 1.4" in blank

    signed = _pdf_text(S.document_list_and_statements(
        docs, subject, {}, {}, win,
        identity={"signature_name": "Nimo Rotem", "signature_title": "Director"}))
    assert "/Nimo Rotem/" in signed
    assert "Director" in signed and "1.4(d)(2)" in signed
    assert "NOT SIGNED" not in signed


def test_the_audit_only_calls_the_statements_met_when_they_are_signed():
    docs = [{"n": 1, "pub": "US-1-A1",
             "biblio": {"pub": "US-1-A1", "label": "US 1", "kind": "publication", "country": "US",
                        "inventor": "A Inventor", "issue_date_pretty": "February 4, 2021"},
             "rows": [{"claim_no": "1"}], "compliance": {}}]
    subject = {"app_no": "19/318,450", "pub_no": "US 2026/0070232 A1"}
    win = S.window("2026-03-12", today=datetime.date(2026, 8, 24))
    unsigned = {f.id: f for f in S.audit(docs, subject, {}, {}, win)}
    assert unsigned["STATEMENTS"].status == S.ACTION
    signed = {f.id: f for f in S.audit(docs, subject, {}, {}, win,
                                       identity={"signature_name": "Nimo Rotem"})}
    assert signed["STATEMENTS"].status == S.OK


def test_a_signature_name_cannot_smuggle_a_slash(monkeypatch):
    """An S-signature is delimited by forward slashes, so one inside the name would close it
    early and print something the signer did not write."""
    import accounts
    monkeypatch.setattr(accounts, "ensure_schema", lambda: None)
    with pytest.raises(ValueError):
        accounts.set_filing_identity(1, signature_name="Nimo /Rotem/")
    with pytest.raises(ValueError):
        accounts.set_filing_identity(1, entity_size="micro")


# ------------------------------------------------------------------ the deadline never goes stale

def test_the_audit_prints_a_date_and_says_to_count_from_today():
    """A PDF written once and read later cannot carry a countdown: "20 days away" is wrong the
    next morning. The deadline does not move, so that is what the paper states."""
    docs = [{"n": 1, "pub": "US-1-A1",
             "biblio": {"pub": "US-1-A1", "label": "US 1", "kind": "publication", "country": "US",
                        "inventor": "A", "issue_date_pretty": "February 4, 2021"},
             "rows": [{"claim_no": "1"}], "compliance": {}}]
    win = S.window("2026-03-12", today=datetime.date(2026, 8, 24))
    f = {x.id: x for x in S.audit(docs, {"app_no": "19/318,450"}, {}, {}, win)}
    d = f["TIMING"].detail
    assert "2026-09-12" in d
    assert "count from today" in d
    assert str(datetime.date.today()) in d, "the audit must date itself"
