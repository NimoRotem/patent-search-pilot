"""Build the results-page view model from a CoverageAgent report + Postgres + enrichment cache.

Postgres provides ranking, the matched claim/paragraph coordinate (for highlighting) and the
structured sections; the SerpApi cache (enrich_display) provides drawings, PDF and rich biblio.
The agent report provides elements, element evidence, the claim chart and the coverage ledger.
"""
from __future__ import annotations
import json, re
from datetime import date, datetime
import db, embed, status as status_mod
import enrich_display                       # office links + cached drawing provenance (local only)
from search_modes import Subject, Mode, classify_basis, Basis
from config import DATA

FLAG = {"US": "🇺🇸", "EP": "🇪🇺", "WO": "🌐", "DE": "🇩🇪", "GB": "🇬🇧", "FR": "🇫🇷",
        "JP": "🇯🇵", "CN": "🇨🇳", "KR": "🇰🇷"}


# ---- federated source tags -----------------------------------------------------------------
# The results header shows one tag per search source with its state for THIS run. It is built
# data-driven, so a source activated upstream (Lens, say) appears here with no template change.
#
# Priority order:
#   1. a per-source status structure on the federation payload, if the engine supplies one;
#   2. otherwise: hit counts derived from hits[].sources, crossed with the engine's advertised
#      source catalogue so "searched and returned nothing" is distinguishable from "not wired up".
_SRC_LABEL = {
    "local": "Local corpus",
    "serpapi_gpatents": "SerpApi",
    "bigquery_gpatents": "BigQuery",
    "gpatents_scrape": "GP scrape",
    "pqai": "PQAI",
    "epo_ops": "EPO OPS",
    "uspto": "USPTO",
    "openalex": "OpenAlex",
    "lens": "Lens",
}


def _src_label(sid):
    return _SRC_LABEL.get(sid) or str(sid).replace("_", " ").title()


# The engine's /api/health source catalogue, cached. Refreshed on a background thread so that
# rendering a report NEVER blocks on a network call; until the first refresh lands the tag row
# simply falls back to what the report itself records.
_ENGINE_SRC = {"t": 0.0, "v": []}
_ENGINE_TTL = 900.0


def _engine_sources():
    import os, threading, time
    if "PYTEST_CURRENT_TEST" in os.environ:
        return []                       # see module note: no stray network thread under test
    now = time.time()
    if _ENGINE_SRC["t"] and (now - _ENGINE_SRC["t"]) < _ENGINE_TTL:
        return _ENGINE_SRC["v"]
    _ENGINE_SRC["t"] = now              # claim the slot before starting, so N workers start one

    def _go():
        try:
            import federation, requests
            r = requests.get(federation.BASE_URL + "/api/health",
                             headers=federation._headers(), timeout=6)
            if r.ok:
                d = r.json()
                if isinstance(d.get("sources"), list):
                    _ENGINE_SRC["v"] = [x for x in d["sources"] if isinstance(x, dict)]
        except Exception:
            pass                        # a health probe must never affect a page render

    threading.Thread(target=_go, daemon=True).start()
    return _ENGINE_SRC["v"]


def _source_tags(report, n_local):
    """-> [{id,label,state,n,note}] where state is used | none | failed | off."""
    tags = [{"id": "local", "label": "Local corpus",
             "state": "used" if n_local else "none", "n": n_local, "note": "", "why": ""}]
    fed = report.get("federation")
    if not fed:
        return tags

    # 1. Engine-supplied per-source status wins outright.
    status = fed.get("source_status") or fed.get("by_source")
    if isinstance(status, dict):
        status = [{"name": k, **(v if isinstance(v, dict) else {"n": v})}
                  for k, v in status.items()]
    if isinstance(status, list) and status and isinstance(status[0], dict):
        for x in status:
            sid = x.get("id") or x.get("name")
            if not sid:
                continue
            n = int(x.get("n") or x.get("n_hits") or 0)
            st = x.get("state")
            if not st:
                if not x.get("enabled", True):
                    st = "off"
                elif x.get("error") or x.get("failed"):
                    st = "failed"
                else:
                    st = "used" if n else "none"
            tags.append({"id": sid, "label": x.get("label") or _src_label(sid),
                         "state": st, "n": n,
                         "note": str(x.get("note") or "")[:160],
                         "why": str(x.get("reason") or x.get("error") or "")[:160]})
        return tags

    # 2. Derive from the hits actually recorded, crossed with the advertised catalogue.
    counts = {}
    for h in fed.get("hits") or []:
        for sid in (h.get("sources") or []):
            counts[sid] = counts.get(sid, 0) + 1

    known = _engine_sources()
    fed_ok = bool(fed.get("ok"))
    seen = set()
    for x in known:
        sid = x.get("name")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        n = counts.get(sid, 0)
        if not x.get("enabled", True):
            st = "off"
        elif not fed_ok:
            # The federated call failed as a whole, so no per-source outcome was ever observed.
            # These sources are configured and healthy as far as the engine knows; saying they
            # each "failed" would be asserting N failures we did not measure.
            st = "unknown"
        elif not x.get("search_available", True):
            st = "failed"
        else:
            st = "used" if n else "none"
        tags.append({"id": sid, "label": _src_label(sid), "state": st, "n": n,
                     "note": str(x.get("note") or "")[:160],
                     "why": str(x.get("reason") or "")[:160]})

    for sid, n in sorted(counts.items()):
        if sid not in seen:
            tags.append({"id": sid, "label": _src_label(sid), "state": "used",
                         "n": n, "note": "", "why": ""})

    # The one failure we actually observed.
    if not fed_ok:
        tags.append({"id": "federation", "label": "External APIs", "state": "failed",
                     "n": 0, "note": "", "why": str(fed.get("error") or "")[:160]})
    return tags


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


def _relevancy(cosine):
    """Map the best-matching passage's cosine similarity to a 0-100 relevancy score for display
    (like a search-engine relevancy). Calibrated so strong semantic matches land in the 80-95 band
    and weak ones in the 40s — the raw cosine is kept on the card too (match_score)."""
    try:
        c = float(cosine or 0)
    except (TypeError, ValueError):
        c = 0.0
    pct = (c - 0.35) / (0.90 - 0.35) * 100.0        # 0.90 cos -> ~100, 0.35 -> 0
    return int(max(1, min(99, round(pct))))


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


def _thin_ids(cur, ids):
    """Publications with NO ingested claims AND NO abstract — a card built from one shows almost
    nothing (the old OCR'd patents the user complained about). Demoted below refs that have real
    text so the top of the list always has content to read."""
    if not ids:
        return set()
    cur.execute("SELECT p.id FROM publications p WHERE p.id = ANY(%s) "
                "AND (p.abstract IS NULL OR p.abstract='') "
                "AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.publication_id=p.id)", (list(ids),))
    return {r["id"] for r in cur.fetchall()}


def substance_order(cur, families, reps, keep):
    """Drop design-patent families and demote title-only families below substantive ones, then
    trim to `keep`. Stable within each group (preserves the retrieval ranking). Returns
    (ordered_families, stats)."""
    ids = [reps[f]["id"] for f in families if f in reps]
    titleonly = _titleonly_ids(cur, ids)
    thin = _thin_ids(cur, ids) | titleonly       # no-text refs also sink below substantive ones
    kept, demoted, dropped = [], [], 0
    for f in families:
        r = reps.get(f)
        if not r:
            continue
        if _is_design(r["publication_number"], r["kind_code"]):
            dropped += 1
            continue
        (demoted if r["id"] in thin else kept).append(f)
    ordered = kept + demoted          # refs with real text first (in-rank), thin/title-only after
    return ordered[:keep], {"design_dropped": dropped, "titleonly_demoted": len(demoted),
                            "thin_demoted": len(demoted), "titleonly_ids": titleonly}


def _attach_family_members(cur, cards):
    """Group the OTHER filings of the same patent family (other countries / kinds) under each card,
    so a result can be expanded to see where else the invention was filed. One batched query keyed
    on the reps' simple_family_id; the representative shown as the card is excluded from its list."""
    for c in cards:
        c["family_members"] = []
        c["n_family"] = 0
    reps_by_sfid = {}
    for c in cards:
        if c.get("sfid"):
            reps_by_sfid.setdefault(c["sfid"], c["pub"])
    if not reps_by_sfid:
        return
    cur.execute(
        "SELECT publication_number, country, kind_code, publication_date, filing_date, "
        "earliest_priority_date, simple_family_id FROM publications "
        "WHERE simple_family_id = ANY(%s) "
        "ORDER BY (country='US') DESC, publication_date DESC NULLS LAST",
        (list(reps_by_sfid.keys()),))
    members = {}
    for r in cur.fetchall():
        members.setdefault(r["simple_family_id"], []).append(r)
    for c in cards:
        sfid = c.get("sfid")
        if not sfid:
            continue
        seen, out = set(), []
        for r in members.get(sfid, []):
            pn = r["publication_number"]
            if pn == c["pub"] or pn in seen:
                continue
            seen.add(pn)
            st = status_mod.classify_status(r["kind_code"], r["country"], r["earliest_priority_date"],
                                            r["filing_date"], r["publication_date"])
            out.append({"pub": pn, "country": r["country"], "flag": FLAG.get(r["country"], "🏳️"),
                        "kind": r["kind_code"],
                        "date": str(r["publication_date"]) if r["publication_date"] else None,
                        "status": st})
            if len(out) >= 24:
                break
        c["family_members"] = out
        c["n_family"] = len(out)


def _cached_images(pub):
    """Figure image files already downloaded for this pub (served at /figures/<pub>/<file>)."""
    try:
        from enrich_display import FIGDIR, _pubkey
        d = FIGDIR / _pubkey(pub)
    except Exception:
        return []
    if not d.exists():
        return []
    files = sorted([f.name for f in d.iterdir()
                    if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff")])
    from_pdf = any(f.startswith("pdf") for f in files)
    return [{"file": f, "from_pdf": from_pdf} for f in files]


def _card_content(cur, pid, pub, matched, family_id=None):
    """Everything needed to render the card's tabs WITHOUT a round-trip: claims / description /
    figure captions straight from Postgres (already ingested for most refs) + any figure images
    already downloaded. This is what makes the data show immediately instead of 'not ingested'."""
    s = sections(cur, pid)
    mc = matched.get("coord") if isinstance(matched.get("coord"), dict) else None
    imgs = _cached_images(pub)
    #  Office links + drawing provenance, from the ALREADY-CACHED display record only.
    #  load_cached() never hits the network, so adding these costs nothing per card; a reference
    #  that has not been enriched yet simply has no Espacenet link until it is, rather than making
    #  the results page wait on an API call.
    google_patents = provenance = None
    #  BUILD the Espacenet link rather than trusting the cached one. Display records cached before
    #  the URL scheme was corrected still hold the old bare full-text form
    #  (/patent/search?q=<number>), which can land on a result list or on the wrong document --
    #  exactly the bug espacenet_url() was written to fix. Constructing it here is pure string
    #  work, costs no request, and is family-scoped when the family is known.
    try:
        espacenet = enrich_display.espacenet_url(pub, family_id)
    except Exception:
        espacenet = None
    try:
        cached = enrich_display.load_cached(pub) or {}
        disp = cached.get("_display") or {}
        google_patents = disp.get("google_patents")
        provenance = disp.get("drawings_provenance")
        if not espacenet:
            espacenet = disp.get("espacenet")
    except Exception:
        pass
    return {
        "espacenet": espacenet,
        "google_patents": google_patents,
        "drawings_provenance": provenance,
        "claims": s["claims"],
        "description": s["paragraphs"][:60],          # cap for page weight; PDF has the full text
        "figure_caps": s["figures"],
        "images": imgs,
        "n_images": len(imgs),
        "matched_coord_raw": mc,
        "has_content": bool(s["claims"] or s["paragraphs"] or imgs),
    }


# ---- full view -----------------------------------------------------------------------------
def build_view(report, top_n=25):
    """Assemble the whole page view model. Reference text (claims/description/figure captions) and
    any already-downloaded drawings are attached PER CARD from Postgres here, so the results render
    with real content immediately; only missing drawings are fetched lazily by the page."""
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
        st = status_mod.classify_status(b["kind"], b["country"], b["priority_date"],
                                        b["filing_date"], b["publication_date"])
        content = _card_content(cur, rep["id"], b["pub"], m, b.get("family_id"))
        cards.append({
            "rank": rank, "family": fam, **b,
            "match_score": round(m["score"], 3), "match_coord": _coord_str(m["coord"]),
            "match_kind": m["kind"], "basis": basis,
            "relevancy": _relevancy(m["score"]),         # 0-100 best-passage semantic match
            "status": st,
            "sfid": rep.get("simple_family_id") or None,
            "channels": sorted(fam_channels.get(fam, [])),
            "covers_elements": covered, "n_covers": len(covered),
            "has_local_claims": rep["n_claims"] > 0,
            **content,                                    # claims/description/figures/images (from DB+cache)
        })

    _attach_family_members(cur, cards)
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
        "domain": report.get("domain"),
        "federation": report.get("federation"),
        "source_tags": _source_tags(report, len(cards)),
        "federation_offered": bool(report.get("federation_offered")),
        "coverage_ledger": {
            "cpc_branches": report.get("cpc_branches", []),
            "round_new_families": report.get("round_new_families", []),
            "combination_view": report.get("combination_view", {}),
        },
    }
