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
import datetime
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






def test_the_zip_carries_the_whole_package():
    body = open(os.path.join(ROOT, "src", "webapp.py"), encoding="utf-8").read()
    assert "_concise_package(out, docs, subject, report=" in body, (
        "the extra papers are never built, or are built without the report the timing check needs")
    assert "READ_ME_FIRST.txt" in body, "the zip drops the note that carries the verdict"
    assert "00_AUDIT.pdf" in body, "the packet has no audit"


# ------------------------------------------------------------------ 5. identification fields only

def test_assignee_and_priority_date_are_not_printed():
    text = _pdf_text(concise_render.to_pdf(_doc()))
    assert "Assignee" not in text, "assignment changes hands and is not a 1.290(e) field"
    assert "Earliest Priority Date" not in text
    #  and what 1.290(e) does ask for is still there
    assert "First Named Inventor" in text
    assert "Publication Date" in text or "Issue Date" in text


def test_no_em_dash_reaches_a_paper_that_gets_filed():
    """An em dash was in the heading of every concise description filed at the Office, and in the
    PDF's own metadata title. It is a house rule that none of them ship, and the place to enforce
    it is the rendered bytes rather than a reviewer's eye.
    """
    import io

    import concise_md
    import concise_render as cr
    import submission as S

    docs = [{"n": 1, "pub": "JP-2007301640-A",
             "biblio": {"pub": "JP-2007301640-A", "label": "JP 2007301640 A", "kind": "foreign",
                        "country": "JP", "inventor": "Takeshi Ide",
                        "issue_date_pretty": "November 22, 2007",
                        "title": "Faceplate for magnetic chuck"},
             "summary": "It discloses a faceplate for a magnetic chuck.",
             "rows": [{"claim_no": "1", "claim_text": "a magnet in a housing",
                       "quote_claim": "claim 1", "prose": "The reference discloses a magnet.",
                       "quote": "a magnet is arranged in the housing", "location": "[0004]"}],
             "compliance": {}}]
    subject = {"app_no": "19/318,450", "pub_no": "US 2026/0070232 A1",
               "title": "Magnetic gripper", "inventor": "A Inventor"}
    docs[0]["subject"] = subject
    win = S.window("2026-03-12", today=datetime.date(2026, 8, 24))

    def _text(blob):
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(blob))
        meta = " ".join(str(v) for v in (r.metadata or {}).values())
        return meta + "\n" + "\n".join((p.extract_text() or "") for p in r.pages)

    import submission_package as sp

    papers = {
        "the concise description": _text(cr.to_pdf(docs[0])),
        "its markdown": concise_md.to_markdown(docs[0]),
        "the audit": _text(S.audit_pdf(S.audit(docs, subject, {}, {}, win), docs, subject, win)),
        "the document list": _text(S.document_list_and_statements(docs, subject, {}, {}, win)),
        "the manifest": S.manifest_csv(docs, {}, {}),
        "the subject line": cr.subject_line(subject),
        "the running head": cr.running_head(subject),
        #  The translation cover is a paper we write around somebody else's text, and its own
        #  heading carried one. The translated passage is quoted as it came and is not ours to
        #  edit, so only the chrome is asserted here.
        "the translation cover": _text(sp.translation_pdf(
            docs[0], {"rows": [{"id": 1, "english": "a magnet is arranged in the housing"}],
                      "engine": "test"}, subject)),
    }
    for what, text in papers.items():
        for dash in ("—", "–"):
            assert dash not in text, "%s carries %r" % (what, dash)


# ------------------------------------------------------------- 6. the fonts on the filed page

def _fonts_of(blob):
    """{BaseFont: is it embedded} for every font resource on every page."""
    import io
    from pypdf import PdfReader
    out = {}
    for pg in PdfReader(io.BytesIO(blob)).pages:
        res = (pg.get("/Resources") or {}).get("/Font") or {}
        try:
            res = res.get_object()
        except Exception:                                                 # noqa: BLE001
            pass
        for k in res:
            f = res[k].get_object()
            d = f.get("/FontDescriptor")
            out[str(f.get("/BaseFont"))] = bool(d) and any(
                x in d.get_object() for x in ("/FontFile", "/FontFile2", "/FontFile3"))
    return out


def _cjk_doc():
    """The real CN 216190291 U shape: a Chinese-only inventor and a Latin applicant."""
    return _doc(n=3, biblio={
        "pub": "CN-216190291-U", "label": "CN 216190291 U", "kind": "foreign", "country": "CN",
        "inventor": "徐勇", "assignee": "Hubei Sanliu Heavy Industries Co., Ltd.",
        "title": "永磁起重器", "issue_date_pretty": "April 5, 2022"})


def _filed_papers():
    """One of each paper that goes to the Office, with a document that exercises the fallback."""
    import submission as S
    import submission_package as sp
    d = _cjk_doc()
    win = S.window("2026-03-12", today=datetime.date(2026, 8, 24))
    return {
        "concise description": concise_render.to_pdf(d),
        "audit": S.audit_pdf(S.audit([d], SUBJECT, {}, {}, win), [d], SUBJECT, win),
        "document list": S.document_list_and_statements([d], SUBJECT, {}, {}, win),
        "translation": sp.translation_pdf(
            d, {"rows": [{"id": 1, "english": "a magnet in the housing"}], "engine": "t"},
            SUBJECT),
    }


def test_this_host_can_embed_every_face_a_filing_needs():
    """A box with no font files renders a filing on the base-14 and it bounces at upload. That is
    an environment fault, not a code one, so it is said here rather than discovered at Patent
    Center."""
    import pdf_fonts
    assert pdf_fonts.missing() == [], (
        "install the fonts: %s" % pdf_fonts.missing())


def test_every_filed_pdf_embeds_all_of_its_fonts():
    """Patent Center's PDF guidelines list an unembedded font as a validation failure, and
    reportlab's default faces are the base-14, which are never embedded. Measured on the packet
    for adhoc-efbf2979420b: seventeen of twenty-one papers would have bounced.

    A Table is the trap. Its cells here are all Paragraphs, so every style names an embedded face,
    and the Table STILL emitted its own default cell font. So did the canvas.
    """
    for what, blob in _filed_papers().items():
        fonts = _fonts_of(blob)
        assert fonts, "%s has no font resources at all" % what
        unembedded = sorted(n for n, emb in fonts.items() if not emb)
        assert not unembedded, "%s carries %s" % (what, unembedded)


def test_a_name_the_latin_face_cannot_draw_is_not_printed_as_boxes():
    """Asked for a glyph it does not have, reportlab substitutes ZapfDingbats. 徐勇 went onto a
    filed document list as ■■, which leaves the 1.290(e)(4) identification blank."""
    for what, blob in _filed_papers().items():
        text = _pdf_text(blob)
        for glyph in ("■", "�"):
            assert glyph not in text, "%s printed %r" % (what, glyph)


def test_the_paper_identifies_the_party_by_a_name_it_can_print():
    """1.290(e)(3) accepts the applicant, the patentee OR the first named inventor, and that OR is
    the way out: the applicant carries a Latin name where all seven inventors do not."""
    text = _pdf_text(concise_render.to_pdf(_cjk_doc()))
    assert "Hubei Sanliu" in text
    assert "Applicant" in text


def test_cjk_that_has_no_alternative_still_renders_and_still_extracts():
    """A title or a quoted passage has no applicant to fall back on, so it has to be drawn. It
    also has to come back out: a Table cell in a face with no CJK glyphs dropped the characters
    from the text layer entirely, which is a paper that looks right and is not searchable."""
    text = _pdf_text(concise_render.to_pdf(_cjk_doc()))
    assert "永磁起重器" in text, "the CJK title did not survive into the text layer"


def test_the_fallback_only_wraps_what_it_has_to():
    """Wrapping everything would work and would also make every paper a CJK font embedding. The
    span is only around the runs the Latin face cannot draw."""
    import pdf_fonts
    assert pdf_fonts.with_fallback("Takeshi Ide") == "Takeshi Ide"
    assert pdf_fonts.with_fallback("Jörg Müller, Renée Lévesque") == "Jörg Müller, Renée Lévesque"
    out = pdf_fonts.with_fallback("Inventor: 徐勇 (CN)")
    assert out.startswith("Inventor: <font face=") and out.endswith("</font> (CN)")
    #  and it steps over the markup the caller already escaped rather than splitting an entity
    assert pdf_fonts.with_fallback("A &amp; B") == "A &amp; B"
    assert "&amp;" in pdf_fonts.with_fallback("徐 &amp; 勇")


def test_a_character_the_source_scan_could_not_read_is_named_not_edited():
    """Google's OCR of a 1986 Japanese publication put a solid black square mid-sentence, and it
    went onto a filed paper looking exactly like a rendering failure of ours. It is not: it is
    what the machine translation says. Editing a translation to look tidier is the one response
    that would actually be wrong, so the audit names it and the paper carries it as it came.
    """
    import submission as S

    d = _doc(n=4, biblio={"pub": "JP-S6165742-A", "label": "JP S61-65742 A", "country": "JP",
                          "kind": "foreign", "inventor": "A Inventor",
                          "issue_date_pretty": "April 4, 1986"})
    win = S.window("2026-03-12", today=datetime.date(2026, 8, 24))
    dirty = {"JP-S6165742-A": {"text": "the plate jr, ■, and :jS2 at intersections",
                               "claims": "", "engine": "t"}}
    clean = {"JP-S6165742-A": {"text": "the plate and the bracket at intersections",
                               "claims": "", "engine": "t"}}

    f = {x.id: x for x in S.audit([d], SUBJECT, {}, dirty, win)}
    assert "TRANSLATION-OCR" in f, "an unreadable character on a filed paper went unmentioned"
    assert f["TRANSLATION-OCR"].status == S.NOTE, "it is a thing to read, not a thing to fix"
    assert "Doc 4 (JP S61-65742 A): 1" in f["TRANSLATION-OCR"].detail
    assert "not in this rendering" in f["TRANSLATION-OCR"].detail, (
        "say whose fault it is, or somebody spends an hour on the renderer")

    assert "TRANSLATION-OCR" not in {x.id for x in S.audit([d], SUBJECT, {}, clean, win)}
    #  and the translation still has to be attached at all, which is the (d)(4) finding proper
    assert f["TRANSLATION"].status == S.OK
