"""Assemble the export data model for a litigation-grade prior-art report (Milestone 4 §1).

Reuses the cached agent report + webview (claim chart, biblio, matched coordinate, sections) +
enrich_display (drawings, PDF, rich biblio, legal status) + the rationale cache. Everything is
local: drawing images are the files already downloaded under data/figures/<pub>/.
"""
from __future__ import annotations
import json
from datetime import date
from pathlib import Path
import db, embed, webview, enrich_display
from config import DATA

REPORTS = DATA / "reports"
RATIONALE = DATA / "rationale"

CORPUS_NOTE = ("Semantic + agentic search over a 107,795-publication vacuum-gripping corpus "
               "(US/EP/WO/DE, all dates; 1,819,616 embedded passages). Retrieval: 8-channel "
               "adaptive cascade (dense + BM25 + CPC + citation/family + query-by-example + "
               "cross-lingual) with weighted reciprocal-rank fusion and a cross-encoder reranker. "
               "Prior-art dating by a jurisdiction-neutral novelty/inventive-step engine.")


def _load_report(slug):
    p = REPORTS / f"{slug}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _rationale(slug, pub):
    c = RATIONALE / f"{slug}__{pub}.json"
    if c.exists():
        try:
            return json.loads(c.read_text())
        except Exception:
            return None
    return None


def _best_drawing(pub):
    """Return (abs_path, caption) for the first locally-downloaded figure, or (None, None)."""
    disp = enrich_display.load_cached(pub)
    disp = (disp or {}).get("_display") if disp else None
    if not disp:
        disp = enrich_display.enrich_for_display(pub)
    imgs = (disp or {}).get("images") or []
    if imgs:
        p = enrich_display.FIGDIR / pub / imgs[0]["file"]
        if p.exists():
            return str(p), "Fig. 1"
    return None, None


def assemble(slug, selected_pubs, top_n=25):
    report = _load_report(slug)
    if not report:
        raise ValueError(f"no cached report for {slug}")
    view = webview.build_view(report, top_n=max(top_n, len(selected_pubs) + 5))
    by_pub = {c["pub"]: c for c in view["cards"]}
    # honour the FULL selection: ranked-order first, then any selected pubs not in the top cards
    want = list(dict.fromkeys(selected_pubs))                  # de-dupe, preserve order
    ranked = [c["pub"] for c in view["cards"]]
    selected = [p for p in ranked if p in set(want)]
    selected += [p for p in want if p not in set(selected)]    # append the rest (still exported)

    query = report.get("query", "")
    qvec = embed.embed_query(query[:8000], 768) if query else None

    conn = db.connect(); conn.autocommit = True
    cur = conn.cursor()
    refs = []
    for pub in selected:
        card = by_pub.get(pub, {})
        cur.execute("SELECT id FROM publications WHERE publication_number=%s LIMIT 1", (pub,))
        row = cur.fetchone()
        pid = row["id"] if row else None
        b = webview.biblio(cur, pid) if pid else {}
        secs = webview.sections(cur, pid) if pid else {}
        disp = enrich_display.enrich_for_display(pub)
        # matched passage to quote
        quoted = None
        if pid and qvec is not None:
            m = webview.match_in_pub(cur, pid, qvec)
            if m and m.get("text"):
                quoted = {"kind": m["kind"], "coord": webview._coord_str(m["coord"]),
                          "text": m["text"][:1200], "score": round(m.get("score", 0), 3)}
        draw_path, draw_cap = _best_drawing(pub)
        rat = _rationale(slug, pub) or {}
        # legal status: prefer enrichment events
        legal = None
        ev = (disp or {}).get("legal_events") or b.get("legal_events") or []
        if ev:
            legal = "; ".join(f"{e.get('code') or ''} {e.get('date') or ''}".strip()
                              for e in ev[:3] if e)
        refs.append({
            "rank": card.get("rank"),
            "pub": pub,
            "title": b.get("title") or (disp or {}).get("title"),
            "flag": b.get("flag", ""),
            "country": b.get("country") or (disp or {}).get("country"),
            "assignees": b.get("assignees") or (disp or {}).get("assignees") or [],
            "inventors": (b.get("inventors") or (disp or {}).get("inventors") or [])[:5],
            "priority_date": b.get("priority_date") or (disp or {}).get("priority_date"),
            "filing_date": b.get("filing_date") or (disp or {}).get("filing_date"),
            "publication_date": b.get("publication_date") or (disp or {}).get("publication_date"),
            "family_id": b.get("family_id") or (disp or {}).get("family_id"),
            "cpc": [c["code"] for c in (b.get("cpc") or [])][:8] or
                   [c["code"] for c in ((disp or {}).get("classifications") or [])][:8],
            "legal_status": legal,
            "basis": card.get("basis", "n/a"),
            "channels": card.get("channels", []),
            "match_score": card.get("match_score"),
            "covers_elements": card.get("covers_elements", []),
            "abstract": b.get("abstract") or (disp or {}).get("abstract"),
            "drawing_path": draw_path, "drawing_caption": draw_cap,
            "quoted": quoted,
            "why": rat.get("why", ""), "reads_on": rat.get("reads_on", []),
            "google_patents": (disp or {}).get("google_patents"),
            "espacenet": (disp or {}).get("espacenet"),
        })
    cur.close(); conn.close()

    # exec summary
    elements = report["elements"]
    covered = set()
    for r in refs:
        covered.update(r["covers_elements"])
    cv = report.get("combination_view", {})
    uncovered = cv.get("uncovered_elements", [])
    strongest = sorted(refs, key=lambda r: r.get("match_score") or 0, reverse=True)[:3]

    return {
        "slug": slug, "title": view.get("title", slug),
        "query": query, "mode": report.get("mode", "novelty"),
        "subject": report.get("subject"), "subject_flag": view.get("subject_flag", ""),
        "generated": date.today().isoformat(),
        "corpus_note": CORPUS_NOTE,
        "elements": elements,
        "n_elements": len(elements),
        "n_covered": len(covered), "covered_elements": sorted(covered),
        "uncovered_elements": uncovered,
        "claim_chart": view["claim_chart"],
        "combination_view": cv,
        "strongest": [{"pub": r["pub"], "title": r["title"], "score": r["match_score"],
                       "basis": r["basis"]} for r in strongest],
        "references": refs,
        "appendix": [{"rank": c.get("rank"), "pub": c["pub"], "title": c.get("title"),
                      "score": c.get("match_score"), "basis": c.get("basis"),
                      "channels": c.get("channels", [])}
                     for c in view["cards"]],
        "channels_used": report.get("channels_used", []),
        "languages": report.get("languages", []),
    }
