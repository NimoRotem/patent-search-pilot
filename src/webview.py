"""Build the results-page view model from a CoverageAgent report + Postgres + enrichment cache.

Postgres provides ranking, the matched claim/paragraph coordinate (for highlighting) and the
structured sections; the SerpApi cache (enrich_display) provides drawings, PDF and rich biblio.
The agent report provides elements, element evidence, the claim chart and the coverage ledger.
"""
from __future__ import annotations
import json, re
from datetime import date, datetime
import db, embed
from search_modes import Subject, Mode, classify_basis, Basis
from config import DATA

FLAG = {"US": "🇺🇸", "EP": "🇪🇺", "WO": "🌐", "DE": "🇩🇪", "GB": "🇬🇧", "FR": "🇫🇷",
        "JP": "🇯🇵", "CN": "🇨🇳", "KR": "🇰🇷"}


def _d(s):
    if not s:
        return None
    if isinstance(s, (date, datetime)):
        return s if isinstance(s, date) else s.date()
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _vec(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ---- family -> representative publication --------------------------------------------------
def resolve_family_reps(cur, family_keys):
    """Map each family_key to its best representative publication row. Prefer a member with
    full-text claims, then US, then most-recent. One query for all keys."""
    if not family_keys:
        return {}
    cur.execute(
        """
        WITH cand AS (
          SELECT p.*,
                 COALESCE(NULLIF(p.simple_family_id,''), p.publication_number) AS fam,
                 (SELECT count(*) FROM claims c WHERE c.publication_id=p.id) AS n_claims,
                 (SELECT count(*) FROM chunks ch WHERE ch.publication_id=p.id AND ch.embedding IS NOT NULL) AS n_emb
          FROM publications p
          WHERE COALESCE(NULLIF(p.simple_family_id,''), p.publication_number) = ANY(%s)
        )
        SELECT DISTINCT ON (fam) fam, id, publication_number, kind_code, country,
               publication_date, filing_date, earliest_priority_date, title, abstract,
               simple_family_id, tier, facsimile_path, n_claims, n_emb
        FROM cand
        ORDER BY fam,
                 (n_claims > 0) DESC,
                 (country='US') DESC,
                 n_emb DESC,
                 publication_date DESC NULLS LAST
        """,
        (list(family_keys),),
    )
    return {r["fam"]: r for r in cur.fetchall()}


def biblio(cur, pid):
    cur.execute("""SELECT p.*, COALESCE(NULLIF(p.simple_family_id,''),p.publication_number) fam
                   FROM publications p WHERE p.id=%s""", (pid,))
    p = cur.fetchone()
    if not p:
        return None
    cur.execute("SELECT role, raw_name FROM parties WHERE publication_id=%s", (pid,))
    parties = cur.fetchall()
    assignees = [r["raw_name"] for r in parties if r["role"] == "assignee" and r["raw_name"]]
    inventors = [r["raw_name"] for r in parties if r["role"] == "inventor" and r["raw_name"]]
    cur.execute("SELECT DISTINCT symbol, is_first FROM classifications WHERE publication_id=%s "
                "AND scheme='CPC' ORDER BY is_first DESC, symbol LIMIT 12", (pid,))
    cpc = [{"code": r["symbol"], "first": r["is_first"]} for r in cur.fetchall()]
    cur.execute("SELECT event_code, event_date FROM legal_events WHERE publication_id=%s "
                "ORDER BY event_date DESC NULLS LAST LIMIT 8", (pid,))
    events = [{"code": r["event_code"], "date": str(r["event_date"]) if r["event_date"] else None}
              for r in cur.fetchall()]
    return {
        "pid": pid, "pub": p["publication_number"], "kind": p["kind_code"],
        "title": p["title"], "abstract": p["abstract"], "country": p["country"],
        "flag": FLAG.get(p["country"], "🏳️"),
        "publication_date": str(p["publication_date"]) if p["publication_date"] else None,
        "filing_date": str(p["filing_date"]) if p["filing_date"] else None,
        "priority_date": str(p["earliest_priority_date"]) if p["earliest_priority_date"] else None,
        "family_id": p["fam"], "tier": p["tier"], "facsimile_path": p["facsimile_path"],
        "assignees": assignees, "inventors": inventors[:8], "cpc": cpc, "legal_events": events,
    }


def match_in_pub(cur, pid, qvec):
    """Nearest chunk in a publication to the query -> (kind, coord, cosine score, text)."""
    cur.execute(
        "SELECT kind, coord, 1-(embedding <=> %s::vector) AS score, text "
        "FROM chunks WHERE publication_id=%s AND embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::vector LIMIT 1",
        (_vec(qvec), pid, _vec(qvec)),
    )
    r = cur.fetchone()
    if not r:
        return {"kind": None, "coord": None, "score": 0.0, "text": None}
    coord = r["coord"] if isinstance(r["coord"], dict) else (json.loads(r["coord"]) if r["coord"] else None)
    return {"kind": r["kind"], "coord": coord, "score": float(r["score"]), "text": r["text"]}


def sections(cur, pid):
    """Structured sections from Postgres: claims, paragraphs, figure captions, citations."""
    cur.execute("SELECT claim_no, is_independent, text, resolved_text FROM claims "
                "WHERE publication_id=%s ORDER BY claim_no", (pid,))
    claims = [{"claim_no": r["claim_no"], "independent": r["is_independent"],
               "text": r["text"], "resolved_text": r["resolved_text"]} for r in cur.fetchall()]
    cur.execute("SELECT para_no, heading, page_no, text FROM paragraphs "
                "WHERE publication_id=%s ORDER BY id LIMIT 400", (pid,))
    paras = [{"para_no": r["para_no"], "heading": r["heading"], "page_no": r["page_no"],
              "text": r["text"]} for r in cur.fetchall()]
    cur.execute("SELECT figure_no, caption, reference_numbers FROM figures "
                "WHERE publication_id=%s ORDER BY id LIMIT 80", (pid,))
    figs = [{"figure_no": r["figure_no"], "caption": r["caption"],
             "reference_numbers": r["reference_numbers"]} for r in cur.fetchall()]
    cur.execute("SELECT dst_pub, category, origin FROM citations WHERE src_pub="
                "(SELECT publication_number FROM publications WHERE id=%s) LIMIT 60", (pid,))
    cites = [{"pub": r["dst_pub"], "category": r["category"], "origin": r["origin"]}
             for r in cur.fetchall()]
    return {"claims": claims, "paragraphs": paras, "figures": figs, "citations": cites}


# ---- claim chart ---------------------------------------------------------------------------
def build_claim_chart(report, max_cols=8):
    """Rows = elements, columns = the references with the strongest cross-element evidence
    (combination-view refs first). Each cell = best evidence of that ref for that element."""
    elements = report["elements"]
    ev = report.get("element_evidence", {})
    # aggregate ref strength
    ref_agg = {}   # pub -> {"score": sum, "elements": set, "family": fam}
    for el, hits in ev.items():
        for h in hits:
            pub = h.get("pub")
            if not pub:
                continue
            a = ref_agg.setdefault(pub, {"score": 0.0, "elements": set(), "family": h.get("family")})
            a["score"] += float(h.get("score") or 0)
            a["elements"].add(el)
    # prioritize combination view refs
    cv = report.get("combination_view", {})
    priority = []
    if cv.get("primary"):
        priority.append(cv["primary"])
    for s in cv.get("secondaries", []):
        if s.get("ref"):
            priority.append(s["ref"])
    ordered = [p for p in priority if p in ref_agg]
    for pub, _ in sorted(ref_agg.items(), key=lambda kv: (len(kv[1]["elements"]), kv[1]["score"]),
                         reverse=True):
        if pub not in ordered:
            ordered.append(pub)
    cols = ordered[:max_cols]
    # cell lookup
    cell = {}
    for el, hits in ev.items():
        best = {}
        for h in hits:
            pub = h.get("pub")
            if pub in cols:
                if pub not in best or float(h.get("score") or 0) > best[pub]["score"]:
                    best[pub] = {"score": float(h.get("score") or 0), "coord": h.get("coord"),
                                 "basis": h.get("basis"), "kind": h.get("kind"),
                                 "channels": h.get("channels", [])}
        cell[el] = best
    # score range for coloring
    scores = [c["score"] for row in cell.values() for c in row.values()]
    smax = max(scores) if scores else 1.0
    smin = min(scores) if scores else 0.0
    rows = []
    for el in elements:
        cells = []
        for pub in cols:
            c = cell.get(el, {}).get(pub)
            if c:
                intensity = (c["score"] - smin) / (smax - smin) if smax > smin else 0.5
                coord_str = _coord_str(c["coord"])
                # M9 §3: a cell backed only by a whole-doc match (no specific claim/para/figure)
                # cannot show WHERE the element is disclosed — mark it "weak" so the chart doesn't
                # imply verified coverage. (Coord-backed cells still carry a residual false-positive
                # rate that neither the fused score nor the element↔chunk cosine separates cleanly —
                # see data/reports/RELEVANCE_AUDIT.md; the reliable fix is per-cell LLM verification.)
                strength = "cited" if coord_str else "weak"
                cells.append({"pub": pub, "score": round(c["score"], 3), "coord": coord_str,
                              "basis": c["basis"], "intensity": round(intensity, 3),
                              "covered": True, "strength": strength})
            else:
                cells.append({"pub": pub, "covered": False})
        rows.append({"element": el, "cells": cells,
                     "coverage": report.get("element_coverage", {}).get(el, {})})
    columns = [{"pub": pub, "n_elements": len(ref_agg.get(pub, {}).get("elements", []))} for pub in cols]
    return {"columns": columns, "rows": rows, "combination_view": cv}


def _coord_str(coord):
    if not coord:
        return ""
    if isinstance(coord, str):
        try:
            coord = json.loads(coord)
        except Exception:
            return coord
    for k, lbl in (("claim_no", "cl"), ("para_no", "¶"), ("figure_no", "fig")):
        if coord.get(k) is not None:
            return f"{lbl} {coord[k]}"
    return ""


# ---- substance filter (M9 relevance audit) -------------------------------------------------
# Title-only publications (their ONLY embedded chunk is the 'whole'/title, e.g. a 1904 "Vacuum
# lifting device" with no abstract/claims text) match a query on the bare title and used to flood
# the top-10 with substance-less hits. Design patents (kind 'S…' / -D… numbers) carry no technical
# disclosure and are never technical prior art. This filter runs at the DISPLAY layer ONLY — it
# re-orders / trims what the page SHOWS. Retrieval, RRF fusion, and the gold-eval recall metric all
# read report["ranked_families"], which is left untouched, so recall cannot regress. (An earlier
# attempt demoted inside the retrieval channels and regressed agentic recall@100 0.185 -> 0.138;
# moving it here fixes precision@10 with zero recall cost.)
_DESIGN_NUM_RE = re.compile(r"-D[0-9]")


def _is_design(pub_number, kind_code):
    return bool((kind_code or "").upper().startswith("S") or _DESIGN_NUM_RE.search(pub_number or ""))


def _titleonly_ids(cur, ids):
    """Of the given publication ids, which have ONLY a 'whole' (title) embedded chunk."""
    if not ids:
        return set()
    cur.execute("SELECT publication_id FROM ("
                "  SELECT publication_id, string_agg(DISTINCT kind, ',') kinds FROM chunks "
                "  WHERE embedding IS NOT NULL AND publication_id = ANY(%s) GROUP BY publication_id) t "
                "WHERE kinds = 'whole'", (list(ids),))
    return {r["publication_id"] for r in cur.fetchall()}


def substance_order(cur, families, reps, keep):
    """Drop design-patent families and demote title-only families below substantive ones, then
    trim to `keep`. Stable within each group (preserves the retrieval ranking). Returns
    (ordered_families, stats)."""
    ids = [reps[f]["id"] for f in families if f in reps]
    titleonly = _titleonly_ids(cur, ids)
    kept, demoted, dropped = [], [], 0
    for f in families:
        r = reps.get(f)
        if not r:
            continue
        if _is_design(r["publication_number"], r["kind_code"]):
            dropped += 1
            continue
        (demoted if r["id"] in titleonly else kept).append(f)
    ordered = kept + demoted          # substantive first (in-rank), title-only after
    return ordered[:keep], {"design_dropped": dropped, "titleonly_demoted": len(demoted),
                            "titleonly_ids": titleonly}


# ---- full view -----------------------------------------------------------------------------
def build_view(report, top_n=25):
    """Assemble the whole page view model. Enrichment (drawings/pdf/sections/rationale) is
    filled lazily by the webapp per card; here we do the DB-backed ranking + matched coord."""
    query = report.get("query", "")
    subj = None
    s = report.get("subject")
    # subject may be a pub number string (from report) — build a light Subject if we can
    subject_obj = None
    conn = db.connect(); conn.autocommit = True
    cur = conn.cursor()
    if s:
        cur.execute("SELECT publication_number, earliest_priority_date, filing_date, "
                    "publication_date, country FROM publications WHERE publication_number=%s LIMIT 1", (s,))
        r = cur.fetchone()
        if r:
            subject_obj = Subject(number=r["publication_number"],
                                  efd=r["earliest_priority_date"] or r["filing_date"] or r["publication_date"],
                                  filing_date=r["filing_date"], publication_date=r["publication_date"],
                                  jurisdiction=r["country"])

    qvec = embed.embed_query(query[:8000], 768) if query else None

    # Pull a wider window than we show, then apply the display-layer substance filter (drop design,
    # demote title-only) and trim to top_n — so a demoted title-only hit is replaced by the next
    # substantive family rather than leaving a hole. report["ranked_families"] itself is untouched.
    window = report.get("ranked_families", [])[:max(top_n * 3, 60)]
    reps = resolve_family_reps(cur, window)
    ranked, subs_stats = substance_order(cur, window, reps, top_n)

    # which elements each family covers (from evidence)
    fam_elements = {}
    ev = report.get("element_evidence", {})
    for el, hits in ev.items():
        for h in hits:
            fam = h.get("family")
            if fam:
                fam_elements.setdefault(fam, {}).setdefault(el, float(h.get("score") or 0))
    # channels per family
    fam_channels = {}
    for ch, fams in report.get("channel_families", {}).items():
        for f in fams:
            fam_channels.setdefault(f, set()).add(ch)

    cards = []
    for rank, fam in enumerate(ranked, 1):
        rep = reps.get(fam)
        if not rep:
            continue
        b = biblio(cur, rep["id"])
        if not b:
            continue
        m = match_in_pub(cur, rep["id"], qvec) if qvec is not None else {"score": 0, "coord": None, "kind": None}
        basis = "n/a"
        if subject_obj:
            bb = classify_basis({"publication_date": _d(b["publication_date"]),
                                 "earliest_priority_date": _d(b["priority_date"]),
                                 "filing_date": _d(b["filing_date"])}, subject_obj)
            basis = bb.value
        covered = sorted(fam_elements.get(fam, {}).keys())
        cards.append({
            "rank": rank, "family": fam, **b,
            "match_score": round(m["score"], 3), "match_coord": _coord_str(m["coord"]),
            "match_kind": m["kind"], "basis": basis,
            "channels": sorted(fam_channels.get(fam, [])),
            "covers_elements": covered, "n_covers": len(covered),
            "has_local_claims": rep["n_claims"] > 0,
        })

    chart = build_claim_chart(report)
    cur.close(); conn.close()
    return {
        "query": query, "mode": report.get("mode"), "subject": s,
        "subject_flag": FLAG.get(subject_obj.jurisdiction, "") if subject_obj else "",
        "rounds": report.get("rounds"), "n_families": report.get("n_families"),
        "channels_used": report.get("channels_used", []),
        "languages": report.get("languages", []),
        "llm_usage": report.get("llm_usage", {}),
        "elements": report["elements"],
        "element_coverage": report.get("element_coverage", {}),
        "claim_chart": chart,
        "cards": cards,
        "substance_filter": {k: v for k, v in subs_stats.items() if k != "titleonly_ids"},
        "coverage_ledger": {
            "cpc_branches": report.get("cpc_branches", []),
            "round_new_families": report.get("round_new_families", []),
            "combination_view": report.get("combination_view", {}),
        },
    }
