"""The three gates that stand between a built packet and Patent Center.

  the copy contains the quotation   the cheapest check there is, and it catches a copy that is not
                                    the document as well as a quotation that is not in it
  the PDF uploads                   fonts embedded, PDF 1.1 to 1.6, US Letter or A4, no encryption,
                                    no layers, no attachments
  nothing is inferred               argument does not need a word from the patentability lexicon
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pdf_conform                                                       # noqa: E402
import pdf_fonts                                                         # noqa: E402
import submission as S                                                   # noqa: E402
import submission_compliance as sc                                       # noqa: E402
import submission_package as sp                                          # noqa: E402

QUOTE = ("rectilinear sliding of the assembly in a direction parallel to a holding face provided "
         "by the edges of the pole plates")


def _doc(n=1, pub="GB-874600-A", rows=None):
    return {"n": n, "pub": pub, "rows": rows if rows is not None else [{"quote": QUOTE}],
            "biblio": {"pub": pub, "label": "GB 874,600", "country": "GB", "kind": "",
                       "inventor": "A Smith", "issue_date_pretty": "9 August 1961"}}


def _findings(docs, copies):
    return {f.id: f for f in S._copy_quote_findings(docs, copies)}


# --------------------------------------------------------------------------- copy holds quote


def test_a_quotation_that_is_in_the_copy_passes():
    copies = {"GB-874600-A": {"pages": 14, "chars": 20000,
                              "text": "... " + QUOTE + " ...", "drawings_only": False}}
    got = _findings([_doc()], copies)
    assert got["COPY-QUOTES"].status == S.OK
    assert "the examiner will open" in got["COPY-QUOTES"].detail


def test_the_drawings_only_copy_is_caught_by_the_quotations_alone():
    """The GB 874,600 copy filed in a real packet was its six drawing sheets. Eight quotations were
    attributed to an abstract that is not on any of them."""
    copies = {"GB-874600-A": {"pages": 6, "chars": 0, "text": "", "drawings_only": True}}
    got = _findings([_doc()], copies)
    assert "COPY-QUOTES" not in got, "a copy with no text layer answers neither way"
    assert got["COPY-QUOTES-UNREADABLE"].status == S.ACTION
    assert "image scans" in got["COPY-QUOTES-UNREADABLE"].detail


def test_a_quotation_the_readable_copy_does_not_contain_is_named():
    copies = {"GB-874600-A": {"pages": 14, "chars": 20000, "drawings_only": False,
                              "text": "A device for lifting steel plates by magnetic attraction."}}
    got = _findings([_doc()], copies)
    assert got["COPY-QUOTES"].status == S.ACTION
    assert "1 of 1 quotations are not in the copy" in got["COPY-QUOTES"].detail
    assert "either what you attached is not the document or the quotation is not in it" \
        in got["COPY-QUOTES"].detail


def test_quotes_in_copy_matches_on_the_opening_run_so_an_elision_does_not_fail_it():
    copy = {"text": "... " + QUOTE + " ..."}
    got = sp.quotes_in_copy(copy, [QUOTE[:60] + " …"])
    assert got["missing"] == []


def test_inspect_copy_keeps_the_text_the_check_needs():
    #  A one-page PDF with a real text layer, built with the same engine the packet uses.
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(pdf_fonts.font(pdf_fonts.SERIF), 11)
    for i, line in enumerate(QUOTE.split(" of ")):
        c.drawString(72, 700 - 20 * i, "rectilinear sliding of the assembly " + line)
    c.showPage()
    c.save()
    got = sp.inspect_copy(buf.getvalue())
    assert got["pages"] == 1
    assert "rectilinear sliding" in got["text"]
    assert got["drawings_only"] is False


# --------------------------------------------------------------------------- the PDF gate


def _one_page_pdf(pagesize=None, base14=False):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=pagesize or letter)
    c.setFont("Helvetica" if base14 else pdf_fonts.font(pdf_fonts.SERIF), 11)
    c.drawString(72, 200, "A magnetic gripper.")
    c.showPage()
    c.save()
    return buf.getvalue()


def test_a_paper_typeset_in_the_embedded_faces_passes():
    if pdf_fonts.missing():
        import pytest
        pytest.skip("this host has no font files, which the FONTS finding reports separately")
    got = pdf_conform.check(_one_page_pdf())
    assert got["ok"] is True, got["problems"]
    assert got["unembedded"] == []
    assert got["encrypted"] is False and got["layers"] is False


def test_an_unembedded_base_14_font_is_caught_on_the_artefact_not_on_the_intention():
    """Seventeen of twenty-one papers in a real packet named Times or Helvetica and embedded
    neither. reportlab writes the name and not the font; Patent Center calls that a failure."""
    got = pdf_conform.check(_one_page_pdf(base14=True))
    assert got["ok"] is False
    assert any("Helvetica" in n for n in got["unembedded"])
    assert any("not embedded" in p for p in got["problems"])


def test_a_page_that_is_neither_letter_nor_a4_is_a_validation_failure():
    got = pdf_conform.check(_one_page_pdf(pagesize=(400, 400)))
    assert got["ok"] is False
    assert any("neither US Letter nor A4" in p for p in got["problems"])


def test_a4_is_accepted_in_either_orientation():
    from reportlab.lib.pagesizes import A4
    for size in (A4, (A4[1], A4[0])):
        assert not any("Letter" in p for p in pdf_conform.check(_one_page_pdf(size))["problems"])


def test_something_that_is_not_a_pdf_is_its_own_problem():
    got = pdf_conform.check(b"<html>not a pdf</html>")
    assert got["ok"] is False and got["problems"]


def test_a_fetched_copy_and_a_generated_paper_are_told_apart():
    assert pdf_conform.is_generated("01_DocumentList_and_Statements.pdf") is True
    assert pdf_conform.is_generated("50_Translation_Doc03_JP.pdf") is True
    assert pdf_conform.is_generated("40_Copy_Doc06_GB874600A.pdf") is False


def test_the_audit_blocks_on_a_paper_that_would_bounce_and_asks_only_the_owner_to_fix_it():
    report = {"01_DocumentList_and_Statements.pdf": {"ok": False,
                                                     "problems": ["2 fonts not embedded: Helvetica"]},
              "40_Copy_Doc06_GB.pdf": {"ok": False, "problems": ["it declares PDF 1.7"]}}
    got = {f.id: f for f in S._pdf_findings(report)}
    assert got["PDF-CONFORM"].status == S.BLOCKED
    assert "01_DocumentList" in got["PDF-CONFORM"].detail
    assert got["PDF-CONFORM-COPY"].status == S.ACTION
    assert "cannot be regenerated here" in got["PDF-CONFORM-COPY"].detail


def test_a_clean_report_says_the_audit_paper_is_covered_by_the_document_list():
    report = {"01_DocumentList_and_Statements.pdf": {"ok": True, "problems": []},
              "10_ConciseDescription_Doc01_X.pdf": {"ok": True, "problems": []}}
    got = {f.id: f for f in S._pdf_findings(report)}
    assert got["PDF-CONFORM"].status == S.OK
    assert "00_AUDIT.pdf" in got["PDF-CONFORM"].detail


def test_a_name_this_host_has_no_glyph_for_is_reported_rather_than_printed_as_boxes(monkeypatch):
    """reportlab substitutes ZapfDingbats for a missing glyph and "n" in ZapfDingbats is a filled
    square. CN 216190291 U's inventor went onto a filed document list as two black boxes."""
    monkeypatch.setattr(pdf_fonts, "ready", lambda: {pdf_fonts.SERIF: "/x.ttf"})
    docs = [{"n": 4, "pub": "CN-216190291-U", "rows": [],
             "biblio": {"inventor": "徐勇", "title": "", "assignee": "", "label": ""}}]
    assert S.unprintable_in(docs)


def test_a_host_with_the_fallback_face_prints_it_and_says_nothing():
    if pdf_fonts.FALLBACK not in pdf_fonts.ready():
        import pytest
        pytest.skip("fonts-droid-fallback is not installed on this host")
    docs = [{"n": 4, "pub": "CN-216190291-U", "rows": [],
             "biblio": {"inventor": "徐勇", "title": "", "assignee": "", "label": ""}}]
    assert S.unprintable_in(docs) == []


# --------------------------------------------------------------------------- inference


CYLINDER = "The magnet holder is annular and generally cylindrical in shape."


def test_an_inference_the_passage_does_not_carry_is_removed():
    text = ("The reference discloses a magnet holder that is generally cylindrical in shape, "
            "implying it extends along a longitudinal axis.")
    clean, changed = sc.strip_inference(text, CYLINDER)
    assert "longitudinal" not in clean
    assert changed and "inference" in changed[0]


def test_an_assertion_that_the_reference_meets_the_claim_term_always_goes():
    text = ("The reference discloses a magnetic device for removing parts therefrom, which "
            "constitutes a magnetic gripper system.")
    clean, changed = sc.strip_inference(text, "a magnetic gripper for removing parts therefrom")
    assert "constitutes" not in clean
    assert changed and "conclusion" in changed[0]


def test_a_restatement_the_passage_does_carry_is_left_alone():
    text = "The reference indicates the holder is annular and generally cylindrical in shape."
    clean, changed = sc.strip_inference(text, CYLINDER)
    assert changed == []
    assert clean == text.strip(" .").rstrip() or "annular" in clean


def test_a_teaches_row_has_no_passage_so_every_inference_on_it_is_the_drafters_own():
    text = "The arrangement thereby holds the workpiece against the pole face."
    _clean, changed = sc.strip_inference(text, "")
    assert changed


def test_the_audit_reports_an_inference_that_survived_a_hand_edit():
    docs = [{"n": 2, "rows": [{"quote": CYLINDER,
                               "disclosure": "It is generally cylindrical, implying it extends "
                                             "along a longitudinal axis."}]}]
    hits = S._inference_hits(docs)
    assert hits and "Doc 2" in hits[0]


def test_the_statutory_linter_alone_would_have_found_neither_of_them():
    """The point of the whole check: a linter hunting for "anticipates", "obvious" or "§ 103"
    matches nothing in either of the two statements that actually failed."""
    for text in ("It is generally cylindrical, implying it extends along a longitudinal axis.",
                 "a device for removing parts therefrom, which constitutes a magnetic gripper "
                 "system"):
        assert S._ARGUMENT.search(text) is None
        assert sc.strip_inference(text, "")[1]


# --------------------------------------------------------------------------- translations


def test_a_translation_is_only_accepted_for_the_member_it_translates(monkeypatch):
    """The acquisition ladder answers with whatever it resolved. Taking the first record whatever
    its key is how a translation of a sibling ends up attached to a document it does not
    translate: two publications of one application differ in text, in paragraph numbering and in
    claim count."""
    import sys
    import types
    fake = types.ModuleType("sources")
    fake.fetch_fulltext = lambda pubs, timeout=120.0: {
        "DE-102019131000-B4": {"description": "A magnetic gripper with two pole shoes.",
                               "claims": "1. A gripper.", "source": "google"}}
    monkeypatch.setitem(sys.modules, "sources", fake)
    assert sp.fetch_translation("DE-102019131000-A1") == {}
    got = sp.fetch_translation("DE-102019131000-B4")
    assert got and "magnetic gripper" in got["text"]


def test_a_word_broken_across_a_line_in_the_copy_still_matches():
    """A PDF text layer breaks lines where the typesetter did, so "magnet-\\nic" extracts as
    "magnet ic". A guard that fails on that cries wolf on every long quotation."""
    copy = {"text": "the rectilinear slid-\ning of the assem-\nbly in a direc-\ntion parallel to a "
                    "holding face provided by the edges of the pole plates"}
    assert sp.quotes_in_copy(copy, [QUOTE])["missing"] == []


def test_an_english_quotation_is_checked_against_the_translation_not_the_korean_copy():
    """Six of six quotations from a Korean publication "missing" from its Korean copy is a guard
    that teaches its reader to skip the line where a real defect is."""
    d = _doc(pub="KR-20200036441-A")
    d["biblio"]["country"] = "KR"
    copies = {"KR-20200036441-A": {"pages": 13, "chars": 7077, "drawings_only": False,
                                   "text": "자성 그리퍼 및 극 슈"}}
    only_copy = {f.id: f for f in S._copy_quote_findings([d], copies)}
    assert "COPY-QUOTES" not in only_copy, "the Korean copy cannot answer an English quotation"
    assert only_copy["COPY-QUOTES-TRANSLATION"].status == S.ACTION

    with_tr = {f.id: f for f in S._copy_quote_findings(
        [d], copies, {"KR-20200036441-A": {"text": "... " + QUOTE + " ..."}})}
    assert with_tr["COPY-QUOTES"].status == S.OK
    assert "COPY-QUOTES-TRANSLATION" not in with_tr


def test_a_quotation_missing_from_the_translation_is_still_named_as_missing():
    d = _doc(pub="KR-20200036441-A")
    d["biblio"]["country"] = "KR"
    got = {f.id: f for f in S._copy_quote_findings(
        [d], {"KR-20200036441-A": {"text": "x"}},
        {"KR-20200036441-A": {"text": "A magnetic gripper for lifting steel plates."}})}
    assert got["COPY-QUOTES"].status == S.ACTION
    assert "not in the translation" in got["COPY-QUOTES"].detail


def test_cutting_an_inference_never_leaves_a_sentence_fragment_behind():
    text = ("The reference discloses a pole shoe fastened to the housing. The device thereby holds "
            "the workpiece against the pole face.")
    clean, changed = sc.strip_inference(text, "")
    assert changed
    assert "The device." not in clean
    assert "pole shoe fastened to the housing" in clean
