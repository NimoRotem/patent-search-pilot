"""Filing readiness, and the package that comes out the other end.

The last question this product has to answer is the one the user actually came for: *can I file
this?*  Answering it honestly means separating three different things that all feel like "not
ready":

  BLOCKERS      the application would be defective or incomplete as filed — an unresolved
                drafting note where a dimension should be, a citation that resolves to nothing,
                no named inventor.  These are listed as blockers and the export says so.
  FORMALITIES   things the USPTO will object to but which do not stop a filing date — a title
                over 500 characters, an abstract over 150 words.
  NOT OUR JOB   the oath or declaration, the entity-status certification, formal drawings under
                37 CFR 1.84, the fee payment, and the attorney's own review.  These are listed as
                what remains, never silently ticked off.

NO FEE AMOUNTS ARE PRINTED.  The counts that drive them (total claims, independent claims,
multiple dependent claims, specification sheet count) are computed exactly, and which surcharges
those counts trigger is stated — but the dollar figures change by fee-setting rulemaking and a
number baked in here would be quietly wrong within a year.  The current schedule is one link away
and always right.

WHAT THIS IS NOT.  Nothing here is a legal opinion, and no check in this module says anything
about whether the application is allowable.  It reads the document against the formal
requirements of 37 CFR 1.52, 1.72, 1.75, 1.77 and 1.121.
"""
from __future__ import annotations

import html
import re
from datetime import date
from io import BytesIO
from typing import Any, Mapping, Sequence

import draft_qa
import draft_workspace

FEE_SCHEDULE_URL = "https://www.uspto.gov/learning-and-resources/fees-and-payment/uspto-fee-schedule"
EFS_URL = "https://patentcenter.uspto.gov/"

# 37 CFR 1.77(b): the order in which the parts of a utility specification are arranged.
FILING_ORDER = (
    ("title", "TITLE OF THE INVENTION", False),
    ("cross_reference", "CROSS-REFERENCE TO RELATED APPLICATIONS", True),
    ("field", "FIELD OF THE INVENTION", True),
    ("background", "BACKGROUND OF THE INVENTION", True),
    ("summary", "BRIEF SUMMARY OF THE INVENTION", True),
    ("drawing_descriptions", "BRIEF DESCRIPTION OF THE SEVERAL VIEWS OF THE DRAWINGS", True),
    ("detailed_description", "DETAILED DESCRIPTION OF THE INVENTION", True),
)

_NOTE_RE = re.compile(r"\[DRAFTING NOTE:([^\]]*)\]", re.IGNORECASE)


# =============================================================================================
# Readiness
# =============================================================================================
def fee_profile(claims_text: str) -> dict[str, Any]:
    """The claim counts that decide the filing fee, and which surcharges they trigger."""
    claims = draft_qa.split_claims(claims_text)
    independent, multiple, extra_from_multiple = 0, 0, 0
    for claim in claims:
        dependencies = draft_qa.claim_dependencies(claim["text"])
        if not dependencies:
            independent += 1
        elif len(dependencies) > 1:
            multiple += 1
            # 37 CFR 1.75(c): a multiple dependent claim is counted as the number of claims to
            # which it refers, which is why one of them can cost more than ten ordinary claims.
            extra_from_multiple += len(dependencies) - 1
    total = len(claims)
    billable = total + extra_from_multiple
    surcharges = []
    if independent > 3:
        surcharges.append(f"{independent - 3} independent claim(s) over the three included "
                          "(37 CFR 1.16(h))")
    if billable > 20:
        surcharges.append(f"{billable - 20} claim(s) over the twenty included (37 CFR 1.16(i))")
    if multiple:
        surcharges.append(f"{multiple} multiple dependent claim(s) (37 CFR 1.16(j)); each is "
                          "counted as the number of claims it refers to")
    return {"total": total, "independent": independent, "dependent": total - independent,
            "multiple_dependent": multiple, "billable": billable, "surcharges": surcharges,
            "fee_schedule_url": FEE_SCHEDULE_URL}


def open_notes(sections: Mapping[str, str]) -> list[dict[str, str]]:
    out = []
    for key, _name, heading in draft_workspace.SECTION_FILES:
        for match in _NOTE_RE.finditer(str(sections.get(key) or "")):
            out.append({"section": heading, "note": match.group(1).strip()[:400]})
    return out


def readiness(*, project: Mapping[str, Any], version: Mapping[str, Any],
              qa: Mapping[str, Any] | None = None,
              references: Sequence[Mapping[str, Any]] = (),
              figures: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Is this draft in a state where a practitioner could take it to Patent Center?"""
    sections = dict(version.get("sections") or {})
    blockers: list[dict[str, str]] = []
    formalities: list[dict[str, str]] = []
    remaining: list[str] = []

    notes = open_notes(sections)
    if notes:
        blockers.append({
            "title": f"{len(notes)} unresolved drafting note(s)",
            "detail": "Each one marks a fact only the inventor has. Filing with them in place "
                      "puts placeholder text into the published specification.",
            "items": "; ".join(f"{n['section']}: {n['note']}" for n in notes[:8])})

    checks = list((qa or {}).get("checks") or [])
    for check in checks:
        if check.get("status") == "fail" and check.get("severity") == "error":
            blockers.append({"title": check.get("name", "Consistency check failed"),
                             "detail": check.get("detail", ""),
                             "items": "; ".join(str(i) for i in (check.get("items") or [])[:6])})
        elif check.get("status") == "warn" and check.get("severity") == "warn":
            formalities.append({"title": check.get("name", ""), "detail": check.get("detail", ""),
                                "items": "; ".join(str(i) for i in (check.get("items") or [])[:6])})
    for finding in ((qa or {}).get("findings") or []):
        if finding.get("severity") == "critical":
            blockers.append({"title": finding.get("title", "Critical review finding"),
                             "detail": finding.get("detail", ""),
                             "items": finding.get("where", "")})
    if qa is None:
        blockers.append({"title": "This draft has not been reviewed",
                         "detail": "Run the consistency review before treating any draft as "
                                   "ready.", "items": ""})

    if not str(project.get("inventors") or "").strip():
        blockers.append({"title": "No inventor is named",
                         "detail": "37 CFR 1.41 and 1.63: the application data sheet must name "
                                   "each inventor, and each must sign an oath or declaration.",
                         "items": ""})
    if not str(project.get("applicant") or "").strip():
        formalities.append({"title": "No applicant is recorded",
                            "detail": "37 CFR 1.46: where the applicant is not the inventor, the "
                                      "application data sheet must say who it is.", "items": ""})

    described = draft_qa.figures_mentioned(str(sections.get("drawing_descriptions") or ""))
    if described and not figures:
        formalities.append({
            "title": f"{len(described)} figure(s) are described but no drawing sheet exists",
            "detail": "35 USC 113 requires a drawing where one is necessary to understand the "
                      "invention. Drawings may be filed informally and corrected later, but they "
                      "must be filed.", "items": ""})
    elif not described:
        formalities.append({"title": "The application describes no drawings",
                            "detail": "Most mechanical and electrical applications need at least "
                                      "one. Confirm this one genuinely does not.", "items": ""})

    fees = fee_profile(str(sections.get("claims") or ""))
    remaining = [
        "An oath or declaration signed by every named inventor (37 CFR 1.63), or a substitute "
        "statement where one is permitted.",
        "An Application Data Sheet (37 CFR 1.76) — the fields are pre-filled in the package.",
        "Entity-status certification if claiming small or micro entity fees (37 CFR 1.27, 1.29).",
        "Formal drawings meeting 37 CFR 1.84. Nothing in this product checks sheet size, margins, "
        "line weight, shading or lettering.",
        "An Information Disclosure Statement listing the art you are aware of (37 CFR 1.56, 1.97). "
        "The citation listing in this package is a starting point, not a signed form.",
        "The filing, search and examination fees due on the counts above.",
        "Review by a registered US patent practitioner before anything is submitted.",
    ]
    return {
        "ready": not blockers,
        "blockers": blockers,
        "formalities": formalities,
        "remaining": remaining,
        "fees": fees,
        "notes": notes,
        "citation_count": len({str(c) for c in (version.get("citations") or [])}),
        "reference_count": len(references),
        "verdict": (qa or {}).get("verdict", "unknown"),
        "patent_center_url": EFS_URL,
    }


# =============================================================================================
# The filing package
# =============================================================================================
def numbered_paragraphs(text: str, start: int) -> tuple[list[str], int]:
    """Number the paragraphs of a section as ``[0001]``, per 37 CFR 1.52(b)(6).

    Numbered paragraphs are optional at filing and close to mandatory in practice: without them
    every later amendment has to identify the passage it changes by quotation instead of by
    number.  Numbering here rather than in the draft keeps the draft readable while the filing
    copy is in the form the Office wants.
    """
    out: list[str] = []
    counter = start
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        block = block.strip()
        if not block:
            continue
        out.append(f"[{counter:04d}] {block}")
        counter += 1
    return out, counter


def filing_text(project: Mapping[str, Any], version: Mapping[str, Any]) -> str:
    """The specification as plain text, in 37 CFR 1.77(b) order with numbered paragraphs."""
    sections = dict(version.get("sections") or {})
    lines: list[str] = []
    counter = 1
    for key, heading, numbered in FILING_ORDER:
        body = str(sections.get(key) or "").strip()
        if key == "title":
            lines += [heading, "", body or str(project.get("title") or ""), ""]
            continue
        if not body:
            continue
        lines += [heading, ""]
        if numbered:
            paragraphs, counter = numbered_paragraphs(body, counter)
            lines += paragraphs + [""]
        else:
            lines += [body, ""]
    lines += ["", "=" * 72, "", "CLAIMS", "",
              "What is claimed is:", "", str(sections.get("claims") or "").strip(), "",
              "=" * 72, "", "ABSTRACT OF THE DISCLOSURE", "",
              str(sections.get("abstract") or "").strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def ads_fields(project: Mapping[str, Any], version: Mapping[str, Any]) -> list[dict[str, str]]:
    """What goes on the Application Data Sheet, as far as we legitimately know it."""
    sections = dict(version.get("sections") or {})
    inventors = [line.strip() for line in
                 re.split(r"[\n;]+", str(project.get("inventors") or "")) if line.strip()]
    cross_reference = str(sections.get("cross_reference") or "").strip()
    return [
        {"field": "Title of invention",
         "value": str(sections.get("title") or project.get("title") or "")[:500],
         "note": "37 CFR 1.72(a) — 500 characters maximum."},
        {"field": "Application type", "value": "Utility, non-provisional",
         "note": "Change this if you are filing a provisional or a design application."},
        {"field": "Inventor(s)", "value": "\n".join(inventors) or "(not supplied)",
         "note": "37 CFR 1.41 — legal name, residence and mailing address for each."},
        {"field": "Applicant", "value": str(project.get("applicant") or "") or "(not supplied)",
         "note": "37 CFR 1.46 — required where the applicant is not the inventor."},
        {"field": "Domestic benefit / priority", "value": cross_reference or "(none claimed)",
         "note": "37 CFR 1.78 — a benefit claim must appear in the ADS, not only in the "
                 "specification."},
        {"field": "Foreign priority", "value": "(none stated)",
         "note": "37 CFR 1.55 — add every foreign application whose priority you claim."},
        {"field": "Entity status", "value": "(not certified)",
         "note": "37 CFR 1.27 / 1.29 — small or micro entity status must be certified, not "
                 "assumed."},
        {"field": "Correspondence address", "value": "(not supplied)", "note": "37 CFR 1.33."},
    ]


def citation_listing(version: Mapping[str, Any],
                     references: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, str]]:
    """The cited art in the shape an IDS listing wants it, resolved and dated.

    This is the LISTING, not an executed SB/08a: the duty of disclosure under 37 CFR 1.56 is
    personal to the people involved in the application, and no software discharges it.
    """
    import draft_cite
    by_publication = {str(r.get("publication_number")): r for r in references}
    out = []
    for citation in dict.fromkeys(str(c) for c in (version.get("citations") or [])):
        record = draft_cite.resolve(citation, allow_remote=False)
        reference = by_publication.get(citation, {})
        publication = record.get("publication_number") or citation
        country = publication[:2] if len(publication) > 2 else ""
        out.append({
            "publication_number": publication,
            "country": country,
            "kind_code": record.get("kind_code", ""),
            "publication_date": record.get("publication_date", ""),
            "name_of_patentee": record.get("assignee") or reference.get("title", "")[:120],
            "title": record.get("title") or reference.get("title", ""),
            "resolved": "yes" if record.get("found") else "NOT RESOLVED",
            "source": record.get("source", ""),
        })
    return out


def render_filing_docx(project: Mapping[str, Any], version: Mapping[str, Any], *,
                       readiness_report: Mapping[str, Any],
                       references: Sequence[Mapping[str, Any]] = ()) -> BytesIO:
    """The filing copy: cover checklist, ADS fields, specification, claims, abstract, IDS list."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    sections = dict(version.get("sections") or {})
    document = Document()
    layout = document.sections[0]
    layout.page_width, layout.page_height = Inches(8.5), Inches(11)
    # 37 CFR 1.52(a)(1)(ii): at least 2.0 cm left and top, 2.0 cm right, 2.0 cm bottom.
    layout.left_margin = layout.top_margin = Inches(1)
    layout.right_margin = layout.bottom_margin = Inches(0.85)
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    normal.paragraph_format.line_spacing = 2.0        # 37 CFR 1.52(b)(2)(ii)
    normal.paragraph_format.space_after = Pt(0)

    def heading(text: str) -> None:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(text)
        run.bold = True

    def plain(text: str, *, size: int = 12, bold: bool = False, spacing: float = 2.0) -> None:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.line_spacing = spacing
        run = paragraph.add_run(text)
        run.bold = bold
        run.font.size = Pt(size)

    # -- cover -------------------------------------------------------------------------------
    heading("FILING PACKAGE — PREPARED " + date.today().isoformat())
    plain(str(sections.get("title") or project.get("title") or ""), bold=True, spacing=1.15)
    plain("This package contains the specification, claims and abstract in filing form, the "
          "Application Data Sheet fields as far as they are known, and the cited-art listing. It "
          "is not a filing and it is not legal advice. Everything under “Still required” below "
          "must be done by a person.", size=10, spacing=1.15)
    if readiness_report.get("blockers"):
        plain(f"NOT READY: {len(readiness_report['blockers'])} blocker(s) remain.",
              bold=True, size=11, spacing=1.15)
        for blocker in readiness_report["blockers"]:
            plain(f"  • {blocker.get('title')} — {blocker.get('detail')}", size=10, spacing=1.15)
    else:
        plain("No blockers were found by the automated checks. That is not the same as ready to "
              "file; see “Still required”.", bold=True, size=11, spacing=1.15)
    for label, items in (("Formalities to settle", readiness_report.get("formalities") or []),):
        if items:
            plain(label, bold=True, size=11, spacing=1.15)
            for item in items:
                plain(f"  • {item.get('title')} — {item.get('detail')}", size=10, spacing=1.15)
    fees = readiness_report.get("fees") or {}
    plain("Claim counts for the fee calculation", bold=True, size=11, spacing=1.15)
    plain(f"  {fees.get('total', 0)} claims, {fees.get('independent', 0)} independent, "
          f"{fees.get('multiple_dependent', 0)} multiple dependent "
          f"(counted as {fees.get('billable', 0)} for fee purposes).", size=10, spacing=1.15)
    for surcharge in fees.get("surcharges") or []:
        plain(f"  • {surcharge}", size=10, spacing=1.15)
    plain(f"  Current amounts: {FEE_SCHEDULE_URL}", size=10, spacing=1.15)
    plain("Still required", bold=True, size=11, spacing=1.15)
    for item in readiness_report.get("remaining") or []:
        plain(f"  • {item}", size=10, spacing=1.15)

    # -- ADS ---------------------------------------------------------------------------------
    document.add_page_break()
    heading("APPLICATION DATA SHEET — FIELD VALUES (37 CFR 1.76)")
    plain("Transcribe these into the Patent Center ADS form. A blank is a value we do not have, "
          "never a value of none.", size=10, spacing=1.15)
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    for cell, label in zip(table.rows[0].cells, ("Field", "Value", "Rule")):
        cell.text = label
        cell.paragraphs[0].runs[0].bold = True
    for field in ads_fields(project, version):
        row = table.add_row().cells
        row[0].text = field["field"]
        row[1].text = field["value"]
        row[2].text = field["note"]

    # -- specification -------------------------------------------------------------------------
    document.add_page_break()
    counter = 1
    for key, title, numbered in FILING_ORDER:
        body = str(sections.get(key) or "").strip()
        if key == "title":
            heading(title)
            plain(body or str(project.get("title") or ""))
            continue
        if not body:
            continue
        heading(title)
        if numbered:
            paragraphs, counter = numbered_paragraphs(body, counter)
            for paragraph in paragraphs:
                plain(paragraph)
        else:
            plain(body)

    document.add_page_break()
    heading("CLAIMS")
    plain("What is claimed is:")
    for claim in draft_qa.split_claims(str(sections.get("claims") or "")):
        plain(f"{claim['number']}. {claim['text']}")

    document.add_page_break()
    heading("ABSTRACT OF THE DISCLOSURE")
    plain(str(sections.get("abstract") or "").strip())

    listing = citation_listing(version, references)
    if listing:
        document.add_page_break()
        heading("CITED ART — LISTING FOR AN INFORMATION DISCLOSURE STATEMENT")
        plain("This is the listing, not an executed form. The duty of disclosure under 37 CFR "
              "1.56 belongs to the people substantively involved in the application.",
              size=10, spacing=1.15)
        cites = document.add_table(rows=1, cols=5)
        cites.style = "Table Grid"
        for cell, label in zip(cites.rows[0].cells,
                               ("Publication", "Kind", "Date", "Patentee", "Title")):
            cell.text = label
            cell.paragraphs[0].runs[0].bold = True
        for item in listing:
            row = cites.add_row().cells
            row[0].text = item["publication_number"]
            row[1].text = item["kind_code"]
            row[2].text = item["publication_date"]
            row[3].text = item["name_of_patentee"]
            row[4].text = item["title"][:180]

    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


def readiness_html(report: Mapping[str, Any]) -> str:
    """A compact rendering used by the studio page; kept beside the rules it reports."""
    def block(title: str, items: Sequence[Mapping[str, str]], tone: str) -> str:
        if not items:
            return ""
        rows = "".join(
            f"<li><b>{html.escape(str(i.get('title') or ''))}</b> "
            f"<span>{html.escape(str(i.get('detail') or ''))}</span>"
            + (f"<code>{html.escape(str(i.get('items')))}</code>" if i.get("items") else "")
            + "</li>" for i in items)
        return f'<div class="rdy {tone}"><h4>{html.escape(title)}</h4><ul>{rows}</ul></div>'
    return (block("Blockers", report.get("blockers") or [], "bad") +
            block("Formalities", report.get("formalities") or [], "warn"))
