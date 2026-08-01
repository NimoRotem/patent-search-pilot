"""Portable exports for an immutable US-application draft version."""
from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

import drafting

WORKING_DRAFT_NOTICE = (
    "WORKING DRAFT — attorney review and inventor verification required before filing. "
    "Bracketed drafting notes identify missing facts; AI output is not legal advice."
)


def _clean_filename(value: str, fallback: str = "us-patent-draft") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-.")
    return (value[:90] or fallback).lower()


def download_name(project: Mapping[str, Any], version_no: int, suffix: str) -> str:
    return f"{_clean_filename(str(project.get('title') or ''))}-v{int(version_no)}.{suffix}"


def render_markdown(project: Mapping[str, Any], version: Mapping[str, Any],
                    references: Sequence[Mapping[str, Any]] = ()) -> str:
    """Render a self-describing working file while preserving the application section order."""
    sections = version.get("sections") or {}
    body = drafting.render_application_markdown(sections)
    source_lines = []
    for ref in references:
        pub = str(ref.get("publication_number") or "").strip()
        if not pub:
            continue
        title = str(ref.get("title") or "").strip()
        url = str(ref.get("source_url") or "").strip()
        label = f"{pub} — {title}" if title else pub
        source_lines.append(f"- [{label}]({url})" if url else f"- {label}")
    trace = "\n".join(source_lines) or "- No source references recorded."
    return (
        f"> {WORKING_DRAFT_NOTICE}\n\n"
        f"Search report: `{project.get('search_slug') or ''}`  \n"
        f"Draft version: {int(version.get('version_no') or 0)}  \n"
        f"Status: {version.get('status') or 'draft'}\n\n"
        f"{body}\n---\n\n"
        "## Drafting source trace (not part of the application)\n\n"
        f"{trace}\n"
    )


def _set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    tr_pr.append(repeat)


def _add_text(doc: Document, text: str, *, claims: bool = False) -> None:
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines()]
        if claims and len(lines) > 1:
            for line in lines:
                if not line.strip():
                    continue
                p = doc.add_paragraph(line.strip())
                p.paragraph_format.keep_together = False
            continue
        p = doc.add_paragraph("\n".join(lines).strip())
        p.paragraph_format.keep_together = False


def render_docx(project: Mapping[str, Any], version: Mapping[str, Any],
                references: Sequence[Mapping[str, Any]] = ()) -> BytesIO:
    """Build an editable Letter-size DOCX with USPTO-oriented layout defaults.

    Claims and Abstract begin on separate pages.  The generated file is intentionally labelled as
    a working draft; filing metadata, declarations, formal drawings and fees remain practitioner
    responsibilities.
    """
    sections = version.get("sections") or {}
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.left_margin = Inches(1)
    sec.right_margin = sec.top_margin = sec.bottom_margin = Inches(0.75)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(6)
    for style_name in ("Title", "Heading 1"):
        style = doc.styles[style_name]
        style.font.name = "Times New Roman"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")

    doc.core_properties.title = str(sections.get("title") or project.get("title") or "")[:255]
    doc.core_properties.subject = "US utility patent application working draft"
    doc.core_properties.comments = WORKING_DRAFT_NOTICE

    header = sec.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hr = header.add_run("WORKING DRAFT — REVIEW BEFORE FILING")
    hr.bold = True
    hr.font.name = "Times New Roman"
    hr.font.size = Pt(9)

    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fr = footer.add_run("AI-assisted draft · attorney and inventor review required")
    fr.font.name = "Times New Roman"
    fr.font.size = Pt(8)

    for index, (key, heading) in enumerate(drafting.SECTION_ORDER):
        if key in {"claims", "abstract"}:
            doc.add_page_break()
        if index == 0:
            title = doc.add_paragraph(style="Title")
            title.alignment = WD_ALIGN_PARAGRAPH.CENTER
            title.add_run(str(sections.get(key) or project.get("title") or "Untitled"))
            continue
        doc.add_heading(heading.upper(), level=1)
        _add_text(doc, str(sections.get(key) or ""), claims=(key == "claims"))

    # Keep an auditable source manifest in a final, explicitly non-application section. This is
    # useful during attorney review and deliberately segregated from the application body.
    doc.add_section(WD_SECTION.NEW_PAGE)
    doc.add_heading("DRAFTING SOURCE TRACE — NOT PART OF THE APPLICATION", level=1)
    doc.add_paragraph(WORKING_DRAFT_NOTICE)
    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    for cell, label in zip(table.rows[0].cells, ("Rank", "Publication", "Title")):
        cell.text = label
        cell.paragraphs[0].runs[0].bold = True
    _set_repeat_table_header(table.rows[0])
    for ref in references:
        row = table.add_row().cells
        row[0].text = str(ref.get("report_rank") or "")
        row[1].text = str(ref.get("publication_number") or "")
        row[2].text = str(ref.get("title") or "")

    output = BytesIO()
    doc.save(output)
    output.seek(0)
    return output


def render_pdf(project: Mapping[str, Any], version: Mapping[str, Any],
               references: Sequence[Mapping[str, Any]] = ()) -> BytesIO:
    """Render a paginated attorney-review PDF; DOCX remains the editable filing handoff."""
    output = BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=letter, leftMargin=inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=str((version.get("sections") or {}).get("title") or project.get("title") or "")[:255],
        subject="US utility patent application working draft",
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "PatentBody", parent=styles["BodyText"], fontName="Times-Roman", fontSize=12,
        leading=18, spaceAfter=8, splitLongWords=True)
    heading = ParagraphStyle(
        "PatentHeading", parent=styles["Heading1"], fontName="Times-Bold", fontSize=12,
        leading=16, spaceBefore=12, spaceAfter=8, keepWithNext=True)
    title_style = ParagraphStyle(
        "PatentTitle", parent=heading, alignment=1, fontSize=14, leading=18, spaceAfter=18)
    warning = ParagraphStyle(
        "PatentWarning", parent=body, fontName="Helvetica-Bold", fontSize=9, leading=12,
        textColor="#8a3b00", spaceAfter=14)
    story = [Paragraph(html.escape(WORKING_DRAFT_NOTICE), warning)]
    sections = version.get("sections") or {}
    for index, (key, label) in enumerate(drafting.SECTION_ORDER):
        if key in {"claims", "abstract"}:
            story.append(PageBreak())
        content = str(sections.get(key) or "").strip()
        if index == 0:
            story.append(Paragraph(html.escape(content or str(project.get("title") or "Untitled")),
                                   title_style))
            continue
        story.append(Paragraph(html.escape(label.upper()), heading))
        for block in re.split(r"\n\s*\n", content):
            if block.strip():
                story.append(Paragraph(html.escape(block.strip()).replace("\n", "<br/>"), body))
    story.extend((PageBreak(), Paragraph("DRAFTING SOURCE TRACE — NOT PART OF THE APPLICATION", heading),
                  Paragraph(html.escape(WORKING_DRAFT_NOTICE), warning)))
    for ref in references:
        pub = str(ref.get("publication_number") or "").strip()
        if pub:
            label = f"{pub} — {str(ref.get('title') or '').strip()}".rstrip(" —")
            story.append(Paragraph(html.escape(label), body))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(inch, 0.45 * inch, "AI-assisted working draft — attorney review required")
        canvas.drawRightString(7.75 * inch, 0.45 * inch, f"Page {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
