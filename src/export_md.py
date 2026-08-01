"""Render the export model to a single Markdown file.

WHAT THIS ONE IS FOR
--------------------
The PDF, the DOCX and the XLSX are all *presentation*: they carry drawings, colour, links, layout,
and they carry the reference text only as the one best-matching passage. This export is the
opposite trade. No images, no hyperlinks, no formatting beyond headings — and in exchange it
carries EVERY reference's full claim set and full description, i.e. the patent text the result
page keeps behind a tab and truncates at 60 paragraphs, and that the other three exports drop
entirely.

That makes it the format to feed another reader — a person doing a close read offline, a diff
against a claim set, or another model with a long context. It is plain UTF-8 text, so it stays
small (a 25-reference report is a few hundred KB) and diffs cleanly.

Links are omitted on purpose rather than by oversight: the publication numbers are all present and
are the stable identifier: a bare number resolves at any office, while a baked-in URL is one office
redesign away from being wrong. The scope disclosure travels here in full, same as everywhere else.
"""
from __future__ import annotations

from pathlib import Path

import disclosure

BASIS_LABEL = {"public_prior_art": "PUBLIC prior art",
               "secret_prior_art": "SECRET prior art (novelty only)",
               "priority_interval": "priority-interval art"}


def _clean(s):
    """Flatten whitespace but keep paragraph text intact. Markdown is whitespace-significant, and
    patent text from OCR carries stray newlines mid-sentence that would otherwise break lists and
    headings."""
    return " ".join(str(s or "").split())


def _hdr(lines, text, level=2):
    lines.append("")
    lines.append("#" * level + " " + text)
    lines.append("")


def _kv(lines, k, v):
    if v in (None, "", [], 0) and v != 0:
        return
    lines.append(f"- **{k}:** {v}")


def render(model, out_path):
    L = []

    # ---- header + the honesty layer, first, same as every other surface ----------------------
    L.append(f"# {disclosure.DOC_TITLE}")
    L.append("")
    L.append(f"**{disclosure.DOC_SUBTITLE}**")
    L.append("")
    _kv(L, "Mode", model["mode"].replace("_", " ").title())
    _kv(L, "Subject", model.get("subject") or "—")
    _kv(L, "Generated", model["generated"])
    _kv(L, "References in this file", len(model["references"]))
    _kv(L, "References ranked by the search", model.get("n_cards"))
    _kv(L, "Languages", ", ".join(model.get("languages") or []))

    _hdr(L, "Search query / invention")
    L.append(model["query"])                      # in full — never truncated in this format

    _hdr(L, "Scope and measured reliability — read before relying on this file")
    for heading, body in disclosure.scope_paragraphs():
        L.append(f"**{heading}.** {body}")
        L.append("")
    L.append("**Indexed CPC classes:** " + "; ".join(disclosure.cpc_lines()))
    L.append("")
    L.append(disclosure.not_indexed())
    L.append("")
    L.append("**Method & corpus:** " + model["corpus_note"])

    # ---- executive summary ------------------------------------------------------------------
    _hdr(L, "Executive summary")
    _kv(L, "Elements disclosed by the cited art",
        f"{model['n_covered']}/{model['n_elements']}")
    if model.get("covered_elements"):
        L.append("- **Covered:** " + " · ".join(model["covered_elements"]))
    L.append("- **Apparently novel (not surfaced by this search):** " +
             (" · ".join(model.get("uncovered_elements") or []) or "none identified"))
    if model.get("strongest"):
        L.append("")
        L.append("Strongest references:")
        L.append("")
        for s in model["strongest"]:
            L.append(f"- `{s['pub']}` — {_clean(s.get('title'))} "
                     f"(match {s.get('score')}; {BASIS_LABEL.get(s.get('basis'), 'not dated')})")

    # ---- coverage ledger --------------------------------------------------------------------
    _hdr(L, "Coverage ledger")
    _kv(L, "Rounds", model.get("rounds"))
    _kv(L, "Families surfaced", model.get("n_families_surfaced"))
    _kv(L, "CPC branches searched", ", ".join(model.get("cpc_branches") or []))
    _kv(L, "Retrieval channels", ", ".join(model.get("channels_used") or []))
    if model.get("round_new_families"):
        _kv(L, "New families per round",
            ", ".join(f"r{i + 1}: {n}" for i, n in enumerate(model["round_new_families"])))
    for s in model.get("source_tags") or []:
        state = {"used": "used", "none": "no results", "failed": "failed",
                 "unknown": "not run", "off": "not configured"}.get(s.get("state"), s.get("state"))
        n = f", {s['n']} hits" if s.get("n") else ""
        L.append(f"- **{s.get('label')}:** {state}{n}{('; ' + s['why']) if s.get('why') else ''}")

    # ---- retrieval map ----------------------------------------------------------------------
    _hdr(L, disclosure.CHART_TITLE)
    L.append(f"*{disclosure.CHART_TAG}*")
    L.append("")
    L.append(disclosure.chart_warning())
    L.append("")
    chart = model.get("claim_chart") or {}
    cols = chart.get("columns") or []
    if cols:
        L.append("| Invention element | " + " | ".join(c.get("pub") for c in cols) + " |")
        L.append("|---|" + "---|" * len(cols))
        for row in chart.get("rows") or []:
            cells = []
            for cell in row.get("cells") or []:
                if not cell.get("covered"):
                    cells.append("·")
                    continue
                bit = disclosure.cell_word(cell) + f" {cell.get('score')}"
                if cell.get("coord"):
                    bit += f" ({cell['coord']})"
                cells.append(bit)
            L.append(f"| {_clean(row.get('element'))} | " + " | ".join(cells) + " |")
        L.append("")
        for word, n, gloss in disclosure.legend_lines(chart):
            L.append(f"- **{n} {word}** — {gloss}")
        summ = disclosure.verification_summary(chart)
        if summ:
            L.append("")
            L.append(summ)

    cv = model.get("combination_view") or {}
    if cv.get("primary"):
        _hdr(L, "Combination / inventive-step analysis")
        L.append(f"- **Primary reference:** `{cv['primary']}` — discloses: "
                 f"{', '.join(cv.get('covers') or []) or '—'}")
        for sec in cv.get("secondaries") or []:
            L.append(f"- **+ Secondary:** `{sec['ref']}` — supplies: {', '.join(sec['supplies'])}")
        if cv.get("uncovered_elements"):
            L.append(f"- **Not disclosed by any reference:** {', '.join(cv['uncovered_elements'])}")

    # ---- the references, in full ------------------------------------------------------------
    _hdr(L, "Cited references — full text", 1)
    for i, r in enumerate(model["references"], start=1):
        _hdr(L, f"{i}. {r['pub']} — {_clean(r.get('title')) or '(untitled)'}", 2)
        _kv(L, "Relevancy", f"{r.get('relevancy')}/100"
            + (f" ({r['relevancy_source']})" if r.get("relevancy_source") else ""))
        _kv(L, "Rank in this search", r.get("rank"))
        _kv(L, "Jurisdiction", r.get("country"))
        _kv(L, "Legal status", r.get("status_label"))
        _kv(L, "Prior-art basis", BASIS_LABEL.get(r.get("basis"), "not dated"))
        _kv(L, "Priority / filed / published",
            f"{r.get('priority_date') or '—'} / {r.get('filing_date') or '—'} / "
            f"{r.get('publication_date') or '—'}")
        _kv(L, "Assignee", "; ".join(r.get("assignees") or []))
        _kv(L, "Inventors", ", ".join(r.get("inventors") or []))
        _kv(L, "CPC", ", ".join(r.get("cpc") or []))
        _kv(L, "Family", r.get("family_summary"))
        if r.get("family_timeline"):
            _kv(L, "Family timeline",
                "; ".join(f"{yr}: {', '.join(ccs)}" for yr, ccs in r["family_timeline"] if yr))
        if r.get("family_members"):
            _kv(L, "Family members", ", ".join(r["family_members"]))
        _kv(L, "Legal events", r.get("legal_status"))
        _kv(L, "Found via", ", ".join(r.get("found_via") or []))
        _kv(L, "Figures", r.get("n_images"))
        _kv(L, "Reads on", " · ".join(r.get("covers_elements") or []))
        _kv(L, "Best-passage match", r.get("match_score"))

        if r.get("relevancy_opinion"):
            _hdr(L, "Why relevant", 4)
            L.append(r["relevancy_opinion"])
        if r.get("why") and r["why"] != r.get("relevancy_opinion"):
            _hdr(L, "Grounded rationale", 4)
            L.append(r["why"])
            if r.get("reads_on"):
                L.append("")
                L.append("Reads on: " + ", ".join(r["reads_on"]))
        if r.get("quoted"):
            q = r["quoted"]
            _hdr(L, f"Best-matching passage ({q.get('kind')} {q.get('coord')}, "
                    f"similarity {q.get('score')})", 4)
            L.append("> " + _clean(q.get("text")))

        if r.get("abstract"):
            _hdr(L, "Abstract", 4)
            L.append(_clean(r["abstract"]))

        #  THE POINT OF THIS FORMAT. `text` is only populated when assemble(include_text=True), so
        #  a model built for the PDF/DOCX/XLSX path simply has no full text and this degrades to
        #  the abstract above rather than raising.
        t = r.get("text") or {}
        claims = t.get("claims") or []
        if claims:
            _hdr(L, f"Claims ({len(claims)})", 4)
            for c in claims:
                num = c.get("claim_no")
                tag = " *(independent)*" if c.get("independent") else ""
                L.append(f"**{num if num is not None else '—'}.**{tag} {_clean(c.get('text'))}")
                L.append("")
        desc = t.get("description") or []
        if desc:
            _hdr(L, f"Description ({len(desc)} paragraphs)", 4)
            for p in desc:
                if p.get("heading"):
                    L.append(f"*{_clean(p['heading'])}*")
                    L.append("")
                num = f"[{p['para_no']}] " if p.get("para_no") else ""
                L.append(num + _clean(p.get("text")))
                L.append("")
        caps = t.get("figure_caps") or []
        if caps:
            _hdr(L, f"Figure captions ({len(caps)})", 4)
            for f in caps:
                L.append(f"- **{f.get('figure_no') or '—'}** {_clean(f.get('caption'))}")
        if not (claims or desc):
            L.append("")
            L.append("*No claims or description text is held for this publication — the corpus has "
                     "only bibliographic data for it. See the scope section on text depth.*")
        L.append("")
        L.append("---")

    # ---- appendix ---------------------------------------------------------------------------
    _hdr(L, "Appendix — full ranked reference list", 1)
    L.append("| # | Publication | Title | Match | Basis | Channels |")
    L.append("|---|---|---|---|---|---|")
    for a in model.get("appendix") or []:
        L.append(f"| {a.get('rank') or ''} | {a.get('pub')} | {_clean(a.get('title'))} | "
                 f"{a.get('score') or ''} | {BASIS_LABEL.get(a.get('basis'), 'not dated')} | "
                 f"{', '.join(a.get('channels') or [])} |")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return str(out)
