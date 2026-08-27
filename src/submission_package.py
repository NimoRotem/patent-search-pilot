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

import citation                     # a translation has to be OF the member being filed
import concise_render
import pdf_fonts

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
        #  IT HAS TO BE A TRANSLATION OF THE MEMBER BEING FILED. The ladder answers with whatever
        #  it resolved, and taking the first record regardless of its key is how a translation of a
        #  sibling ends up attached to a document it does not translate. Two publications of one
        #  application differ in text, in paragraph numbering and in claim count, so filing one as
        #  the translation of the other is filing something the examiner cannot reconcile with the
        #  copy beside it. See citation.same_publication.
        if not citation.same_publication(key, pub):
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


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _squash(s):
    """Letters and digits only, no spaces at all.

    A PDF text layer breaks lines wherever the typesetter did, and a word broken across a line
    comes back hyphenated: "magnet-\\nic" extracts as "magnet ic" and never matches "magnetic".
    Removing every separator on both sides makes the comparison immune to that, to double spaces
    and to the soft hyphen, at the cost of ignoring word boundaries, which does not matter when the
    needle is a run of twelve words.
    """
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


#  Words of a quotation to probe for. The same window `submission_compliance.verify_quotes` uses,
#  because a stored passage is capped mid-sentence and a whole-string match would fail on the
#  ellipsis rather than on the copy.
_PROBE_WORDS = 12


def quotes_in_copy(copy, quotes):
    """Which of these quotations are NOT in the copy that will be filed. -> {"checked", "missing"}

    THE CHEAPEST CHECK IN THE PACKET, and counsel put it third on the build list because it catches
    the citation defects as well as the copy defects: "after assembling the packet, confirm that
    every quoted string is present in the text layer of the copy being filed. If the quote isn't in
    the copy, either the copy is wrong or the quote is."

    Both happened in one packet. The GB 874,600 copy was its six drawing sheets, so an examiner
    following any of the eight quotations attributed to it would have found pictures. And a
    quotation attributed to US 2022/0045594 A1 gave a numeric tolerance the document states
    qualitatively, so it was not in the copy either, for the opposite reason.

    A copy with no text layer answers "unknown", not "missing": that is a scan, and saying its
    quotations are absent would be as wrong as saying they are present. The audit reports the two
    separately.
    """
    out = {"checked": 0, "missing": [], "readable": False}
    hay = _squash((copy or {}).get("text") or "")
    if not hay:
        return out
    out["readable"] = True
    for q in quotes or []:
        q = str(q or "").strip()
        if not q:
            continue
        out["checked"] += 1
        probe = _squash(" ".join(_norm(q.rstrip(" ….")).split()[:_PROBE_WORDS]))
        if probe and probe not in hay:
            out["missing"].append(q)
    return out


def inspect_copy(blob):
    """What is actually in this copy. -> {"pages", "chars", "drawings_only", "text"}

    PRESENCE IS NOT COMPLETENESS, and the difference reached a filing. The copy attached for
    GB 874,600 A was six pages of figures whose own header reads "COMPLETE SPECIFICATION, 4 SHEETS,
    This drawing is a reproduction of the Original on a reduced scale": the drawing sheets alone,
    with no front page, no description and no claims. The concise description for that document
    quoted its abstract eight times, so an examiner checking a quotation against the filed copy
    would not have found it.

    A patent facsimile with no extractable text at all is either a pure image scan or a drawings
    bundle, and both need a human to look before they are filed as "the item".
    """
    out = {"pages": 0, "chars": 0, "drawings_only": False, "text": ""}
    if not blob:
        return out
    try:
        import io as _io

        from pypdf import PdfReader
        r = PdfReader(_io.BytesIO(blob))
        out["pages"] = len(r.pages)
        text = " ".join((p.extract_text() or "") for p in r.pages)
        out["chars"] = len("".join(text.split()))
        #  KEPT, because the audit checks every quotation against the copy that is actually going
        #  in the envelope. Capped so a 300-page facsimile cannot make the packet build expensive.
        out["text"] = text[:2_000_000]
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
        return out
    #  Under ~40 characters a page there is no specification text in here, whatever the page count.
    out["drawings_only"] = bool(out["pages"]) and out["chars"] < 40 * out["pages"]
    return out



def _styles():
    base = ParagraphStyle("sp", fontName=pdf_fonts.font(pdf_fonts.SERIF), fontSize=11, leading=13.5,
                          alignment=TA_LEFT, spaceAfter=0)
    return {
        "h": ParagraphStyle("h", parent=base, fontName=pdf_fonts.font(pdf_fonts.SERIF_BOLD), fontSize=11.5, leading=14,
                            spaceAfter=6),
        "app": ParagraphStyle("app", parent=base, fontName=pdf_fonts.font(pdf_fonts.SERIF_ITALIC), spaceAfter=10),
        "body": ParagraphStyle("body", parent=base, spaceBefore=4, spaceAfter=7),
        "th": ParagraphStyle("th", parent=base, fontName=pdf_fonts.font(pdf_fonts.SERIF_BOLD), fontSize=10, leading=12.5),
        "td": ParagraphStyle("td", parent=base, fontSize=10, leading=12.5),
        "note": ParagraphStyle("note", parent=base, fontSize=9.5, leading=12,
                               textColor=colors.HexColor("#333333"), spaceBefore=8),
    }


def _doc(buf, subject, title):
    running = concise_render.running_head(subject)

    def _page(canv, docobj):
        canv.saveState()
        canv.setFont(pdf_fonts.font(pdf_fonts.SERIF), 9.5)
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




def translation_pdf(doc, translation, subject) -> bytes:
    """An English machine translation of one listed non-English document."""
    st = _styles()
    b = doc.get("biblio") or {}
    buf = io.BytesIO()
    tmpl = _doc(buf, subject, "English translation of %s" % (b.get("label") or doc.get("pub")))
    story = [Paragraph("ENGLISH TRANSLATION: %s" % _esc(b.get("label") or doc.get("pub")),
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
