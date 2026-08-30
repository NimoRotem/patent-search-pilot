"""The filed paper carries what a filer would have typed, and nothing about the software.

Every phrase asserted absent here was on the real 01_DocumentList_and_Statements.pdf built for
U.S. application 19/318,450, and every one of them is process, arithmetic or an instruction to the
filer rather than filing substance.
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pypdf                                                              # noqa: E402

import submission                                                         # noqa: E402

SUBJECT = {"app_no": "19/318,450", "pub_no": "US 2026/0070232 A1",
           "title": "Lifting device", "inventor": "David H. Morton",
           "pub_date": "2026-03-12"}

DOCS = [
    {"n": 1, "pub": "CN114906626A",
     "biblio": {"pub": "CN114906626A", "label": "CN114906626A", "country": "CN",
                "applicant": "ZHEJIANG SHENGDA STEEL TOWER CO Ltd",
                "issue_date_pretty": "August 16, 2022"}},
    {"n": 2, "pub": "US20240087784A1",
     "biblio": {"pub": "US20240087784A1",
                "label": "U.S. Patent Application Publication No. US 2024/0087784 A1",
                "country": "US", "inventor": "David H. Morton",
                "issue_date_pretty": "March 14, 2024"}},
]
COPIES = {"CN114906626A": b"%PDF-1.4 copy"}
TRANSLATIONS = {"CN114906626A": {"text": "translated"}}
WIN = {"deadline": "2026-09-12", "state": "open",
       "basis": "publication 2026-03-12 plus six months = 2026-09-12; no rejection has been mailed",
       "why": ""}

SIGNER = {"entity_size": "small", "signature_name": "Nimo Rotem",
          "signature_title": "Director", "signature_consent_at": "2026-08-29T10:00:00Z"}
NO_CONSENT = {"entity_size": "small", "signature_name": "Nimo Rotem",
              "signature_title": "Director"}


def _pages(blob):
    return [p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(blob)).pages]


def _text(blob):
    return " ".join(" ".join(_pages(blob)).split())


def _build(identity=SIGNER, exemption=False, docs=DOCS):
    return submission.document_list_and_statements(
        docs, SUBJECT, COPIES, TRANSLATIONS, WIN,
        exemption_claimed=exemption, entity_size="small", identity=identity)


# ------------------------------------------------------- what must not be on a paper being filed
def test_no_fee_narrative():
    t = _text(_build()).lower()
    for phrase in ("1.290(f)", "1.17(o)", "small-entity rate", "micro-entity",
                   "schedule of", "patent center", "unit of that fee", "units of that fee"):
        assert phrase not in t, phrase


def test_no_timing_paragraph():
    t = _text(_build()).lower()
    for phrase in ("must be filed before", "window may extend", "certificate of mailing",
                   "1.290(i)", "1.290(b)"):
        assert phrase not in t, phrase


def test_no_signature_provenance():
    t = _text(_build()).lower()
    for phrase in ("stored signature", "at the signer's instruction", "signed under 37 cfr 1.4",
                   "the statements above are the signer's"):
        assert phrase not in t, phrase


def test_no_instructions_to_the_filer():
    t = _text(_build()).lower()
    for phrase in ("so they can be read", "reproduced here", "before they are adopted",
                   "check the rate", "not signed", "set one in your profile"):
        assert phrase not in t, phrase


def test_no_compliance_narrative():
    t = _text(_build()).lower()
    for phrase in ("requires one for every listed item", "as 1.290(d)(2) requires",
                   "is available if"):
        assert phrase not in t, phrase


def test_outstanding_is_stated_as_fact_not_shouted():
    blob = _build(docs=[dict(DOCS[0], n=1)])            # a copy exists, a translation does not
    t = _text(submission.document_list_and_statements(
        [DOCS[0]], SUBJECT, {}, {}, WIN, identity=SIGNER))
    assert "OUTSTANDING" not in t
    assert "copy not attached" in t
    assert blob


# ---------------------------------------------------------------- what must still be on the paper
def test_the_required_statements_survive():
    t = _text(_build())
    assert "STATEMENTS UNDER 37 CFR" in t
    assert "not an individual who has a duty to disclose" in t
    assert "complies with the requirements of 35 U.S.C. 122(e)" in t


def test_the_document_list_survives():
    t = _text(_build())
    assert "Section A. U.S. patents" in t
    assert "Section B. All other items" in t
    assert "CN114906626A" in t and "US 2024/0087784" in t


def test_the_1290g_statement_is_filed_when_it_is_claimed():
    t = _text(_build(exemption=True, docs=DOCS[:1]))
    assert "STATEMENT UNDER 37 CFR" in t
    assert "first and only submission under 35 U.S.C. 122(e)" in t
    #  The statement, not the arithmetic that goes with it.
    assert "three or fewer" not in t.lower()


def test_the_1290g_statement_is_absent_when_it_is_not_claimed():
    assert "first and only submission" not in _text(_build(exemption=False, docs=DOCS[:1]))


# --------------------------------------------------------------------- signature is an execution
def test_unsigned_by_default_when_consent_was_never_recorded():
    t = _text(_build(identity=NO_CONSENT))
    assert "/Nimo Rotem/" not in t
    assert "Printed name:" in t


def test_signed_only_on_recorded_authorisation():
    assert "/Nimo Rotem/" in _text(_build(identity=SIGNER))


def test_signature_and_signer_identity_are_on_one_page():
    """The real packet stranded /Nimo Rotem/ on page 2 and "Nimo Rotem, Director" on page 3."""
    pages = _pages(_build())
    sig = [i for i, p in enumerate(pages) if "/Nimo Rotem/" in " ".join(p.split())]
    named = [i for i, p in enumerate(pages) if "Nimo Rotem, Director" in " ".join(p.split())]
    assert sig and named, (sig, named)
    assert sig[0] == named[0], "signature on page %s, signer identity on page %s" % (sig, named)


def test_a_long_list_still_keeps_the_signature_whole():
    many = [dict(DOCS[0], n=i, pub="CN%07dA" % i,
                 biblio=dict(DOCS[0]["biblio"], pub="CN%07dA" % i, label="CN%07dA" % i))
            for i in range(1, 41)]
    pages = _pages(submission.document_list_and_statements(
        many, SUBJECT, {}, {}, WIN, identity=SIGNER))
    sig = [i for i, p in enumerate(pages) if "/Nimo Rotem/" in " ".join(p.split())]
    named = [i for i, p in enumerate(pages) if "Nimo Rotem, Director" in " ".join(p.split())]
    assert sig and named and sig[0] == named[0], (sig, named)


# ------------------------------------------------------------------- the backstop, on real text
def test_filing_leaks_catches_the_sentences_that_were_actually_filed():
    for line in (
            "It is paid in Patent Center; check the rate has not moved.",
            "These statements are made by the party filing the submission. They are reproduced "
            "here so they can be read and checked before they are adopted in Patent Center.",
            "The signature above was applied from the signer's own stored signature at the "
            "signer's instruction.",
            "no rejection has been mailed, so the window may extend beyond the date below",
            "Under 37 CFR 1.290(f) the fee set by 37 CFR 1.17(o) is due for every ten items"):
        assert submission.filing_leaks(line), line


def test_filing_leaks_passes_ordinary_filing_language():
    for line in (
            "The party making this submission is not an individual who has a duty to disclose "
            "information with respect to the above-identified application under 37 CFR 1.56.",
            "This submission complies with the requirements of 35 U.S.C. 122(e) and 37 CFR 1.290.",
            "To the knowledge of the person signing this statement after making reasonable "
            "inquiry, this submission is the first and only submission under 35 U.S.C. 122(e) "
            "filed in this application by the party making the submission."):
        assert submission.filing_leaks(line) == [], (line, submission.filing_leaks(line))


def test_the_generated_paper_trips_no_leak_term():
    assert submission.filing_leaks(_text(_build())) == []
    assert submission.filing_leaks(_text(_build(exemption=True, docs=DOCS[:1]))) == []
    assert submission.filing_leaks(_text(_build(identity=NO_CONSENT))) == []
