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
import pubnorm  # single link-builder: zero-padded Google/Espacenet URLs (dropped-zero fix)
from config import DATA

REPORTS = DATA / "reports"
RATIONALE = DATA / "rationale"

_METHOD_NOTE = ("Retrieval: 8-channel adaptive cascade (dense + BM25 + CPC + citation/family + "
                "query-by-example + cross-lingual) with weighted reciprocal-rank fusion and a "
                "cross-encoder reranker. Prior-art dating by a jurisdiction-neutral "
                "novelty/inventive-step engine.")


def corpus_note():
    """Method + corpus sentence for the export cover.

    The publication count used to be the string literal "107,795" baked in here. A weekly
    incremental ingest moves that number, so a filed report could state a corpus size the tool no
    longer had -- read it live, and degrade to no number rather than to a wrong one.
    """
    import corpus_facts
    f = corpus_facts.facts()
    n = f"{f['publications']:,}-publication" if f.get("publications") else ""
    juris = "/".join(f.get("jurisdictions") or [])
    chunks = f" ({f['chunks']:,} embedded passages)" if f.get("chunks") else ""
    return (f"Semantic + agentic search over a {n} vacuum-gripping corpus "
            f"[{juris}; current to {f.get('max_date_str') or 'unknown'}]{chunks}. {_METHOD_NOTE}")


# Back-compat for anything importing the constant directly.
CORPUS_NOTE = ("Semantic + agentic search over a bounded vacuum-gripping corpus. " + _METHOD_NOTE)


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


def _best_drawing(pub, card=None):
    """Return (abs_path, caption) for a figure ON DISK for this publication, or (None, None).

    An export embeds bytes, so a remote URL is not enough — it has to become a local file. Two
    kinds of figure record reach us and only one used to work here:

      * recovered figures      {"file": "ops000.png"}  -> already under data/figures/<pub>/
      * lemad-Mongo figures    {"full": "https://…"}   -> a Google-CDN URL, nothing downloaded

    Since the Mongo fast path landed, the *shown* drawing on most cards is the second kind, so
    every such reference exported with "[facsimile not digitized]" while the screen showed a
    perfectly good sketch. Fetch the CDN image once into the same figure dir the recovered ones
    live in (so it is cached for the next export and served by /figures like any other) and hand
    back the path. A failed fetch degrades to no drawing, never to an exception.
    """
    def _first_local(imgs):
        for im in imgs or []:
            if im.get("file"):
                try:
                    p = enrich_display.FIGDIR / enrich_display._pubkey(pub) / im["file"]
                except ValueError:
                    return None
                if p.exists() and p.stat().st_size > 0:
                    return p
        return None

    def _first_remote(imgs):
        for im in imgs or []:
            url = im.get("full") or im.get("thumbnail")
            if url and str(url).startswith(("http://", "https://")):
                return url
        return None

    pools = []
    if card and card.get("images"):
        pools.append(card["images"])
    cached = enrich_display.load_cached(pub) or {}
    pools.append((cached.get("_display") or {}).get("images"))

    for imgs in pools:
        p = _first_local(imgs)
        if p:
            return str(p), "Fig. 1"

    for imgs in pools:
        url = _first_remote(imgs)
        if not url:
            continue
        try:
            figdir = enrich_display.FIGDIR / enrich_display._pubkey(pub)
        except ValueError:
            return None, None
        figdir.mkdir(parents=True, exist_ok=True)
        dest = figdir / ("cdn000" + enrich_display._fig_ext(url))
        try:
            if enrich_display._download(url, dest) and dest.exists() and dest.stat().st_size > 0:
                return str(dest), "Fig. 1"
        except Exception:
            pass

    # Last resort: run the live recovery chain (SerpApi / Google page / OPS / PDF raster).
    try:
        disp = enrich_display.enrich_for_display(pub) or {}
    except Exception:
        return None, None
    p = _first_local(disp.get("images"))
    return (str(p), "Fig. 1") if p else (None, None)


def _prov_labels(card):
    """"Found via" labels exactly as the card chips read them: local retrieval channels plus the
    external APIs / image channel that surfaced this reference."""
    out = [str(x) for x in (card.get("channels") or [])]
    for p in card.get("prov") or []:
        lbl = p.get("label")
        if lbl and lbl not in out:
            out.append(lbl)
    return out


def _family_line(card):
    """The card's one-line family summary ("Family of 3 in 2 jurisdictions"), or None."""
    n, j = card.get("family_n"), card.get("family_juris")
    if not n:
        return None
    src = " (corpus-only)" if card.get("family_source") == "corpus" else ""
    return (f"Family of {n} in {j} jurisdiction{'s' if j != 1 else ''}{src}")


def _family_flat(card):
    """[(year, [country codes])] from the card's worldwide family timeline — the year → jurisdiction
    strip the card renders, flattened for a spreadsheet cell / a Markdown line."""
    return [(g.get("year"), [c.get("cc") for c in (g.get("codes") or []) if c.get("cc")])
            for g in (card.get("family_timeline") or [])]


def _attach_verification(slug, view, report):
    """Make sure the exported claim chart carries the SAME per-cell disclosure verdicts the web
    page shows.

    This was the defect behind the whole export-accuracy problem: assemble() calls
    webview.build_view() fresh, and build_view does not verify anything -- verification is applied
    afterwards, by webapp._build_view_cached(), and only to the cached view the browser reads. So
    every exported PDF/DOCX was built from a chart whose cells had no `verify` key at all, and the
    exporters happily shaded them green by retrieval score.

    Prefer the verdicts already cached next to the report (free, and identical to what the user saw
    on screen); only fall back to running the verifier if no cached view exists. Matching is by
    (element, pub) rather than by position because the export may request a different top_n and
    therefore a different column set.
    """
    chart = view.get("claim_chart") or {}
    #  A READING chart already carries its verdict on every cell, and it is a stronger one than
    #  anything this function can supply: a verbatim quote that was found in the reference, located
    #  to a real passage by code, and put to an independent refuter. verify_matrix below looks a
    #  cell up in report["element_evidence"] by coordinate, and a reading cell is not in there, so
    #  running it would demote every one of them to "no-coord" — which is exactly what it did to
    #  the web page before this guard existed.
    if chart.get("source") == "reading":
        return
    cached = REPORTS / f"{slug}.view.json"
    verdicts = {}
    if cached.exists():
        try:
            cv = (json.loads(cached.read_text()).get("claim_chart") or {})
            for row in cv.get("rows", []):
                for c in row.get("cells", []):
                    if c.get("covered") and c.get("verify"):
                        verdicts[(row.get("element"), c.get("pub"))] = (c["verify"], c.get("verify_why"))
        except Exception:
            verdicts = {}
    hit = miss = 0
    for row in chart.get("rows", []):
        for c in row.get("cells", []):
            if not c.get("covered"):
                continue
            v = verdicts.get((row.get("element"), c.get("pub")))
            if v:
                c["verify"], c["verify_why"] = v[0], v[1]
                hit += 1
            else:
                miss += 1
    if miss:
        # No cached verdict for at least one rendered cell. Run the verifier rather than exporting
        # unverified cells that would print as "unchecked" -- but never fail the export over it.
        try:
            import claim_chart
            claim_chart.verify_matrix(chart, report)
        except Exception:
            pass
    if not chart.get("verification"):
        try:
            import claim_chart
            chart["verification"] = claim_chart._verify_stats(chart)
        except Exception:
            pass
    return view


def _load_cached_view(slug):
    p = REPORTS / f"{slug}.view.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _full_text(cur, pid, card):
    """Claims + description paragraphs IN FULL for one reference.

    The web card deliberately caps description at 60 paragraphs and hides most of it behind a tab,
    and the PDF/DOCX quote only the single best-matching passage. The Markdown export exists
    precisely to carry what those drop, so read the sections straight from Postgres (uncapped by
    the view's page-weight budget) and fall back to whatever the card carries for a federated /
    Mongo-only reference that has no local row.
    """
    claims, paras, figcaps = [], [], []
    if pid is not None:
        s = webview.sections(cur, pid)
        claims = s.get("claims") or []
        paras = s.get("paragraphs") or []
        figcaps = s.get("figures") or []
    if not claims:
        claims = card.get("claims") or []
    if not paras:
        paras = card.get("description") or []
    if not figcaps:
        figcaps = card.get("figure_caps") or []
    return {
        "claims": [{"claim_no": c.get("claim_no"), "independent": c.get("independent"),
                    "text": c.get("resolved_text") or c.get("text") or ""} for c in claims],
        "description": [{"para_no": p.get("para_no"), "heading": p.get("heading"),
                         "text": p.get("text") or ""} for p in paras],
        "figure_caps": [{"figure_no": f.get("figure_no"), "caption": f.get("caption") or ""}
                        for f in figcaps],
    }


def assemble(slug, selected_pubs, top_n=25, include_text=False, include_drawings=True):
    """Build the export model.

    `include_text`      — attach every reference's full claims + description (Markdown export).
    `include_drawings`  — resolve a local figure file per reference (PDF/DOCX/XLSX embed it; the
                          Markdown export is deliberately text-only, and skipping the resolve also
                          skips any CDN fetch, which is what makes it fast).
    """
    report = _load_report(slug)
    if not report:
        raise ValueError(f"no cached report for {slug}")
    # Export the SAME unified, listwise-ordered list the web page shows. That order (and the
    # folded-in federated-only cards) lives only in the cached view — build_view alone is fusion
    # order and local-only. Prefer the cache; fall back to a fresh build if none exists yet.
    cached = _load_cached_view(slug)
    if cached and cached.get("cards"):
        view = cached
    else:
        view = webview.build_view(report, top_n=max(top_n, len(selected_pubs) + 5))
    _attach_verification(slug, view, report)
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
        draw_path, draw_cap = _best_drawing(pub, card) if include_drawings else (None, None)
        rat = _rationale(slug, pub) or {}
        # legal status: prefer enrichment events
        legal = None
        ev = (disp or {}).get("legal_events") or b.get("legal_events") or []
        if ev:
            legal = "; ".join(f"{e.get('code') or ''} {e.get('date') or ''}".strip()
                              for e in ev[:3] if e)
        # Federated-only references have no local DB row and SerpApi enrichment may not resolve an
        # out-of-corpus pub, so fall back to the card's own fields (title/abstract/assignee/date/
        # cpc came in on the federated hit). Without this a federated-only reference exported blank.
        card_cpc = [c.get("code") for c in (card.get("cpc") or []) if c.get("code")]
        refs.append({
            "rank": card.get("rank"),
            "pub": pub,
            "title": b.get("title") or (disp or {}).get("title") or card.get("title"),
            "flag": b.get("flag") or card.get("flag") or "",
            "country": b.get("country") or (disp or {}).get("country") or card.get("country"),
            "assignees": b.get("assignees") or (disp or {}).get("assignees") or card.get("assignees") or [],
            "inventors": (b.get("inventors") or (disp or {}).get("inventors") or card.get("inventors") or [])[:5],
            "priority_date": b.get("priority_date") or (disp or {}).get("priority_date") or card.get("priority_date"),
            "filing_date": b.get("filing_date") or (disp or {}).get("filing_date") or card.get("filing_date"),
            "publication_date": b.get("publication_date") or (disp or {}).get("publication_date") or card.get("publication_date"),
            "family_id": b.get("family_id") or (disp or {}).get("family_id") or card.get("family_id"),
            "cpc": ([c["code"] for c in (b.get("cpc") or [])][:8] or
                    [c["code"] for c in ((disp or {}).get("classifications") or [])][:8] or
                    card_cpc[:8]),
            "legal_status": legal,
            "basis": card.get("basis", "n/a"),
            "channels": card.get("channels", []),
            "match_score": card.get("match_score"),
            "covers_elements": card.get("covers_elements", []),
            "abstract": b.get("abstract") or (disp or {}).get("abstract") or card.get("abstract"),
            "drawing_path": draw_path, "drawing_caption": draw_cap,
            "quoted": quoted,
            "why": rat.get("why", ""), "reads_on": rat.get("reads_on", []),
            # zero-padded office links (pubnorm) so exported US pre-grant links resolve.
            "google_patents": pubnorm.google_url(pub) or (disp or {}).get("google_patents") or card.get("google_patents"),
            "espacenet": pubnorm.espacenet_url(pub, b.get("family_id")) or (disp or {}).get("espacenet") or card.get("espacenet"),

            #  ---- what the RESULT CARD shows, carried into every export ----------------------
            #  These are read off the screen by whoever asked for the export, so an export that
            #  omits them is not the same document. They were previously web-only: the relevancy
            #  number and its written opinion, the legal-status tag, the family strip, the flag
            #  emoji, the "found via" chips and the figure count.
            "relevancy": (card.get("relevancy_score") if card.get("relevancy_score") is not None
                          else card.get("relevancy")),
            "relevancy_source": card.get("relevancy_source"),
            "relevancy_opinion": card.get("relevancy_opinion") or "",
            "status_label": (card.get("status") or {}).get("label"),
            "status_note": (card.get("status") or {}).get("note"),
            "family_summary": _family_line(card),
            "family_n": card.get("family_n"),
            "family_juris": card.get("family_juris"),
            "family_timeline": _family_flat(card),
            "family_members": [m.get("pub") for m in (card.get("family_members") or []) if m.get("pub")],
            "found_via": _prov_labels(card),
            "n_images": card.get("n_images") or 0,
            #  The card's PDF affordance, as the office-hosted URL rather than this app's own
            #  /pdf/<pub> route — an exported file is read somewhere the app is not reachable.
            "pdf_url": (disp or {}).get("pdf_url"),
            "drawings_provenance": card.get("drawings_provenance"),
            "match_coord": card.get("match_coord"),
            "match_kind": card.get("match_kind"),
            #  Full claims + description. Only for the Markdown export; the PDF/DOCX would grow by
            #  hundreds of pages and they already quote the matched passage.
            "text": _full_text(cur, pid, card) if include_text else None,
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
        "corpus_note": corpus_note(),
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

        #  ---- the header + coverage-ledger facts the report page shows -----------------------
        #  The three collapsed panels on the page (element × reference grid, coverage ledger,
        #  search scope) are the tool's own account of how much of the field it actually saw. The
        #  grid already travelled; these carry the other two, so a spreadsheet or a Markdown file
        #  is not a more confident document than the screen it came from.
        "n_cards": len(view.get("cards") or []),
        "n_families_surfaced": report.get("n_families"),
        "rounds": report.get("rounds"),
        "cpc_branches": report.get("cpc_branches", []),
        "round_new_families": report.get("round_new_families", []),
        "source_tags": view.get("source_tags", []),
        "domain": view.get("domain") or report.get("domain"),
        "subject_google": view.get("subject_google"),
    }
