"""37 CFR 1.290, paragraph by paragraph, checked against the packet the code actually builds.

The rule is the specification, so these tests are organised the way the rule is, and each one names
the paragraph it enforces. A submission that misses any of them "may not be entered or considered
by the Office" (1.290(a)), which is why the packet carries an audit rather than a claim.

Rule text verified against the CFR and MPEP 1134.01 on 2026-08-23. The two things most easily got
wrong, and both were:

  * the fee is 1.17(o) per ten items OR FRACTION THEREOF, and three-or-fewer only escapes it WITH
    the 1.290(g) statement. Three or fewer without the statement still pays.
  * 1.290(b) is a nest: before the EARLIER of a notice of allowance, or the LATER of publication
    plus six months and the first rejection. Read the wrong way round it says the opposite.
"""
import datetime
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import submission as S                                                   # noqa: E402

SUBJECT = {"app_no": "19/318,450", "pub_no": "US 2026/0070232 A1", "title": "", "inventor": ""}


def _doc(n=1, pub="US-2021031317-A1", kind="publication", country="US", inventor="A Inventor",
         rows=None, **biblio):
    #  `publication_date` is what submission_compliance.qualify reads; `issue_date_pretty` is only
    #  what the paper prints. A fixture with the pretty one alone is not prior art to anything.
    b = {"pub": pub, "label": "U.S. Patent Application Publication No. US 2021/0031317 A1",
         "kind": kind, "country": country, "inventor": inventor, "title": "A gripper",
         "issue_date_pretty": "February 4, 2021", "publication_date": "2021-02-04",
         "filing_date": "2019-07-30", "priority_date": "2019-07-30"}
    b.update(biblio)
    return {"n": n, "pub": pub, "biblio": b, "subject": SUBJECT, "summary": "It discloses a thing.",
            "rows": rows if rows is not None else [
                {"claim_no": "1", "claim_text": "a gripper", "claim_paraphrase": "a gripper",
                 "quote_claim": False, "note": "discloses a gripper", "quote": "a gripper",
                 "strong": True, "cites": [], "disclosure": "discloses a gripper"}],
            "compliance": {}}


def _jp(n=5):
    return _doc(n=n, pub="JP-2019155534-A", kind="foreign", country="JP",
                inventor="Yusei Kimura", label="JP 2019155534 A",
                issue_date_pretty="September 19, 2019")


def _pdf_text(blob):
    try:
        from pypdf import PdfReader
    except Exception:                                                     # noqa: BLE001
        try:
            from PyPDF2 import PdfReader
        except Exception:
            pytest.skip("no PDF reader available")
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)


# ------------------------------------------------------------------ 1.290(b) the window

D = datetime.date


def test_the_window_is_the_earlier_of_allowance_and_the_later_of_two_dates():
    """The exact shape of the rule, in the four combinations that matter."""
    #  no rejection yet: the floor is publication + six months
    w = S.window("2026-03-12", None, None, today=D(2026, 8, 23))
    assert w["deadline"] == D(2026, 9, 12) and w["open"] is True and w["days_left"] == 20

    #  a rejection AFTER publication+6mo extends the window to the rejection date
    w = S.window("2026-03-12", "2026-11-01", None, today=D(2026, 8, 23))
    assert w["deadline"] == D(2026, 11, 1) and w["open"] is True

    #  a rejection BEFORE publication+6mo does not shorten it: the rule takes the LATER
    w = S.window("2026-03-12", "2026-04-01", None, today=D(2026, 8, 23))
    assert w["deadline"] == D(2026, 9, 12)

    #  a notice of allowance caps it, because that branch is the EARLIER of
    w = S.window("2026-03-12", "2026-11-01", "2026-08-01", today=D(2026, 8, 23))
    assert w["deadline"] == D(2026, 8, 1) and w["open"] is False
    assert w["capped_by_allowance"] is True


def test_a_closed_window_blocks_the_whole_submission():
    w = S.window("2020-01-01", None, None, today=D(2026, 8, 23))
    assert w["open"] is False
    f = {x.id: x for x in S.audit([_doc()], SUBJECT, {}, {}, w)}
    assert f["TIMING"].status == S.BLOCKED
    assert S.verdict(list(f.values()))[0] == S.BLOCKED


def test_an_unknown_publication_date_is_an_action_not_a_guess():
    w = S.window(None, None, None, today=D(2026, 8, 23))
    assert w["open"] is None and w["deadline"] is None
    f = {x.id: x for x in S.audit([_doc()], SUBJECT, {}, {}, w)}
    assert f["TIMING"].status == S.ACTION


def test_six_months_clamps_to_a_month_that_is_shorter():
    assert S._plus_six_months(D(2026, 8, 31)) == D(2027, 2, 28)
    assert S._plus_six_months(D(2026, 3, 12)) == D(2026, 9, 12)


# ------------------------------------------------------------------ 1.290(e) item typing

@pytest.mark.parametrize("pub,kind,expect", [
    ("US-9260251-B2", "patent", S.US_PATENT),
    ("US-2021031317-A1", "publication", S.US_PGPUB),
    ("JP-2019155534-A", "foreign", S.FOREIGN),
    ("EP-1234567-A1", "foreign", S.FOREIGN),
])
def test_each_item_falls_under_its_own_identification_rule(pub, kind, expect):
    assert S.item_kind(_doc(pub=pub, kind=kind, country=pub[:2])) == expect


def test_only_non_us_patent_documents_need_a_copy():
    """1.290(d)(3) excludes exactly U.S. patents and U.S. patent application publications."""
    assert not S.needs_copy(_doc(pub="US-9260251-B2", kind="patent"))
    assert not S.needs_copy(_doc(pub="US-2021031317-A1", kind="publication"))
    assert S.needs_copy(_jp())
    assert S.needs_copy(_doc(pub="EP-1-A1", kind="foreign", country="EP"))


def test_only_non_english_offices_need_a_translation():
    assert not S.needs_translation(_doc())
    assert S.needs_translation(_jp())
    assert not S.needs_translation(_doc(pub="GB-1-A", kind="foreign", country="GB"))
    #  keyed on the OFFICE, never on a language sniff of text we happen to hold
    assert S.needs_translation(_doc(pub="DE-1-A1", kind="foreign", country="DE"))


def test_the_identification_fields_differ_by_type():
    us = dict(S._identification(_doc(pub="US-9260251-B2", kind="patent")))
    assert "First named inventor" in us and "Issue date" in us
    fo = dict(S._identification(_jp()))
    #  1.290(e)(3) wants the office and the document number, which (e)(1) does not ask for
    assert fo["Issuing office"] == "Japan Patent Office"
    assert "Applicant, patentee or first named inventor" in fo
    assert "Publication date" in fo


def test_the_list_puts_us_documents_in_their_own_section():
    docs = [_doc(1), _jp(2), _doc(3, pub="US-9260251-B2", kind="patent")]
    text = _pdf_text(S.document_list_and_statements(
        docs, SUBJECT, {"JP-2019155534-A": True}, {"JP-2019155534-A": True},
        S.window("2026-03-12", today=D(2026, 8, 23))))
    a, b = text.index("Section A"), text.index("Section B")
    assert a < b, "the U.S. section must come first"
    #  the JP item is below the section break and the U.S. ones above it
    assert text.index("2019155534") > b
    assert text.index("2021/0031317") < b


def test_the_application_number_is_on_every_page_of_the_list():
    """1.290(e) says each page, not the first one."""
    docs = [_doc(i) for i in range(1, 26)]           # enough to spill onto a second page
    blob = S.document_list_and_statements(docs, SUBJECT, {}, {},
                                          S.window("2026-03-12", today=D(2026, 8, 23)))
    try:
        from pypdf import PdfReader
    except Exception:                                                     # noqa: BLE001
        pytest.skip("no PDF reader")
    pages = PdfReader(io.BytesIO(blob)).pages
    assert len(pages) > 1, "this fixture is meant to span pages"
    for i, p in enumerate(pages):
        assert "19/318,450" in (p.extract_text() or ""), "page %d has no application number" % (i + 1)


# ------------------------------------------------------------------ 1.290(f) and (g) the fee

@pytest.mark.parametrize("n,units", [(0, 0), (1, 1), (3, 1), (10, 1), (11, 2), (20, 2), (21, 3)])
def test_the_fee_is_charged_per_ten_items_or_fraction_thereof(n, units):
    assert S.fee_units(n) == units


def test_three_or_fewer_is_only_exempt_with_the_statement():
    """MPEP 1134.01 is explicit: three or fewer WITHOUT the 1.290(g) statement still pays."""
    assert S.exemption_available(3) and not S.exemption_available(4)
    docs = [_doc(1), _doc(2), _doc(3)]
    w = S.window("2026-03-12", today=D(2026, 8, 23))

    claimed = _pdf_text(S.document_list_and_statements(docs, SUBJECT, {}, {}, w,
                                                       exemption_claimed=True))
    assert "first and only submission" in claimed and "no fee is required" in claimed

    not_claimed = _pdf_text(S.document_list_and_statements(docs, SUBJECT, {}, {}, w,
                                                           exemption_claimed=False))
    assert "1.17(o)" in not_claimed
    assert "still pays the fee" in not_claimed, (
        "a three-item submission that does not claim the exemption must be told it owes the fee")


# ------------------------------------------------------------------ the audit

def _audit(docs, copies=None, translations=None, **kw):
    w = kw.pop("win", S.window("2026-03-12", today=D(2026, 8, 23)))
    return {f.id: f for f in S.audit(docs, SUBJECT, copies or {}, translations or {}, w, **kw)}


def test_a_missing_copy_is_reported_and_not_papered_over():
    f = _audit([_jp()])
    assert f["COPIES"].status == S.ACTION and "Missing" in f["COPIES"].detail
    f = _audit([_jp()], copies={"JP-2019155534-A": True})
    assert f["COPIES"].status == S.OK


def test_a_missing_translation_is_reported():
    f = _audit([_jp()], copies={"JP-2019155534-A": True})
    assert f["TRANSLATION"].status == S.ACTION
    f = _audit([_jp()], copies={"JP-2019155534-A": True},
               translations={"JP-2019155534-A": {"text": "x"}})
    assert f["TRANSLATION"].status == S.OK


def test_an_all_us_packet_needs_neither():
    f = _audit([_doc(1), _doc(2, pub="US-9260251-B2", kind="patent")])
    assert f["COPIES"].status == S.OK and "excludes" in f["COPIES"].detail
    assert f["TRANSLATION"].status == S.OK


def test_a_document_with_no_description_blocks_the_submission():
    """1.290(d)(2) wants one for each item, so an item without one cannot be listed."""
    f = _audit([_doc(1), _doc(2, rows=[])])
    assert f["DESCRIPTION"].status == S.BLOCKED
    assert S.verdict(list(f.values()))[0] == S.BLOCKED


def test_argument_in_a_description_is_caught():
    """MPEP 1134.01: a claim chart is fine, a conclusion about patentability is not."""
    bad = _doc(rows=[{"claim_no": "1", "claim_text": "x", "claim_paraphrase": "x",
                      "quote_claim": False, "quote": "", "strong": False, "cites": [],
                      "note": "", "disclosure": "This renders claim 1 obvious and unpatentable."}])
    f = _audit([bad])
    assert f["NO-ARGUMENT"].status == S.ACTION
    assert "unpatentable" in f["NO-ARGUMENT"].detail or "obvious" in f["NO-ARGUMENT"].detail
    assert _audit([_doc()])["NO-ARGUMENT"].status == S.OK


def test_a_quotation_from_the_reference_is_not_our_argument():
    """The scan must not fire on a word that appears inside the document's own quoted text: the
    row's `quote` is the reference speaking, not us."""
    d = _doc(rows=[{"claim_no": "1", "claim_text": "x", "claim_paraphrase": "x",
                    "quote_claim": False, "strong": True, "cites": [], "note": "discloses a latch",
                    "disclosure": "discloses a latch",
                    "quote": "the prior art was obvious to the inventor"}])
    assert _audit([d])["NO-ARGUMENT"].status == S.OK


def test_the_verdict_counts_decisions_and_not_advisories():
    f = _audit([_doc()])
    assert f["NO-CERT-MAILING"].status == S.NOTE, "1.8 asks nothing of anybody"
    state, sentence = S.verdict(list(f.values()))
    assert state == S.ACTION
    #  statements and fee are the two real decisions on an all-U.S., in-window packet
    assert "2 items" in sentence


def test_a_fully_satisfied_packet_reads_as_ready():
    f = [x for x in _audit([_doc()]).values()]
    for x in f:
        if x.status == S.ACTION:
            x.status = S.OK
    assert S.verdict(f) == (S.OK, "Every requirement of 37 CFR 1.290 is satisfied by the papers "
                                  "in this packet.")


def test_the_audit_paper_names_every_rule_it_checked():
    docs = [_doc(1), _jp(2)]
    f = S.audit(docs, SUBJECT, {"JP-2019155534-A": True}, {"JP-2019155534-A": {"text": "x"}},
                S.window("2026-03-12", today=D(2026, 8, 23)))
    text = _pdf_text(S.audit_pdf(f, docs, SUBJECT, S.window("2026-03-12", today=D(2026, 8, 23))))
    for cite in ("1.290(b)", "1.290(d)(1)", "1.290(d)(2)", "1.290(d)(3)", "1.290(d)(4)",
                 "1.290(d)(5)", "1.290(e)", "MPEP"):
        assert cite in text, "the audit paper does not mention %s" % cite
    assert "1.290(a)" in text, "it should say what happens to a non-compliant submission"


def test_an_unverified_quotation_takes_its_row_out_of_the_paper():
    """The bar beyond the rule. A row that offered a passage and lost it is removed rather than
    filed as a bare assertion, and the audit says so."""
    import submission_compliance as sc
    doc = _doc(rows=[
        {"claim_no": "1", "claim_text": "a", "claim_paraphrase": "a", "quote_claim": False,
         "note": "n", "disclosure": "d", "quote": "this phrase is really in the document",
         "strong": True, "cites": []},
        {"claim_no": "2", "claim_text": "b", "claim_paraphrase": "b", "quote_claim": False,
         "note": "n", "disclosure": "d", "quote": "this phrase was never in the document",
         "strong": True, "cites": []},
        {"claim_no": "3", "claim_text": "c", "claim_paraphrase": "c", "quote_claim": False,
         "note": "taught", "disclosure": "d", "quote": "", "strong": False, "cites": []},
    ])
    kept, blocked, _notes = sc.apply(
        [doc], {"efd": "2024-09-09"},
        source_text_for=lambda pub: "... this phrase is really in the document ...",
        mode="novelty")
    assert not blocked
    left = [r["claim_no"] for r in kept[0]["rows"]]
    assert left == ["1", "3"], "the row whose quotation failed verification is still on the paper"
    assert kept[0]["compliance"]["rows_dropped"] == 1


def test_a_document_that_loses_every_row_is_dropped_from_the_submission():
    import submission_compliance as sc
    doc = _doc(rows=[{"claim_no": "1", "claim_text": "a", "claim_paraphrase": "a",
                      "quote_claim": False, "note": "n", "disclosure": "d",
                      "quote": "nowhere in the document", "strong": True, "cites": []}])
    kept, blocked, _notes = sc.apply([doc], {"efd": "2024-09-09"},
                                     source_text_for=lambda pub: "something else entirely",
                                     mode="novelty")
    assert not kept and len(blocked) == 1
    assert "nothing left to describe" in blocked[0]["why"]


def test_the_manifest_says_what_is_filed_for_each_item():
    docs = [_doc(1), _jp(2)]
    csv_text = S.manifest_csv(docs, {"JP-2019155534-A": True}, {"JP-2019155534-A": {"text": "x"}})
    lines = [l for l in csv_text.splitlines() if l.strip()]
    assert lines[0].startswith("item,identifier,type")
    assert "not required" in lines[1]          # the U.S. publication
    assert lines[2].count("yes") >= 2          # the JP item: copy and translation both filed
