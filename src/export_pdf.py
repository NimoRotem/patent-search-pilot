"""Render the export model to a print-quality PDF (reportlab platypus). Milestone 4 §1."""
from __future__ import annotations
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                Image, PageBreak, KeepTogether, HRFlowable)
from reportlab.pdfgen import canvas as _canvas
from PIL import Image as PILImage
import html as _html

import disclosure

NAVY = colors.HexColor("#0b2545")
BLUE = colors.HexColor("#0050A0")
ACCENT = colors.HexColor("#2a6cf0")
GOOD = colors.HexColor("#0a7d4d")
SECRET = colors.HexColor("#7b2fbf")
MUTED = colors.HexColor("#5b6b82")
LINE = colors.HexColor("#c9d4e5")
LIGHT = colors.HexColor("#f3f6fb")


def _styles():
    ss = getSampleStyleSheet()
    S = {}
    S["title"] = ParagraphStyle("t", parent=ss["Title"], textColor=NAVY, fontSize=22, leading=26, spaceAfter=6)
    S["sub"] = ParagraphStyle("sub", parent=ss["Normal"], textColor=MUTED, fontSize=10, leading=14)
    S["h2"] = ParagraphStyle("h2", parent=ss["Heading2"], textColor=NAVY, fontSize=14, spaceBefore=14, spaceAfter=6)
    S["h3"] = ParagraphStyle("h3", parent=ss["Heading3"], textColor=BLUE, fontSize=11.5, spaceBefore=8, spaceAfter=3)
    S["body"] = ParagraphStyle("b", parent=ss["Normal"], fontSize=9.5, leading=13, textColor=colors.HexColor("#1c2a44"))
    S["small"] = ParagraphStyle("s", parent=ss["Normal"], fontSize=8, leading=10.5, textColor=MUTED)
    S["quote"] = ParagraphStyle("q", parent=ss["Normal"], fontSize=9, leading=12.5,
                                leftIndent=8, rightIndent=4, spaceBefore=6, spaceAfter=2,
                                textColor=colors.HexColor("#26303f"),
                                backColor=colors.HexColor("#fffbe6"), borderPadding=6,
                                borderColor=colors.HexColor("#e9d68a"), borderWidth=0.5)
    S["cell"] = ParagraphStyle("c", parent=ss["Normal"], fontSize=7.5, leading=9, alignment=TA_CENTER)
    S["cellL"] = ParagraphStyle("cl", parent=ss["Normal"], fontSize=8, leading=10)
    S["chip"] = ParagraphStyle("ch", parent=ss["Normal"], fontSize=7.5, textColor=NAVY)
    S["warn"] = ParagraphStyle("w", parent=ss["Normal"], fontSize=8.5, leading=11.5,
                               textColor=colors.HexColor("#8a3b00"),
                               backColor=colors.HexColor("#fff6e8"), borderPadding=6,
                               borderColor=colors.HexColor("#e8c9a0"), borderWidth=0.5,
                               spaceBefore=4, spaceAfter=6)
    S["scope"] = ParagraphStyle("sc", parent=ss["Normal"], fontSize=9, leading=12,
                                textColor=colors.HexColor("#1c2a44"), spaceAfter=4)
    return S


def _esc(s):
    return _html.escape(str(s or ""))


def _basis_label(b):
    return {"public_prior_art": "PUBLIC prior art", "secret_prior_art": "SECRET prior art (novelty only)",
            "priority_interval": "priority-interval art"}.get(b, "not dated")


def _basis_color(b):
    return {"public_prior_art": GOOD, "secret_prior_art": SECRET}.get(b, MUTED)


def _scaled_image(path, max_w, max_h):
    try:
        with PILImage.open(path) as im:
            w, h = im.size
        ratio = min(max_w / w, max_h / h)
        return Image(path, width=w * ratio, height=h * ratio)
    except Exception:
        return None


def _header_footer(cv: _canvas.Canvas, doc):
    cv.saveState()
    cv.setFont("Helvetica", 7.5)
    cv.setFillColor(MUTED)
    cv.drawString(0.75 * inch, 0.5 * inch,
                  disclosure.DOC_TITLE + " · machine-generated drafting aid, verify every cell "
                  "· CONFIDENTIAL — attorney work product")
    cv.drawRightString(7.75 * inch, 0.5 * inch, f"Page {doc.page}")
    cv.setStrokeColor(LINE); cv.setLineWidth(0.5)
    cv.line(0.75 * inch, 0.62 * inch, 7.75 * inch, 0.62 * inch)
    cv.restoreState()


_NARRATIVE_HEADINGS = (("purpose", "Purpose of this search"),
                       ("key_findings", "Key findings"),
                       ("analysis", "Analysis"))


def _letterhead(model, S):
    """The firm/client block that turns a retrieval export into work product.

    Entirely optional: with no report document, or an empty one, this returns nothing and the
    export is byte-for-byte what it was before the feature existed.
    """
    doc = model.get("report_doc") or {}
    if not any(str(doc.get(k) or "").strip() for k in
               ("firm_name", "firm_address", "firm_attorney", "attorney_email", "firm_detail",
                "client_name", "client_reference_number", "matter_title",
                "subject_patent_number", "subject_patent_date")):
        return []
    out = []
    logo = model.get("report_logo")
    left = []
    if doc.get("firm_name"):
        left.append(Paragraph(f"<b>{_esc(doc['firm_name'])}</b>", S["h3"]))
    for key in ("firm_address", "firm_attorney", "attorney_email", "firm_detail"):
        if doc.get(key):
            left.append(Paragraph(_esc(doc[key]), S["small"]))
    if logo:
        img = _scaled_image(logo, 2.0 * inch, 0.9 * inch)
        head = Table([[left or [Paragraph("", S["small"])], img or ""]],
                     colWidths=[4.3 * inch, 2.5 * inch])
        head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        out.append(head)
    else:
        out.extend(left)
    out.append(HRFlowable(width="100%", thickness=1, color=LINE, spaceBefore=6, spaceAfter=8))

    matter = [(k.replace("_", " ").title(), doc.get(k)) for k in
              ("matter_title", "client_name", "client_reference_number",
               "subject_patent_number", "subject_patent_date")]
    _LABEL = {"Matter Title": "Matter", "Client Name": "Client",
              "Client Reference Number": "Client reference",
              "Subject Patent Number": "Subject application",
              "Subject Patent Date": "Subject date"}
    matter = [(_LABEL.get(k, k), v) for k, v in matter if str(v or "").strip()]
    if matter:
        t = Table([[Paragraph(f"<b>{_esc(k)}</b>", S["small"]), Paragraph(_esc(v), S["small"])]
                   for k, v in matter], colWidths=[1.6 * inch, 5.2 * inch])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        out.append(t)
        out.append(Spacer(1, 6))
    return out


def _narrative(model, S):
    """Purpose / key findings / analysis, printed above the evidence."""
    doc = model.get("report_doc") or {}
    out = []
    for key, heading in _NARRATIVE_HEADINGS:
        body = str(doc.get(key) or "").strip()
        if not body:
            continue
        out.append(Paragraph(heading, S["h2"]))
        for para in [p for p in body.split("\n") if p.strip()]:
            out.append(Paragraph(_esc(para), S["body"]))
    return out


def render(model, out_path):
    S = _styles()
    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                            leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch,
                            title=f"{disclosure.DOC_TITLE} — {model['title']}")
    story = []

    # ---- letterhead + matter, when the author has filled them in ----
    # A search report that a firm sends to a client opens with who prepared it and for whom. When
    # nothing has been entered the block is omitted entirely rather than printed empty, so an
    # internal export stays exactly as it was.
    story.extend(_letterhead(model, S))

    # ---- cover ----
    story.append(Spacer(1, 0.6 * inch))
    story.append(Paragraph(disclosure.DOC_TITLE, S["title"]))
    story.append(Paragraph(f"<b>{_esc(disclosure.DOC_SUBTITLE)}</b>", S["sub"]))
    story.append(HRFlowable(width="100%", thickness=2, color=BLUE, spaceAfter=10))
    mode = model["mode"].replace("_", " ").title()
    subj = f'{model.get("subject") or "—"}'
    meta = (f"<b>Search mode:</b> {mode} &nbsp;&nbsp; <b>Subject:</b> {_esc(subj)} &nbsp;&nbsp; "
            f"<b>Date:</b> {model['generated']}")
    story.append(Paragraph(meta, S["body"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Search query / invention", S["h3"]))
    story.append(Paragraph(_esc(model["query"])[:1600], S["body"]))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Method & corpus", S["h3"]))
    story.append(Paragraph(_esc(model["corpus_note"]), S["small"]))
    story.append(Spacer(1, 12))
    # summary stats box
    stat = [[Paragraph(f"<b>{model['n_covered']}/{model['n_elements']}</b>", S["cell"]),
             Paragraph(f"<b>{len(model['references'])}</b>", S["cell"]),
             Paragraph(f"<b>{len(model['uncovered_elements'])}</b>", S["cell"]),
             Paragraph(f"<b>{', '.join(model['languages']).upper()}</b>", S["cell"])],
            [Paragraph("elements disclosed", S["small"]), Paragraph("references cited", S["small"]),
             Paragraph("novel (uncovered)", S["small"]), Paragraph("languages", S["small"])]]
    t = Table(stat, colWidths=[1.6 * inch] * 4)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
                           ("BACKGROUND", (0, 0), (-1, 0), LIGHT), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(t)

    # ---- scope + measured reliability ----
    # Printed in full on the cover page. The web UI could link this; a filed PDF cannot.
    story.append(Paragraph("Scope and measured reliability — read before relying on this document",
                           S["h2"]))
    for heading, body in disclosure.scope_paragraphs():
        story.append(Paragraph(f"<b>{_esc(heading)}.</b> {_esc(body)}", S["scope"]))
    story.append(Paragraph("<b>Indexed CPC classes:</b> " + _esc("; ".join(disclosure.cpc_lines())),
                           S["small"]))
    story.append(Paragraph(_esc(disclosure.not_indexed()), S["small"]))

    # ---- the author's own framing, above the machine's ----
    # Purpose / key findings / analysis are what the reader of a client report actually reads
    # first. They sit after the scope disclosure (so nobody reaches a conclusion before reading
    # the limits) and before the executive summary the tool generates for itself.
    story.extend(_narrative(model, S))

    # ---- executive summary ----
    story.append(Paragraph("Executive summary", S["h2"]))
    if model["strongest"]:
        story.append(Paragraph("Strongest references:", S["h3"]))
        for s in model["strongest"]:
            story.append(Paragraph(
                f"• <b>{_esc(s['pub'])}</b> — {_esc(s['title'])} "
                f"<font color='#5b6b82'>(match {s['score']}; {_basis_label(s['basis'])})</font>", S["body"]))
    disc = ", ".join(model["covered_elements"]) or "—"
    unc = ", ".join(model["uncovered_elements"]) or "none identified"
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"<b>Elements disclosed by the cited art ({model['n_covered']}/{model['n_elements']}):</b> {_esc(disc)}", S["body"]))
    story.append(Paragraph(f"<b>Apparently novel (not found in corpus):</b> <font color='#b25e00'>{_esc(unc)}</font>", S["body"]))

    # ---- claim chart ----
    story.append(Paragraph(disclosure.CHART_TITLE + " <font size=8 color='#8a3b00'>["
                           + _esc(disclosure.CHART_TAG) + "]</font>", S["h2"]))
    story.append(Paragraph(_esc(disclosure.chart_warning()), S["warn"]))
    story.append(_claim_chart_table(model, S))
    legend = " &nbsp;·&nbsp; ".join(f"<b>{n} {_esc(w)}</b> — {_esc(lbl)}"
                                    for w, n, lbl in disclosure.legend_lines(model["claim_chart"]))
    if legend:
        story.append(Paragraph("<b>Legend:</b> " + legend, S["small"]))
    _summ = disclosure.verification_summary(model["claim_chart"])
    if _summ:
        story.append(Paragraph("<b>" + _esc(_summ) + "</b>", S["small"]))

    # ---- combination / inventive-step ----
    cv = model["combination_view"]
    if cv.get("primary"):
        story.append(Paragraph("Combination / inventive-step analysis", S["h2"]))
        story.append(Paragraph(f"<b>Primary reference:</b> {_esc(cv['primary'])} — discloses: "
                               f"{_esc(', '.join(cv.get('covers', [])) or '—')}", S["body"]))
        for sec in cv.get("secondaries", []):
            story.append(Paragraph(f"<b>+ Secondary:</b> {_esc(sec['ref'])} — supplies: "
                                   f"{_esc(', '.join(sec['supplies']))}", S["body"]))
        if cv.get("uncovered_elements"):
            story.append(Paragraph(f"<b>Elements not disclosed by any reference:</b> "
                                   f"<font color='#b25e00'>{_esc(', '.join(cv['uncovered_elements']))}</font>", S["body"]))

    # ---- per-reference ----
    story.append(PageBreak())
    story.append(Paragraph("Cited references", S["h2"]))
    for i, r in enumerate(model["references"]):
        story.append(_reference_block(r, i + 1, S))
        story.append(Spacer(1, 6))
        story.append(HRFlowable(width="100%", thickness=0.5, color=LINE, spaceAfter=6))

    # ---- appendix ----
    story.append(PageBreak())
    story.append(Paragraph("Appendix — full ranked reference list", S["h2"]))
    ap_rows = [[Paragraph("<b>#</b>", S["small"]), Paragraph("<b>Publication</b>", S["small"]),
                Paragraph("<b>Title</b>", S["small"]), Paragraph("<b>Match</b>", S["small"]),
                Paragraph("<b>Basis</b>", S["small"]), Paragraph("<b>Channels</b>", S["small"])]]
    for a in model["appendix"]:
        ap_rows.append([Paragraph(str(a.get("rank") or ""), S["small"]),
                        Paragraph(_esc(a["pub"]), S["small"]),
                        Paragraph(_esc((a.get("title") or "")[:60]), S["small"]),
                        Paragraph(str(a.get("score") or ""), S["small"]),
                        Paragraph(_basis_label(a.get("basis")).split(" (")[0], S["small"]),
                        Paragraph(_esc(", ".join(a.get("channels", []))), S["small"])])
    ap = Table(ap_rows, colWidths=[0.3*inch, 1.2*inch, 2.7*inch, 0.6*inch, 1.1*inch, 1.1*inch], repeatRows=1)
    ap.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.25, LINE),
                            ("BACKGROUND", (0, 0), (-1, 0), LIGHT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story.append(ap)

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    return str(out_path)


def _claim_chart_table(model, S):
    chart = model["claim_chart"]
    cols = chart["columns"]
    header = [Paragraph("<b>Invention element</b>", S["cellL"])] + \
             [Paragraph(f"<b>{_esc(c['pub'])}</b>", S["cell"]) for c in cols]
    rows = [header]
    styles = [("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.25, LINE),
              ("BACKGROUND", (0, 0), (-1, 0), LIGHT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    for ri, row in enumerate(chart["rows"], start=1):
        cells = [Paragraph(_esc(row["element"]), S["cellL"])]
        for ci, cell in enumerate(row["cells"], start=1):
            if cell.get("covered"):
                #  Appearance is driven by the VERIFICATION VERDICT, never by the retrieval score.
                #  The old code shaded green by `intensity` (the normalised fused score), so the
                #  most confidently-retrieved cell was the greenest -- even when the verifier had
                #  judged it unrelated. See disclosure.CELL_STATES.
                _mark, _label, fill, _cov = disclosure.cell_state(cell)
                word = disclosure.cell_word(cell)
                txt = f"<b>{_esc(word)}</b><br/><font size=6 color='#5b6b82'>{_esc(cell.get('score'))}</font>"
                if cell.get("coord"):
                    txt += f"<br/><font size=6 color='#5b6b82'>{_esc(cell['coord'])}</font>"
                cells.append(Paragraph(txt, S["cell"]))
                styles.append(("BACKGROUND", (ci, ri), (ci, ri), colors.HexColor("#" + fill)))
            else:
                cells.append(Paragraph("·", S["cell"]))
        rows.append(cells)
    n = len(cols)
    cw = [2.2 * inch] + [(5.3 * inch) / max(1, n)] * n
    t = Table(rows, colWidths=cw, repeatRows=1)
    t.setStyle(TableStyle(styles))
    return t


def _reference_block(r, n, S):
    flow = []
    #  Rank + relevancy in the heading, and the card's own family / status / provenance lines in
    #  the biblio, so the printed reference says what the on-screen card said. The bare DOCDB
    #  family id is an internal key: prefer the readable "Family of N in M jurisdictions".
    rel = (f" &nbsp;<font size=8 color='#5b6b82'>[relevancy {r['relevancy']}/100]</font>"
           if r.get("relevancy") is not None else "")
    title = f"{n}. {_esc(r['pub'])} — {_esc(r['title'] or '(untitled)')}{rel}"
    flow.append(Paragraph(title, S["h3"]))
    basis = _basis_label(r["basis"])
    biblio = (f"<b>Assignee:</b> {_esc('; '.join(r['assignees']) or '—')} &nbsp; "
              f"<b>Inventors:</b> {_esc(', '.join(r['inventors']) or '—')}<br/>"
              f"<b>Priority:</b> {_esc(r['priority_date'] or '—')} &nbsp; "
              f"<b>Filing:</b> {_esc(r['filing_date'] or '—')} &nbsp; "
              f"<b>Published:</b> {_esc(r['publication_date'] or '—')} &nbsp; "
              f"<b>Family:</b> {_esc(r.get('family_summary') or r.get('family_id') or '—')}<br/>"
              f"<b>CPC:</b> {_esc(', '.join(r['cpc']) or '—')}<br/>"
              f"<b>Legal status:</b> {_esc(r.get('status_label') or r.get('legal_status') or '—')} &nbsp; "
              f"<b>Prior-art basis:</b> <font color='#{_basis_color(r['basis']).hexval()[2:]}'>{basis}</font> &nbsp; "
              f"<b>Match:</b> {r['match_score']}")
    if r.get("found_via"):
        biblio += f"<br/><b>Found via:</b> {_esc(', '.join(r['found_via']))}"
    left = [Paragraph(biblio, S["body"])]
    if r.get("covers_elements"):
        left.append(Spacer(1, 3))
        left.append(Paragraph(f"<b>Reads on:</b> {_esc(', '.join(r['covers_elements']))}", S["small"]))
    #  The reranker's written take and the separately grounded rationale are different sentences
    #  produced by different prompts; print both when they differ instead of picking one.
    if r.get("relevancy_opinion"):
        left.append(Spacer(1, 3))
        left.append(Paragraph(f"<b>Why relevant:</b> {_esc(r['relevancy_opinion'])}", S["body"]))
    if r.get("why") and r["why"] != r.get("relevancy_opinion"):
        left.append(Spacer(1, 3))
        left.append(Paragraph(f"<b>Grounded rationale:</b> {_esc(r['why'])}", S["body"]))
    # drawing on the right
    img = _scaled_image(r["drawing_path"], 2.0 * inch, 2.0 * inch) if r.get("drawing_path") else None
    if img:
        cap = Paragraph(f"<font size=7 color='#5b6b82'>{_esc(r['pub'])} · drawing</font>", S["small"])
        right = [img, cap]
        block = Table([[left, right]], colWidths=[4.6 * inch, 2.4 * inch])
        block.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        flow.append(block)
    else:
        flow.extend(left)
        flow.append(Paragraph("<font size=7 color='#b25e00'>[facsimile not digitized for this document]</font>", S["small"]))
    # quoted matched passage
    if r.get("quoted"):
        q = r["quoted"]
        flow.append(Spacer(1, 6))
        flow.append(Paragraph(f"<b>Relevant passage</b> ({_esc(q['kind'])} {_esc(q['coord'])}, similarity {q['score']}):", S["small"]))
        flow.append(Spacer(1, 2))
        flow.append(Paragraph(_esc(q["text"]), S["quote"]))
    return KeepTogether(flow)
