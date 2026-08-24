"""Fetching the two attachments a 1.290 submission needs: the legible copy and the translation.

SUPERSEDED IN PART. The document list, the statements, the fee position and the audit moved to
`submission.py`, which builds them from the rule rather than from a template, because the rule is
the specification and the packet has to be checked against it rather than merely shaped like it.
What is left here is the acquisition: getting the copy and the translation, which is I/O and has
nothing to do with the paperwork.

Original note, still true:

The rest of a 37 CFR 1.290 submission: the document list, the statements, and translations.

WHAT WAS MISSING. The archive held concise descriptions and nothing else, which is one of the
several things 1.290(d) requires. A practitioner downloading it had a folder of well-formed
descriptions and no way to tell that the submission was not yet a submission.

1.290(d) asks for a document list, a concise description of the relevance of each listed item, a
legible copy of each listed item OTHER than a U.S. patent or U.S. patent application publication,
an English translation of any non-English item, and the two statements by the submitting party.
Patent Center collects the list and the statements through its own workflow, so those are produced
here as papers to check and to file behind, not as a claim that the workflow has been completed.

The one gap that nothing else can close is the non-English reference: a Japanese publication needs
a legible copy and an English translation, and a reliable machine translation is expressly
acceptable. That translation is fetched from the same Google Patents route the corpus acquisition
uses, which returns English machine translations for JP, CN and KR, and it is labelled as a machine
translation on its face because that is what it is.

NOTHING HERE ASSERTS THAT THE PACKAGE IS COMPLETE. `outstanding()` returns what a human still has
to supply, the README says it in words, and the document list prints OUTSTANDING against any item
whose copy or translation could not be produced. A package that quietly looks finished is worse
than one that says what is missing.
"""
from __future__ import annotations

import io
import re
import traceback

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)

import concise_render

#  A U.S. patent or U.S. pre-grant publication needs no copy: 1.290(d)(3) excludes exactly those.
_US_PUB = re.compile(r"^US", re.I)


def needs_copy(doc):
    """True when 1.290(d)(3) requires a legible copy of this item to be filed with it."""
    pub = str((doc.get("biblio") or {}).get("pub") or doc.get("pub") or "")
    return not _US_PUB.match(pub.strip())


def needs_translation(doc):
    """True when the listed item is not in English.

    Keyed on the ISSUING OFFICE rather than on a language guess over the text we hold: the text in
    the corpus for a JP publication may already be somebody's translation, and "this text looks
    English" is not the question 1.290(d)(4) asks.
    """
    b = doc.get("biblio") or {}
    country = str(b.get("country") or "").upper()[:2]
    return country in ("JP", "CN", "KR", "DE", "FR", "ES", "IT", "RU", "TW", "BR", "PT", "NL",
                       "SE", "DK", "FI", "NO", "PL", "TR")


def fetch_translation(pub, timeout=120.0):
    """An English machine translation of the listed document. -> {"text", "claims", "source"} or {}

    Uses the acquisition ladder rather than the corpus copy, and that is deliberate: MEASURED on
    JP-2019155534-A, the corpus paragraph text is mojibake, UTF-8 bytes decoded as Latin-1, while
    the same document fetched live comes back as 15,901 characters of clean English in under a
    second. Filing a translation built from the corpus copy would have filed mojibake.
    """
    try:
        import sources
        got = sources.fetch_fulltext([pub], timeout=timeout) or {}
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return {}
    for key, rec in got.items():
        if key == "_summary" or not isinstance(rec, dict):
            continue
        text = str(rec.get("description") or "")
        claims = rec.get("claims") or ""
        if isinstance(claims, list):
            claims = "\n".join(str(c) for c in claims)
        if not text.strip() and not str(claims).strip():
            continue
        #  A translation that is not in the Latin script is not a translation. This is the check
        #  that stops the mojibake case, and any other source that answers in the original.
        body = text or str(claims)
        latin = sum(1 for c in body if ord(c) < 0x2E80)
        if latin < 0.9 * max(len(body), 1):
            return {}
        return {"text": text, "claims": str(claims), "source": str(rec.get("source") or "")}
    return {}


def fetch_copy(pub):
    """A legible copy of the publication, as PDF bytes, or b"". Never raises.

    1.290(d)(3) wants the document itself for anything that is not a U.S. patent or U.S. patent
    application publication. The corpus already holds facsimiles for a large part of the niche, so
    this is usually a disk read: JP-2019155534-A is a seven-page A4 copy already on this box.
    """
    try:
        import enrich_display
        canon = enrich_display._canonical_pubkey(pub)
        path = enrich_display.PDFDIR / ("%s.pdf" % canon)
        if path.exists() and path.stat().st_size > 2048:
            blob = path.read_bytes()
            #  A truncated or HTML-error body saved with a .pdf name is not a legible copy.
            return blob if blob[:5] == b"%PDF-" else b""
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
    return b""


def inspect_copy(blob):
    """What is actually in this copy. -> {"pages", "chars", "drawings_only"}

    PRESENCE IS NOT COMPLETENESS, and the difference reached a filing. The copy attached for
    GB 874,600 A was six pages of figures whose own header reads "COMPLETE SPECIFICATION, 4 SHEETS,
    This drawing is a reproduction of the Original on a reduced scale": the drawing sheets alone,
    with no front page, no description and no claims. The concise description for that document
    quoted its abstract eight times, so an examiner checking a quotation against the filed copy
    would not have found it.

    A patent facsimile with no extractable text at all is either a pure image scan or a drawings
    bundle, and both need a human to look before they are filed as "the item".
    """
    out = {"pages": 0, "chars": 0, "drawings_only": False}
    if not blob:
        return out
    try:
        import io as _io

        from pypdf import PdfReader
        r = PdfReader(_io.BytesIO(blob))
        out["pages"] = len(r.pages)
        text = " ".join((p.extract_text() or "") for p in r.pages)
        out["chars"] = len("".join(text.split()))
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return out
    #  Under ~40 characters a page there is no specification text in here, whatever the page count.
    out["drawings_only"] = bool(out["pages"]) and out["chars"] < 40 * out["pages"]
    return out


def outstanding(docs, translations):
    """What a human still has to supply before this can be filed. -> [str]"""
    out = []
    for d in docs:
        n, label = d.get("n"), (d.get("biblio") or {}).get("label") or d.get("pub")
        if needs_copy(d):
            out.append("Document %s (%s): a legible copy of the publication itself. It is not a "
                       "U.S. patent or U.S. patent application publication, so 1.290(d)(3) "
                       "requires the copy to be filed with the submission." % (n, label))
        if needs_translation(d) and not translations.get(d.get("pub")):
            out.append("Document %s (%s): an English translation. One could not be produced "
                       "automatically, so it has to be obtained and attached." % (n, label))
    out.append("The document list and the two statements under 1.290(d)(5) are entered through "
               "Patent Center's own workflow. The papers in this archive are for checking against "
               "what is entered there.")
    out.append("The fee under 37 CFR 1.290(f), or the exemption under 1.290(g), is settled in "
               "Patent Center at the time of filing.")
    return out


# --------------------------------------------------------------------------- rendering


def _styles():
    base = ParagraphStyle("sp", fontName="Times-Roman", fontSize=11, leading=13.5,
                          alignment=TA_LEFT, spaceAfter=0)
    return {
        "h": ParagraphStyle("h", parent=base, fontName="Times-Bold", fontSize=11.5, leading=14,
                            spaceAfter=6),
        "app": ParagraphStyle("app", parent=base, fontName="Times-Italic", spaceAfter=10),
        "body": ParagraphStyle("body", parent=base, spaceBefore=4, spaceAfter=7),
        "th": ParagraphStyle("th", parent=base, fontName="Times-Bold", fontSize=10, leading=12.5),
        "td": ParagraphStyle("td", parent=base, fontSize=10, leading=12.5),
        "note": ParagraphStyle("note", parent=base, fontSize=9.5, leading=12,
                               textColor=colors.HexColor("#333333"), spaceBefore=8),
    }


def _doc(buf, subject, title):
    running = concise_render.running_head(subject)

    def _page(canv, docobj):
        canv.saveState()
        canv.setFont("Times-Roman", 9.5)
        canv.setFillColor(colors.HexColor("#333333"))
        canv.drawString(inch, letter[1] - 0.62 * inch, running)
        canv.drawString(inch, 0.62 * inch, running)
        canv.restoreState()

    tmpl = BaseDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch,
                           topMargin=0.95 * inch, bottomMargin=0.95 * inch, title=title,
                           author="Third-party submission under 37 CFR 1.290")
    frame = Frame(inch, 0.95 * inch, letter[0] - 2 * inch, letter[1] - 1.9 * inch, id="f")
    tmpl.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=_page)])
    return tmpl


def _esc(s):
    return concise_render._esc(s)


def document_list(docs, subject, translations=None) -> bytes:
    """The 1.290(d)(1) listing: every item, identified the way the rule identifies it."""
    translations = translations or {}
    st = _styles()
    buf = io.BytesIO()
    tmpl = _doc(buf, subject, "Document list, 37 CFR 1.290(d)(1)")
    story = [Paragraph("DOCUMENT LIST &mdash; THIRD-PARTY SUBMISSION UNDER 37 CFR &sect; 1.290",
                       st["h"]),
             Paragraph(_esc(concise_render.subject_line(subject)), st["app"])]

    head = [Paragraph("No.", st["th"]), Paragraph("Document", st["th"]),
            Paragraph("First named inventor", st["th"]), Paragraph("Date", st["th"]),
            Paragraph("Copy / translation", st["th"])]
    data = [head]
    for d in docs:
        b = d.get("biblio") or {}
        bits = []
        if needs_copy(d):
            bits.append("copy required: OUTSTANDING")
        else:
            bits.append("no copy required")
        if needs_translation(d):
            bits.append("translation attached" if translations.get(d.get("pub"))
                        else "translation required: OUTSTANDING")
        data.append([Paragraph(str(d.get("n")), st["td"]),
                     Paragraph(_esc(b.get("label") or d.get("pub")), st["td"]),
                     Paragraph(_esc(b.get("inventor") or ""), st["td"]),
                     Paragraph(_esc(b.get("issue_date_pretty") or ""), st["td"]),
                     Paragraph(_esc("; ".join(bits)), st["td"])])
    w = letter[0] - 2 * inch
    tbl = Table(data, colWidths=[w * 0.06, w * 0.30, w * 0.22, w * 0.18, w * 0.24], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#444444")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EFEFEF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tbl)
    story.append(Paragraph(
        "A U.S. patent and a U.S. patent application publication need no copy: 37 CFR "
        "1.290(d)(3) requires a legible copy of each listed item <i>other than</i> those. Any "
        "item above marked OUTSTANDING has to be attached before this is filed.", st["note"]))
    tmpl.build(story)
    return buf.getvalue()


_STATEMENTS = [
    ("Statement under 37 CFR 1.290(d)(5)(i)",
     "The party making this submission is not an individual who has a duty to disclose "
     "information with respect to the above-identified application under 37 CFR 1.56."),
    ("Statement under 37 CFR 1.290(d)(5)(ii)",
     "This submission complies with the requirements of 35 U.S.C. 122(e) and 37 CFR 1.290."),
]


def statements(docs, subject) -> bytes:
    """The 1.290(d)(5) statements, and where the fee question stands."""
    st = _styles()
    buf = io.BytesIO()
    tmpl = _doc(buf, subject, "Statements, 37 CFR 1.290(d)(5)")
    story = [Paragraph("STATEMENTS &mdash; THIRD-PARTY SUBMISSION UNDER 37 CFR &sect; 1.290",
                       st["h"]),
             Paragraph(_esc(concise_render.subject_line(subject)), st["app"])]
    for title, text in _STATEMENTS:
        story.append(Paragraph(_esc(title), st["th"]))
        story.append(Paragraph(_esc(text), st["body"]))

    n = len(docs)
    story.append(Paragraph("Fee", st["th"]))
    if n <= 3:
        story.append(Paragraph(
            "This submission lists %d document%s. 37 CFR 1.290(g) exempts a submission listing "
            "three or fewer total items from the fee, where it is the first submission in this "
            "application by this party or a party in privity with this party and is accompanied "
            "by a statement to that effect. <b>Confirm that this is the first such submission "
            "before relying on the exemption</b>, and make the statement in Patent Center."
            % (n, "" if n == 1 else "s"), st["body"]))
    else:
        story.append(Paragraph(
            "This submission lists %d documents, which is more than the three that 37 CFR "
            "1.290(g) exempts, so the fee under 37 CFR 1.290(f) applies. The amount is set by "
            "37 CFR 1.17 and is calculated and paid in Patent Center at the time of filing."
            % n, st["body"]))
    story.append(Paragraph(
        "These statements are reproduced here so they can be read and checked. They are made to "
        "the Office through Patent Center's own workflow, which is where the document list and "
        "the fee are also entered.", st["note"]))
    tmpl.build(story)
    return buf.getvalue()


def translation_pdf(doc, translation, subject) -> bytes:
    """An English machine translation of one listed non-English document."""
    st = _styles()
    b = doc.get("biblio") or {}
    buf = io.BytesIO()
    tmpl = _doc(buf, subject, "English translation of %s" % (b.get("label") or doc.get("pub")))
    story = [Paragraph("ENGLISH TRANSLATION &mdash; %s" % _esc(b.get("label") or doc.get("pub")),
                       st["h"]),
             Paragraph(_esc(concise_render.subject_line(subject)), st["app"])]
    story.append(Paragraph(
        "This is a <b>machine translation</b> of %s, produced by Google Patents and retrieved on "
        "the date of this submission. It is furnished under 37 CFR 1.290(d)(4) as the English "
        "language translation of a non-English language item of information. The original "
        "publication governs; where the translation and the original differ, the original "
        "controls." % _esc(b.get("label") or doc.get("pub")), st["body"]))
    if b.get("title"):
        story.append(Paragraph("<b>Title:</b> %s" % _esc(b["title"]), st["body"]))
    story.append(Spacer(1, 6))

    claims = " ".join(str(translation.get("claims") or "").split())
    if claims:
        story.append(Paragraph("Claims", st["th"]))
        for part in _paragraphs(translation.get("claims") or ""):
            story.append(Paragraph(_esc(part), st["td"]))
        story.append(Spacer(1, 8))
    if translation.get("text"):
        story.append(Paragraph("Description", st["th"]))
        for part in _paragraphs(translation["text"]):
            story.append(Paragraph(_esc(part), st["td"]))
    tmpl.build(story)
    return buf.getvalue()


def _paragraphs(text, limit=4000):
    """Split into renderable paragraphs. A single 16,000-character string is one flowable that
    reportlab cannot split across pages, and it silently drops off the end of the frame."""
    out = []
    for raw in re.split(r"\n\s*\n|\n", str(text or "")):
        s = " ".join(raw.split())
        while len(s) > limit:
            cut = s.rfind(" ", 0, limit) or limit
            out.append(s[:cut])
            s = s[cut:].lstrip()
        if s:
            out.append(s)
    return out or [""]


def readme(docs, subject, translations) -> str:
    lines = [
        "THIRD-PARTY PREISSUANCE SUBMISSION UNDER 37 CFR 1.290",
        concise_render.running_head(subject).replace("Re: ", ""),
        "",
        "THIS ARCHIVE IS NOT A COMPLETE SUBMISSION ON ITS OWN. It contains the papers that can be",
        "produced from the search: a concise description of relevance for each listed document, a",
        "document list, the statements, and a machine translation of each non-English document",
        "where one could be obtained.",
        "",
        "What it contains:",
    ]
    lines.append("  00_DocumentList.pdf          the 1.290(d)(1) listing")
    lines.append("  01_Statements_and_Fee.pdf    the 1.290(d)(5) statements and the fee position")
    for d in docs:
        b = d.get("biblio") or {}
        lines.append("  ConciseDescription_Doc%-3s   %s" % (d.get("n"), b.get("label") or d.get("pub")))
        if translations.get(d.get("pub")):
            lines.append("  Translation_Doc%-3s          English machine translation of the same"
                         % d.get("n"))
    lines += ["", "Still to be supplied by the practitioner:"]
    for item in outstanding(docs, translations):
        lines.append("  - %s" % item)
    lines += ["", "A machine translation is expressly acceptable under 1.290(d)(4). The one here "
                  "is labelled as", "one on its face."]
    return "\n".join(lines) + "\n"
