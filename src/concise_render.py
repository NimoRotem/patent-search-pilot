"""Render a concise description of relevance to the two formats the USPTO accepts on filing.

PDF is the filing artefact (Patent Center takes PDF for a 37 CFR 1.290 submission) and DOCX is the
editable copy the attorney marks up before filing. Both carry the same content from the same model,
so the paper that gets filed is the paper that was reviewed.

Layout follows the attorney's own examples: a running "Re: U.S. App No. ..." line, the statutory
heading, the application identification, the document's biblio block, a one-paragraph
characterisation, then the two-column table. Letter paper, 1 inch margins, 11pt serif — the
formalities a paper has to satisfy to be accepted rather than returned.
"""
from __future__ import annotations

import html as _html
import io
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph, Table,
                                TableStyle)

HEADING = ("CONCISE DESCRIPTION OF RELEVANCE — THIRD-PARTY SUBMISSION "
           "UNDER 37 CFR § 1.290")


def _esc(s):
    return _html.escape(str(s or ""))


def subject_line(subject):
    """U.S. Application No. 18/915,337 (US 2025/0033224 A1) — "Title" — Inventor."""
    bits = []
    if subject.get("app_no"):
        bits.append("U.S. Application No. %s" % subject["app_no"])
    if subject.get("pub_no"):
        bits.append("(%s)" % subject["pub_no"] if bits else subject["pub_no"])
    head = " ".join(bits)
    tail = []
    if subject.get("title"):
        tail.append("“%s”" % subject["title"])
    if subject.get("inventor"):
        tail.append(subject["inventor"])
    return " — ".join([x for x in [head] + tail if x])


def _left_cell(row, pub_no_label):
    if row["quote_claim"] and row["claim_text"]:
        return "Claim %s: “%s”" % (row["claim_no"], row["claim_text"])
    para = row["claim_paraphrase"] or row["claim_text"]
    return "Claim %s (%s)" % (row["claim_no"], para)


def _right_bits(row):
    """(prose, [citations]) — the quotation is only used where a passage survived grounding."""
    prose = row.get("disclosure") or row.get("note") or ""
    if row.get("strong") and row.get("quote"):
        #  The stored passage is capped mid-word by the reader's character budget. Filed text may
        #  be elided, but it may not look like a transcription error, so cut back to the last whole
        #  word and mark the elision. 40 words is the cap the reader was asked to quote within.
        q = " ".join(row["quote"].split())
        words = q.split()
        elided = len(words) > 40
        if elided:
            words = words[:40]
        if words and not re.search(r"[.!?\"\u201d)]$", words[-1]):
            elided = True
        q = " ".join(words)
        if elided:
            q = q.rstrip(" ,;:-") + " …"
        prose = "%s\n“%s”" % (prose, q) if prose else "“%s”" % q
    return prose, list(row.get("cites") or [])


# --------------------------------------------------------------------------- PDF


def _styles():
    base = ParagraphStyle("cd", fontName="Times-Roman", fontSize=11, leading=13.5,
                          alignment=TA_LEFT, spaceAfter=0)
    return {
        "h": ParagraphStyle("h", parent=base, fontName="Times-Bold", fontSize=11.5, leading=14,
                            spaceAfter=6),
        "app": ParagraphStyle("app", parent=base, fontName="Times-Italic", spaceAfter=8),
        "doc": ParagraphStyle("doc", parent=base, fontName="Times-Bold", spaceAfter=3),
        "bib": ParagraphStyle("bib", parent=base, leftIndent=20, spaceAfter=1),
        "body": ParagraphStyle("body", parent=base, spaceBefore=6, spaceAfter=9),
        "th": ParagraphStyle("th", parent=base, fontName="Times-Bold", fontSize=10.5, leading=13),
        "td": ParagraphStyle("td", parent=base, fontSize=10.5, leading=13),
        "cite": ParagraphStyle("cite", parent=base, fontSize=10.5, leading=13, leftIndent=10,
                               bulletIndent=2, spaceBefore=1),
        "run": ParagraphStyle("run", parent=base, fontSize=9.5, leading=11,
                              textColor=colors.HexColor("#333333")),
    }


def to_pdf(doc_model) -> bytes:
    st = _styles()
    subj = doc_model["subject"]
    running = "Re: U.S. App No. %s" % (subj.get("app_no") or subj.get("pub_no") or "")
    buf = io.BytesIO()

    def _page(canv, docobj):
        canv.saveState()
        canv.setFont("Times-Roman", 9.5)
        canv.setFillColor(colors.HexColor("#333333"))
        canv.drawString(inch, letter[1] - 0.62 * inch, running)
        canv.drawString(inch, 0.62 * inch, running)
        canv.restoreState()

    tmpl = BaseDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch,
                           topMargin=0.95 * inch, bottomMargin=0.95 * inch,
                           title="Concise Description of Relevance — %s" % doc_model["pub"],
                           author="Third-party submission under 37 CFR 1.290")
    frame = Frame(inch, 0.95 * inch, letter[0] - 2 * inch, letter[1] - 1.9 * inch, id="f")
    tmpl.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=_page)])

    story = [Paragraph(_esc(HEADING), st["h"]),
             Paragraph(_esc(subject_line(subj)), st["app"])]

    b = doc_model["biblio"]
    story.append(Paragraph("Document %s: %s (“Document %s”)" % (
        doc_model["n"], _esc(b["label"]), doc_model["n"]), st["doc"]))
    for lbl, val in (("First Named Inventor", b.get("inventor")),
                     ("Assignee", b.get("assignee")),
                     ("Issue Date" if b.get("kind") == "patent" else "Publication Date",
                      b.get("issue_date_pretty")),
                     ("Title", b.get("title")),
                     ("Earliest Priority Date", b.get("priority_date_pretty"))):
        if val:
            story.append(Paragraph("<i>%s:</i> %s" % (_esc(lbl), _esc(val)), st["bib"]))

    story.append(Paragraph(
        _esc(doc_model["summary"]) +
        " Its potential relevance to the claims of the above-identified application is set forth "
        "below.", st["body"]))

    pub_no = subj.get("pub_no") or subj.get("app_no") or ""
    head = [Paragraph("Claim language (%s)" % _esc(pub_no), st["th"]),
            Paragraph("Relevant disclosure of this document", st["th"])]
    data = [head]
    for row in doc_model["rows"]:
        prose, cites = _right_bits(row)
        right = [Paragraph(_esc(prose).replace("\n", "<br/>"), st["td"])]
        for c in cites:
            right.append(Paragraph("• %s" % _esc(c), st["cite"]))
        data.append([Paragraph(_esc(_left_cell(row, pub_no)), st["td"]), right])

    w = letter[0] - 2 * inch
    tbl = Table(data, colWidths=[w * 0.46, w * 0.54], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#444444")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EFEFEF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)
    tmpl.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------- DOCX


def to_docx(doc_model) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    subj = doc_model["subject"]
    d = Document()
    s = d.sections[0]
    s.page_width, s.page_height = Inches(8.5), Inches(11)
    for m in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(s, m, Inches(1))
    normal = d.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")

    running = "Re: U.S. App No. %s" % (subj.get("app_no") or subj.get("pub_no") or "")
    for holder in (s.header, s.footer):
        p = holder.paragraphs[0]
        p.text = running
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        for r in p.runs:
            r.font.size = Pt(9.5)

    h = d.add_paragraph()
    h.add_run(HEADING).bold = True
    ap = d.add_paragraph()
    ap.add_run(subject_line(subj)).italic = True

    b = doc_model["biblio"]
    dp = d.add_paragraph()
    dp.add_run("Document %s: %s (“Document %s”)" % (
        doc_model["n"], b["label"], doc_model["n"])).bold = True
    for lbl, val in (("First Named Inventor", b.get("inventor")),
                     ("Assignee", b.get("assignee")),
                     ("Issue Date" if b.get("kind") == "patent" else "Publication Date",
                      b.get("issue_date_pretty")),
                     ("Title", b.get("title")),
                     ("Earliest Priority Date", b.get("priority_date_pretty"))):
        if val:
            p = d.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.28)
            p.paragraph_format.space_after = Pt(1)
            p.add_run("%s: " % lbl).italic = True
            p.add_run(str(val))

    d.add_paragraph(doc_model["summary"] +
                    " Its potential relevance to the claims of the above-identified application "
                    "is set forth below.")

    pub_no = subj.get("pub_no") or subj.get("app_no") or ""
    t = d.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    hdr = t.rows[0].cells
    for cell, text in zip(hdr, ("Claim language (%s)" % pub_no,
                                "Relevant disclosure of this document")):
        cell.paragraphs[0].add_run(text).bold = True
        shd = OxmlElement("w:shd")
        shd.set(qn("w:fill"), "EFEFEF")
        cell._tc.get_or_add_tcPr().append(shd)

    for row in doc_model["rows"]:
        prose, cites = _right_bits(row)
        cells = t.add_row().cells
        cells[0].text = _left_cell(row, pub_no)
        first = True
        for chunk in (prose or "").split("\n"):
            p = cells[1].paragraphs[0] if first else cells[1].add_paragraph()
            p.text = chunk
            first = False
        for c in cites:
            p = cells[1].add_paragraph()
            p.paragraph_format.left_indent = Inches(0.14)
            p.text = "• %s" % c

    for r in t.rows:
        for c in r.cells:
            for p in c.paragraphs:
                p.paragraph_format.space_after = Pt(2)
                for run in p.runs:
                    run.font.size = Pt(10.5)

    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def filename(doc_model, ext):
    pub = re.sub(r"[^A-Za-z0-9]+", "", doc_model["pub"] or "doc")
    return "ConciseDescription_Doc%s_%s.%s" % (doc_model["n"], pub, ext)
