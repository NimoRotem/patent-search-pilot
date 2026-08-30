"""The citation listing for a USPTO Information Disclosure Statement (form SB/08a).

An applicant who is aware of prior art has a duty to disclose it (37 CFR 1.56), and the way that
is done is form SB/08a with a list of every document being cited. The list is the tedious part —
it wants the document number, kind code, date and patentee split into the right one of three
tables, with a running cite number — and every field it wants is already sitting in a completed
search. Producing it by hand from a results page is transcription work that also invites
transcription errors.

**What this produces, stated exactly.** A citation LISTING in the SB/08a column layout, ready to
attach to the form. It is not a filled official form: the USPTO's own PDF is a fillable AcroForm
we do not fill here, and a document that looked like an executed SB/08a but had no signature,
no application number and no examiner column would be worse than useless. The heading on every
page says what it is, and the caller is told to attach it.

Sorting into the three tables follows the form's own division:

  * **U.S. PATENTS** — a US document with a grant kind code (B1/B2, or the older A before 2001
    when US grants used A);
  * **U.S. PATENT APPLICATION PUBLICATIONS** — a US document whose number is a pre-grant
    publication (an 11-digit YYYYnnnnnnn number, kind A1/A2/A9);
  * **FOREIGN PATENT DOCUMENTS** — everything else, with its country and kind code in their own
    columns, plus the translation column the form requires.
"""
from __future__ import annotations

import re
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

import disclosure

LINE = colors.HexColor("#333333")
HEAD_BG = colors.HexColor("#e8ecf2")
MUTED = colors.HexColor("#555555")

FORM_TITLE = "INFORMATION DISCLOSURE STATEMENT — CITATION LISTING"
FORM_SUB = ("Prepared for attachment to USPTO form SB/08a. This is the citation listing only: "
            "the form itself, the application number, filing date, first named inventor, art unit "
            "and the signature must be completed and signed by the applicant or practitioner.")

US_GRANT_KINDS = ("B1", "B2", "B", "E", "H", "P1", "P2", "P3", "S")
US_APP_KINDS = ("A1", "A2", "A9")


def _styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=ss["Title"], fontSize=13, leading=16, spaceAfter=4),
        "sub": ParagraphStyle("s", parent=ss["Normal"], fontSize=7.5, leading=10, textColor=MUTED),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], fontSize=9.5, leading=12,
                             spaceBefore=12, spaceAfter=4),
        "cell": ParagraphStyle("c", parent=ss["Normal"], fontSize=7.4, leading=9),
        "cellb": ParagraphStyle("cb", parent=ss["Normal"], fontSize=7.4, leading=9,
                                fontName="Helvetica-Bold"),
        "note": ParagraphStyle("n", parent=ss["Normal"], fontSize=7.5, leading=10, textColor=MUTED,
                               spaceBefore=6),
    }


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def split_pub(pub):
    """'US-11338449-B2' -> ('US', '11338449', 'B2'). Tolerates the unhyphenated spelling."""
    raw = re.sub(r"\s+", "", str(pub or "")).upper()
    m = re.match(r"^([A-Z]{2})-?([0-9]+)-?([A-Z][0-9]?)?$", raw)
    if not m:
        return "", raw, ""
    return m.group(1), m.group(2), (m.group(3) or "")


def _is_us_application(number, kind):
    """A US pre-grant publication is numbered YYYYnnnnnnn (11 digits) and published as A1/A2/A9."""
    if kind.upper() in US_APP_KINDS:
        return True
    return len(number) == 11 and number[:4].isdigit() and 1999 <= int(number[:4]) <= 2100


def classify(pub):
    """-> 'us_patent' | 'us_application' | 'foreign'."""
    country, number, kind = split_pub(pub)
    if country != "US":
        return "foreign"
    if _is_us_application(number, kind):
        return "us_application"
    return "us_patent"


def _patentee(ref):
    """Name of patentee or applicant, as the form asks for it: assignee, else first inventor."""
    for key in ("assignees", "inventors"):
        vals = ref.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        names = [str(v).strip() for v in vals if str(v).strip()]
        if names:
            return names[0] + (" et al." if key == "inventors" and len(names) > 1 else "")
    return "—"


def _date(ref):
    d = ref.get("publication_date") or ref.get("filing_date") or ref.get("priority_date") or ""
    return str(d)[:10]


def _fmt_number(number):
    """Group a US patent number the way the form prints it (11,338,449)."""
    if number.isdigit() and len(number) <= 9:
        return f"{int(number):,}"
    return number


def build(model):
    """Export model -> ``{'us_patents': [...], 'us_applications': [...], 'foreign': [...],
    'skipped': [...]}``, each row a dict of the form's own columns with a running cite number."""
    buckets = {"us_patents": [], "us_applications": [], "foreign": []}
    skipped = []
    cite = 0
    for ref in model.get("references") or []:
        pub = ref.get("pub") or ""
        country, number, kind = split_pub(pub)
        if not number:
            skipped.append(pub)
            continue
        cite += 1
        row = {"cite": cite, "pub": pub, "country": country, "number": number, "kind": kind,
               "date": _date(ref), "patentee": _patentee(ref),
               "title": str(ref.get("title") or "")[:300]}
        kind_of = classify(pub)
        if kind_of == "us_patent":
            row["number_display"] = _fmt_number(number)
            buckets["us_patents"].append(row)
        elif kind_of == "us_application":
            row["number_display"] = number
            buckets["us_applications"].append(row)
        else:
            row["number_display"] = number
            buckets["foreign"].append(row)
    buckets["skipped"] = skipped
    return buckets


def _table(rows, columns, widths, S):
    head = [Paragraph(f"<b>{_esc(c)}</b>", S["cell"]) for c, _ in columns]
    data = [head]
    for r in rows:
        data.append([Paragraph(_esc(get(r)), S["cell"]) for _, get in columns])
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def render(model, out_path):
    """Write the citation listing PDF."""
    S = _styles()
    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                            leftMargin=0.55 * inch, rightMargin=0.55 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"IDS citation listing — {model.get('title') or model.get('slug')}")
    b = build(model)
    story = [Paragraph(FORM_TITLE, S["title"]),
             Paragraph(_esc(FORM_SUB), S["sub"]),
             HRFlowable(width="100%", thickness=1.2, color=LINE, spaceBefore=6, spaceAfter=8)]

    doc_meta = model.get("report_doc") or {}
    meta_rows = [
        ("Application / matter", doc_meta.get("matter_title") or model.get("title") or "—"),
        ("Subject application number", doc_meta.get("subject_patent_number")
         or model.get("subject") or "—"),
        ("Applicant / client", doc_meta.get("client_name") or "—"),
        ("Attorney docket number", doc_meta.get("client_reference_number") or "—"),
        ("Prepared", date.today().isoformat()),
        ("Documents cited", str(len(b["us_patents"]) + len(b["us_applications"]) + len(b["foreign"]))),
    ]
    t = Table([[Paragraph(f"<b>{_esc(k)}</b>", S["cell"]), Paragraph(_esc(v), S["cell"])]
               for k, v in meta_rows], colWidths=[2.0 * inch, 5.4 * inch])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, LINE),
                           ("BACKGROUND", (0, 0), (0, -1), HEAD_BG),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 3),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(t)

    W = 7.4 * inch
    if b["us_patents"]:
        story.append(Paragraph("U.S. PATENTS", S["h2"]))
        story.append(_table(b["us_patents"], [
            ("Examiner\nInitials", lambda r: ""),
            ("Cite\nNo.", lambda r: str(r["cite"])),
            ("Patent Number", lambda r: r["number_display"]),
            ("Kind\nCode", lambda r: r["kind"] or "—"),
            ("Issue Date", lambda r: r["date"] or "—"),
            ("Name of Patentee or Applicant of cited document", lambda r: r["patentee"]),
            ("Pages, Columns,\nLines where relevant", lambda r: ""),
        ], [0.72 * inch, 0.4 * inch, 1.0 * inch, 0.5 * inch, 0.85 * inch, W - 4.92 * inch,
            1.45 * inch], S))

    if b["us_applications"]:
        story.append(Paragraph("U.S. PATENT APPLICATION PUBLICATIONS", S["h2"]))
        story.append(_table(b["us_applications"], [
            ("Examiner\nInitials", lambda r: ""),
            ("Cite\nNo.", lambda r: str(r["cite"])),
            ("Publication Number", lambda r: r["number_display"]),
            ("Kind\nCode", lambda r: r["kind"] or "—"),
            ("Publication Date", lambda r: r["date"] or "—"),
            ("Name of Patentee or Applicant of cited document", lambda r: r["patentee"]),
            ("Pages, Columns,\nLines where relevant", lambda r: ""),
        ], [0.72 * inch, 0.4 * inch, 1.15 * inch, 0.5 * inch, 0.9 * inch, W - 5.22 * inch,
            1.55 * inch], S))

    if b["foreign"]:
        story.append(Paragraph("FOREIGN PATENT DOCUMENTS", S["h2"]))
        story.append(_table(b["foreign"], [
            ("Examiner\nInitials", lambda r: ""),
            ("Cite\nNo.", lambda r: str(r["cite"])),
            ("Foreign Document\nNumber", lambda r: r["number_display"]),
            ("Country\nCode", lambda r: r["country"] or "—"),
            ("Kind\nCode", lambda r: r["kind"] or "—"),
            ("Publication\nDate", lambda r: r["date"] or "—"),
            ("Name of Patentee or Applicant", lambda r: r["patentee"]),
            ("T", lambda r: ""),
        ], [0.72 * inch, 0.35 * inch, 1.1 * inch, 0.62 * inch, 0.45 * inch, 0.8 * inch,
            W - 4.34 * inch, 0.3 * inch], S))

    if not (b["us_patents"] or b["us_applications"] or b["foreign"]):
        story.append(Paragraph("No documents were selected for citation.", S["note"]))

    story.append(Paragraph(
        "<b>T</b> — mark if an English-language translation is attached (37 CFR 1.98(a)(3)). "
        "<b>Examiner Initials</b> and <b>Pages, Columns, Lines</b> are left blank for completion "
        "on the filed form. A copy of each foreign patent document and of any non-patent "
        "literature must accompany the statement unless it was cited in a prior application.",
        S["note"]))
    if b["skipped"]:
        story.append(Paragraph(
            "<b>Not listed:</b> " + _esc(", ".join(b["skipped"][:20])) +
            " — no publication number could be parsed; add these by hand.", S["note"]))
    story.append(Paragraph(
        "<b>Source.</b> These documents were surfaced by " + _esc(disclosure.DOC_TITLE) +
        ". " + _esc(disclosure.DOC_SUBTITLE) + " Citing a document here is a disclosure, not an "
        "admission that it is prior art (37 CFR 1.97(h)).", S["note"]))

    def _footer(cv, _doc):
        cv.saveState()
        cv.setFont("Helvetica", 7)
        cv.setFillColor(MUTED)
        cv.drawString(0.55 * inch, 0.35 * inch,
                      "IDS citation listing — attach to USPTO form SB/08a")
        cv.drawRightString(letter[0] - 0.55 * inch, 0.35 * inch, f"Page {cv.getPageNumber()}")
        cv.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out_path
