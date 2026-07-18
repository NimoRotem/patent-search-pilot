"""Render the export model to an editable DOCX (python-docx). Milestone 4 §1."""
from __future__ import annotations
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from PIL import Image as PILImage

NAVY = RGBColor(0x0b, 0x25, 0x45)
BLUE = RGBColor(0x00, 0x50, 0xA0)
GOOD = RGBColor(0x0a, 0x7d, 0x4d)
SECRET = RGBColor(0x7b, 0x2f, 0xbf)
WARN = RGBColor(0xb2, 0x5e, 0x00)
MUTED = RGBColor(0x5b, 0x6b, 0x82)


def _basis_label(b):
    return {"public_prior_art": "PUBLIC prior art", "secret_prior_art": "SECRET prior art (novelty only)",
            "priority_interval": "priority-interval art"}.get(b, "not dated")


def _shade(cell, hex_no_hash):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), hex_no_hash)
    tcPr.append(shd)


def _green_shade(intensity):
    # blend white -> green(#0a7d4d) by intensity
    r = int(255 + (10 - 255) * intensity); g = int(255 + (125 - 255) * intensity); b = int(255 + (77 - 255) * intensity)
    return f"{r:02x}{g:02x}{b:02x}"


def _run(p, text, bold=False, color=None, size=None, italic=False):
    r = p.add_run(text)
    r.bold = bold; r.italic = italic
    if color is not None:
        r.font.color.rgb = color
    if size:
        r.font.size = Pt(size)
    return r


def render(model, out_path):
    doc = Document()
    styles = doc.styles
    styles["Normal"].font.name = "Calibri"
    styles["Normal"].font.size = Pt(10)

    # ---- cover ----
    h = doc.add_heading("Prior-Art Search Report", level=0)
    for run in h.runs:
        run.font.color.rgb = NAVY
    p = doc.add_paragraph()
    _run(p, "Search mode: ", bold=True); _run(p, model["mode"].replace("_", " ").title())
    _run(p, "     Subject: ", bold=True); _run(p, str(model.get("subject") or "—"))
    _run(p, "     Date: ", bold=True); _run(p, model["generated"])

    doc.add_heading("Search query / invention", level=2)
    doc.add_paragraph(model["query"][:2000])
    doc.add_heading("Method & corpus", level=2)
    mp = doc.add_paragraph(); _run(mp, model["corpus_note"], size=8.5, color=MUTED)

    p = doc.add_paragraph()
    _run(p, f"{model['n_covered']}/{model['n_elements']} elements disclosed", bold=True)
    _run(p, f"   ·   {len(model['references'])} references cited   ·   ")
    _run(p, f"{len(model['uncovered_elements'])} apparently novel", bold=True, color=WARN)

    # ---- executive summary ----
    doc.add_heading("Executive summary", level=1)
    if model["strongest"]:
        doc.add_paragraph("Strongest references:").runs[0].bold = True
        for s in model["strongest"]:
            pp = doc.add_paragraph(style="List Bullet")
            _run(pp, f"{s['pub']} — {s['title']} ", bold=True)
            _run(pp, f"(match {s['score']}; {_basis_label(s['basis'])})", color=MUTED, size=9)
    p = doc.add_paragraph()
    _run(p, "Elements disclosed by the cited art: ", bold=True)
    _run(p, ", ".join(model["covered_elements"]) or "—")
    p = doc.add_paragraph()
    _run(p, "Apparently novel (not found in corpus): ", bold=True)
    _run(p, ", ".join(model["uncovered_elements"]) or "none identified", color=WARN)

    # ---- claim chart ----
    doc.add_heading("Element × Reference claim chart", level=1)
    _claim_chart(doc, model)

    # ---- combination ----
    cv = model["combination_view"]
    if cv.get("primary"):
        doc.add_heading("Combination / inventive-step analysis", level=1)
        p = doc.add_paragraph(); _run(p, "Primary reference: ", bold=True)
        _run(p, f"{cv['primary']} — discloses: {', '.join(cv.get('covers', [])) or '—'}")
        for sec in cv.get("secondaries", []):
            p = doc.add_paragraph(); _run(p, "+ Secondary: ", bold=True)
            _run(p, f"{sec['ref']} — supplies: {', '.join(sec['supplies'])}")
        if cv.get("uncovered_elements"):
            p = doc.add_paragraph(); _run(p, "Not disclosed by any reference: ", bold=True)
            _run(p, ", ".join(cv["uncovered_elements"]), color=WARN)

    # ---- per reference ----
    doc.add_page_break()
    doc.add_heading("Cited references", level=1)
    for i, r in enumerate(model["references"]):
        _reference(doc, r, i + 1)

    # ---- appendix ----
    doc.add_page_break()
    doc.add_heading("Appendix — full ranked reference list", level=1)
    tbl = doc.add_table(rows=1, cols=6); tbl.style = "Light Grid Accent 1"
    hdr = tbl.rows[0].cells
    for j, t in enumerate(["#", "Publication", "Title", "Match", "Basis", "Channels"]):
        hdr[j].paragraphs[0].add_run(t).bold = True
    for a in model["appendix"]:
        c = tbl.add_row().cells
        c[0].text = str(a.get("rank") or ""); c[1].text = a["pub"]
        c[2].text = (a.get("title") or "")[:60]; c[3].text = str(a.get("score") or "")
        c[4].text = _basis_label(a.get("basis")).split(" (")[0]
        c[5].text = ", ".join(a.get("channels", []))

    doc.save(str(out_path))
    return str(out_path)


def _claim_chart(doc, model):
    chart = model["claim_chart"]; cols = chart["columns"]
    tbl = doc.add_table(rows=1, cols=1 + len(cols)); tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = tbl.rows[0].cells
    hdr[0].paragraphs[0].add_run("Invention element").bold = True
    for j, c in enumerate(cols):
        run = hdr[j + 1].paragraphs[0].add_run(c["pub"]); run.bold = True; run.font.size = Pt(7.5)
        hdr[j + 1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    for row in chart["rows"]:
        cells = tbl.add_row().cells
        er = cells[0].paragraphs[0].add_run(row["element"]); er.font.size = Pt(8.5)
        for j, cell in enumerate(row["cells"]):
            para = cells[j + 1].paragraphs[0]; para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if cell.get("covered"):
                run = para.add_run(str(cell["score"])); run.bold = True; run.font.size = Pt(8)
                if cell.get("coord"):
                    para.add_run(f"\n{cell['coord']}").font.size = Pt(6.5)
                _shade(cells[j + 1], _green_shade(cell.get("intensity", 0.5)))
            else:
                para.add_run("·").font.color.rgb = MUTED


def _reference(doc, r, n):
    doc.add_heading(f"{n}. {r['pub']} — {r['title'] or '(untitled)'}", level=2)
    # biblio + drawing side by side via a 2-col table
    tbl = doc.add_table(rows=1, cols=2)
    tbl.columns[0].width = Inches(4.4); tbl.columns[1].width = Inches(2.2)
    left, right = tbl.rows[0].cells
    def line(label, val):
        p = left.add_paragraph(); _run(p, f"{label}: ", bold=True); _run(p, str(val or "—")); p.paragraph_format.space_after = Pt(1)
    line("Assignee", "; ".join(r["assignees"]))
    line("Inventors", ", ".join(r["inventors"]))
    line("Priority / Filing / Published", f"{r['priority_date'] or '—'} / {r['filing_date'] or '—'} / {r['publication_date'] or '—'}")
    line("Family", r["family_id"]); line("CPC", ", ".join(r["cpc"]))
    line("Legal status", r["legal_status"])
    bp = left.add_paragraph(); _run(bp, "Prior-art basis: ", bold=True)
    _run(bp, _basis_label(r["basis"]), color=(GOOD if r["basis"] == "public_prior_art" else SECRET if r["basis"] == "secret_prior_art" else MUTED))
    _run(bp, f"     Match: {r['match_score']}")
    if r.get("covers_elements"):
        cp = left.add_paragraph(); _run(cp, "Reads on: ", bold=True, size=9); _run(cp, ", ".join(r["covers_elements"]), size=9)
    if r.get("drawing_path"):
        try:
            with PILImage.open(r["drawing_path"]) as im:
                w, hh = im.size
            width = Inches(min(2.1, 2.1));
            right.paragraphs[0].add_run().add_picture(r["drawing_path"], width=Inches(2.0))
            cap = right.add_paragraph(); _run(cap, f"{r['pub']} · drawing", size=7, color=MUTED)
        except Exception:
            _run(right.paragraphs[0], "[facsimile not digitized]", size=8, color=WARN)
    else:
        _run(right.paragraphs[0], "[facsimile not digitized]", size=8, color=WARN)
    if r.get("why"):
        p = doc.add_paragraph(); _run(p, "Why relevant: ", bold=True); _run(p, r["why"])
    if r.get("quoted"):
        q = r["quoted"]
        p = doc.add_paragraph(); _run(p, f"Relevant passage ({q['kind']} {q['coord']}, similarity {q['score']}):", bold=True, size=9)
        qp = doc.add_paragraph(); qp.paragraph_format.left_indent = Inches(0.2)
        qr = qp.add_run(q["text"]); qr.italic = True; qr.font.size = Pt(9)
    doc.add_paragraph()
