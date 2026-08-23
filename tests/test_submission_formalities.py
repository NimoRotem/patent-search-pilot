"""Five formal defects in filed 37 CFR 1.290 papers, each held shut here.

Reported 2026-08-23 against the package built for adhoc-66bbfcfff0bc. Everything below is about
what appears ON A PAPER FILED AT THE USPTO, so each test asserts the rendered artefact rather than
the intermediate model: a value that is right in the model and wrong on the page is still wrong.

1. Every document said `Re: U.S. App No. US 2026/0070232 A1`. That is the publication number under
   the words "application number". The application is 19/318,450, and the file wrapper this search
   read already knew it.
2. Docs 5 and 6 carried internal drafting notes, under a heading that asked the reader to delete
   them before filing. One of them read "5 of 14 quotations could not be found in the stored text
   of this reference and were removed", which states on the face of an Office paper that the
   process producing it failed its own source verification.
3. Doc 5 printed the Japanese inventor as black boxes. The romanised form was already in the
   record, one entry along.
4. The archive held concise descriptions and none of the other things 1.290(d) asks for.
5. Assignee and earliest priority date are not 1.290(e) identification fields, and the assignee
   values did not cleanly match the public record.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import concise_description as cd                                        # noqa: E402
import concise_render                                                   # noqa: E402
import submission_package as sp                                         # noqa: E402

SUBJECT = {"app_no": "19/318,450", "pub_no": "US 2026/0070232 A1", "title": "", "inventor": ""}


def _doc(**kw):
    b = {"pub": "US-2021031317-A1", "label": "U.S. Patent Application Publication No. "
                                              "US 2021/0031317 A1",
         "kind": "publication", "country": "US", "inventor": "David H. Morton",
         "assignee": "Some Assignee Inc", "title": "A gripper",
         "issue_date_pretty": "February 4, 2021", "priority_date_pretty": "March 1, 2019"}
    b.update(kw.pop("biblio", {}))
    d = {"n": 1, "pub": b["pub"], "biblio": b, "subject": SUBJECT, "summary": "It discloses a thing.",
         "rows": [{"claim_no": "1", "claim_text": "a gripper", "claim_paraphrase": "a gripper",
                   "quote_claim": False, "note": "discloses a gripper", "quote": "", "strong": False,
                   "cites": [], "disclosure": "discloses a gripper"}],
         "compliance": {"quotes": {"checked": 14, "dropped": 5,
                                   "note": "5 of 14 quotations could not be found in the stored "
                                           "text of this reference and were removed; the finding "
                                           "and its citation remain."},
                        "translation": {"translated": 0, "note": "This is a Japanese-language "
                                                                 "document."}}}
    d.update(kw)
    return d


def _pdf_text(blob):
    """Extract text without shelling out: the filed artefact is what is asserted, so read it."""
    try:
        from pypdf import PdfReader
    except Exception:                                                     # noqa: BLE001
        try:
            from PyPDF2 import PdfReader
        except Exception:
            pytest.skip("no PDF reader available")
    import io
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)


# ------------------------------------------------------------------ 1. the application number

def test_the_running_head_names_the_application_not_the_publication():
    assert concise_render.running_head(SUBJECT) == (
        "Re: U.S. Application No. 19/318,450 (Publication No. US 2026/0070232 A1)")


def test_a_publication_number_is_never_printed_as_an_application_number():
    """DEFECT INJECTION. With no application number the line must not silently relabel the
    publication, which is exactly what produced `Re: U.S. App No. US 2026/0070232 A1`."""
    head = concise_render.running_head({"app_no": "", "pub_no": "US 2026/0070232 A1"})
    assert "Application No. US 2026/0070232 A1" not in head
    assert head == "Re: U.S. Publication No. US 2026/0070232 A1"
    assert concise_render.running_head({}) == "Re: the above-identified application"


def test_the_application_number_is_formatted_the_way_the_office_writes_it():
    import webapp
    assert webapp._pretty_app_no("19318450") == "19/318,450"
    assert webapp._pretty_app_no("19/318,450") == "19/318,450"
    #  anything that is not an eight-digit serial comes back untouched rather than mangled
    assert webapp._pretty_app_no("PCT/US2020/012345") == "PCT/US2020/012345"
    assert webapp._pretty_app_no("") == ""


def test_the_head_reaches_the_rendered_page():
    text = _pdf_text(concise_render.to_pdf(_doc()))
    assert "U.S. Application No. 19/318,450" in text
    assert "App No. US 2026" not in text


# ------------------------------------------------------------------ 2. no internal notes on file

def test_the_notes_exist_but_never_reach_the_paper():
    d = _doc()
    notes = concise_render.filing_notes(d)
    assert any("could not be found" in t for _l, t in notes), (
        "the note is still computed, which is right: it belongs on the build page")
    text = _pdf_text(concise_render.to_pdf(d))
    for forbidden in ("could not be found in the stored text",
                      "Prepared for the practitioner",
                      "Filing notes",
                      "delete this block"):
        assert forbidden not in text, "%r was rendered into a filed PDF" % forbidden


def test_the_docx_is_clean_too():
    """The DOCX is the copy an attorney edits and files, so it is a filed artefact as much as the
    PDF is."""
    import io
    import zipfile
    blob = concise_render.to_docx(_doc())
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        body = z.read("word/document.xml").decode("utf8", "replace")
    for forbidden in ("could not be found in the stored text", "Prepared for the practitioner",
                      "Filing notes"):
        assert forbidden not in body, "%r was rendered into a filed DOCX" % forbidden


def test_the_build_page_shows_the_notes_instead():
    body = open(os.path.join(ROOT, "templates", "concise.html"), encoding="utf-8").read()
    assert "d.notes" in body, "the notes are on no paper and on no page, so they are simply gone"


# ------------------------------------------------------------------ 3. the inventor's name

def test_the_romanised_inventor_is_preferred_when_the_record_has_one():
    """The name was in the record all along, one entry along from the kanji."""
    disp = {"inventors": ["勇星 木村", "Yusei Kimura", "勇星 木村", "▲徳▼男 八木", "Tokuo Yagi"]}
    assert cd._first_inventor(disp) == "Yusei Kimura"


def test_a_name_that_has_no_latin_form_is_not_invented():
    """A kanji reading is ambiguous. Guessing one puts an unverified name on a filing, so the
    original is kept and the renderer's font problem is solved separately."""
    assert cd._first_inventor({"inventors": ["勇星 木村"]}) == "勇星 木村"
    assert cd._first_inventor({"inventors": []}) == ""


def test_the_latin_test_is_not_fooled_by_accents():
    assert cd._is_latin("Jörg Müller") and cd._is_latin("Renée Lévesque")
    assert not cd._is_latin("木村") and not cd._is_latin("金村")


def test_no_unrenderable_glyph_reaches_the_page():
    """END TO END: the filed PDF must contain the Latin name, not a row of boxes."""
    d = _doc(biblio={"pub": "JP-2019155534-A", "label": "JP 2019155534 A", "country": "JP",
                     "kind": "foreign", "inventor": "Yusei Kimura", "title": "Magnet gripper",
                     "issue_date_pretty": "September 19, 2019"})
    text = _pdf_text(concise_render.to_pdf(d))
    assert "Yusei Kimura" in text


# ------------------------------------------------------------------ 4. the package is complete

def test_only_non_us_documents_need_a_copy():
    assert not sp.needs_copy(_doc())
    assert sp.needs_copy(_doc(biblio={"pub": "JP-2019155534-A"}))
    assert not sp.needs_copy(_doc(biblio={"pub": "US-9260251-B2"}))


def test_only_non_english_documents_need_a_translation():
    assert not sp.needs_translation(_doc())
    assert sp.needs_translation(_doc(biblio={"pub": "JP-1-A", "country": "JP"}))
    assert sp.needs_translation(_doc(biblio={"pub": "DE-1-A1", "country": "DE"}))


def test_a_translation_that_is_not_in_english_is_refused(monkeypatch):
    """THE MOJIBAKE GUARD. The corpus copy of JP-2019155534-A is UTF-8 read as Latin-1, and filing
    that as an English translation would be worse than filing none."""
    import sources
    monkeypatch.setattr(sources, "fetch_fulltext", lambda pubs, timeout=None: {
        "JP1A": {"description": "本発明は磁性体であるワークを永久磁石の磁力により保持する",
                 "claims": "", "source": "corpus"}})
    assert sp.fetch_translation("JP-1-A") == {}

    monkeypatch.setattr(sources, "fetch_fulltext", lambda pubs, timeout=None: {
        "JP1A": {"description": "The present invention relates to a magnet gripper.",
                 "claims": "1. A magnet gripper.", "source": "gpatents_direct"}})
    got = sp.fetch_translation("JP-1-A")
    assert got and got["source"] == "gpatents_direct"


def test_the_document_list_marks_what_is_still_missing():
    docs = [_doc(n=1), _doc(n=2, biblio={"pub": "JP-2019155534-A", "label": "JP 2019155534 A",
                                         "country": "JP", "inventor": "Yusei Kimura"})]
    text = _pdf_text(sp.document_list(docs, SUBJECT, translations={}))
    assert "OUTSTANDING" in text
    assert "no copy required" in text


def test_the_package_never_claims_to_be_complete():
    docs = [_doc(n=1, biblio={"pub": "JP-1-A", "country": "JP", "label": "JP 1 A"})]
    note = sp.readme(docs, SUBJECT, translations={})
    assert "NOT A COMPLETE SUBMISSION" in note
    items = sp.outstanding(docs, {})
    assert any("legible copy" in i for i in items)
    assert any("translation" in i for i in items)
    assert any("Patent Center" in i for i in items)


def test_the_fee_paragraph_follows_the_document_count():
    one = _pdf_text(sp.statements([_doc()], SUBJECT))
    assert "1.290(g)" in one and "three or fewer" in one
    many = _pdf_text(sp.statements([_doc(n=i) for i in range(1, 8)], SUBJECT))
    assert "more than the three" in many


def test_both_statements_are_on_the_paper():
    text = _pdf_text(sp.statements([_doc()], SUBJECT))
    assert "1.290(d)(5)(i)" in text and "1.290(d)(5)(ii)" in text
    assert "duty to disclose" in text and "122(e)" in text


def test_the_zip_carries_the_whole_package():
    body = open(os.path.join(ROOT, "src", "webapp.py"), encoding="utf-8").read()
    assert "_concise_package(out, docs, subject)" in body, "the extra papers are never built"
    assert '_before_filing.txt' in body, "the zip drops the note saying what is missing"


# ------------------------------------------------------------------ 5. identification fields only

def test_assignee_and_priority_date_are_not_printed():
    text = _pdf_text(concise_render.to_pdf(_doc()))
    assert "Assignee" not in text, "assignment changes hands and is not a 1.290(e) field"
    assert "Earliest Priority Date" not in text
    #  and what 1.290(e) does ask for is still there
    assert "First Named Inventor" in text
    assert "Publication Date" in text or "Issue Date" in text
