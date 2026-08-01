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
import ops_family                            # worldwide INPADOC family -> year/jurisdiction timeline
import pubnorm                               # single link-builder: zero-padded Google/Espacenet URLs
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
    "claim_dense": "Claim semantic",
    "claim_bm25": "Claim keyword",
}


def _src_label(sid):
    return _SRC_LABEL.get(sid) or str(sid).replace("_", " ").title()


# The local pgvector retrieval channels (our own corpus). The two NEW parallel channels —
# 'docchunks' (multi-chunk semantic) and 'image' — are rendered as their own labelled chips, and
# every other channel_families key (federated API ids) flows through the per-result API provenance.
_LOCAL_CHANNELS = {"dense", "bm25", "claim_dense", "claim_bm25", "exact", "cpc",
                   "citation", "qbe", "biblio", "crosslingual", "seed"}


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
    """-> source chips; `degraded` means useful hits plus at least one provider error."""
    tags = [{"id": "local", "label": "Local corpus",
             "state": "used" if n_local else "none", "n": n_local, "note": "", "why": ""}]

    # The two NEW parallel channels that run on a document/link search: multi-chunk semantic search
    # of the query document's own text, and image-similarity search of its drawings. Shown in the
    # same data-driven row so a document search visibly used them (or why it didn't).
    cf = report.get("channel_families") or {}
    if "docchunks" in cf:
        n = len(cf.get("docchunks") or [])
        tags.append({"id": "docchunks", "label": "Semantic chunk match",
                     "state": "used" if n else "none", "n": n,
                     "note": "the query document's own text, chunked and embedded like the corpus",
                     "why": ""})
    img = report.get("image_channel") or {}
    if img or "image" in cf:
        n = int(img.get("n") or len(cf.get("image") or []))
        st = img.get("state") or ("used" if n else "none")
        tags.append({"id": "image", "label": "Image match", "state": st, "n": n,
                     "note": str(img.get("note") or "query drawings vs the corpus figure index"),
                     "why": str(img.get("note") or "")})

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
            detail = x.get("state_detail")
            st = "degraded" if detail == "degraded" else x.get("state")
            if not st:
                if not x.get("enabled", True):
                    st = "off"
                elif x.get("error") or x.get("failed"):
                    st = "failed"
                else:
                    st = "used" if n else "none"
            raw_reason = x.get("reason") or x.get("error") or x.get("note") or ""
            try:
                from federation import _display_reason
                reason = _display_reason(detail or st, raw_reason)
            except Exception:
                reason = " ".join(str(raw_reason).split())[:160]
            tags.append({"id": sid, "label": x.get("label") or _src_label(sid),
                         "state": st, "n": n, "note": reason, "why": reason})
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


def _cosine(a, b):
    """Cosine similarity between two equal-length float vectors. 0.0 on any degenerate input —
    used only to give a federated-only card a display relevancy comparable to the corpus cards."""
    try:
        n = min(len(a), len(b))
        if not n:
            return 0.0
        dot = na = nb = 0.0
        for i in range(n):
            x = float(a[i]); y = float(b[i])
            dot += x * y; na += x * x; nb += y * y
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / ((na ** 0.5) * (nb ** 0.5))
    except Exception:
        return 0.0


_JK_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def _join_key(pub):
    """Publication number -> a normalised join key comparable across systems (mirrors
    federation.join_key: strip non-alphanumerics, upper-case). Kept local so build_view has no
    import dependency on the federation client."""
    if not pub:
        return ""
    pub = str(pub)
    if "patent/" in pub:
        pub = pub.split("patent/", 1)[1].split("/")[0]
    return _JK_NON_ALNUM.sub("", pub).upper()


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
        c["family_timeline"] = []
        c["family_source"] = None
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
        # Corpus-only timeline rows: prefer filing/priority year (what Google's Worldwide
        # applications strip reflects), the card's own row included, so the baseline strip
        # renders instantly with zero network cost. It is upgraded to the authoritative
        # worldwide INPADOC family (EPO OPS) by the prefetch / lazy /api/family path.
        tl_rows = [{"pub": c["pub"], "country": c.get("country"),
                    "date": c.get("filing_date") or c.get("priority_date") or c.get("publication_date")}]
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
            tl_rows.append({"pub": pn, "country": r["country"],
                            "date": r["filing_date"] or r["earliest_priority_date"] or r["publication_date"]})
            if len(out) >= 24:
                break
        c["family_members"] = out
        c["n_family"] = len(out)
        try:
            fam = ops_family.corpus_timeline(c["pub"], tl_rows)
            c["family_timeline"] = fam["timeline"]
            c["family_source"] = fam["source"]      # "corpus" (partial) until OPS upgrades it
            c["family_n"] = fam["n_members"]
            c["family_juris"] = fam["n_jurisdictions"]
        except Exception:
            pass


def ensure_family_timelines(cards):
    """Attach the corpus-only family timeline to cards that predate the feature.

    A `<slug>.view.json` cached before Feature 1 has no `family_timeline` field, and the cache is
    served without re-running build_view. This cheap one-query upgrade (no rerank, no LLM) fills
    the baseline strip on those cached reports so they render it on the next load. Returns True
    when it changed anything, so the caller can persist the upgraded cache."""
    cards = cards or []
    if not cards or all("family_timeline" in c for c in cards):
        return False
    conn = db.connect(); conn.autocommit = True
    cur = conn.cursor()
    try:
        _attach_family_members(cur, cards)
    finally:
        cur.close(); conn.close()
    return True


def _cached_images(pub):
    """Figure image files already downloaded for this pub (served at /figures/<pub>/<file>)."""
    try:
        from enrich_display import FIGDIR, _canonical_pubkey
        d = FIGDIR / _canonical_pubkey(pub)
    except Exception:
        return []
    if not d.exists():
        return []
    files = sorted([f.name for f in d.iterdir()
                    if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff")])
    from_pdf = any(f.startswith("pdf") for f in files)
    return [{"file": f, "from_pdf": from_pdf} for f in files]


def prune_missing_image_files(cards):
    """Remove stale local figure entries from a cached view.

    View JSON can outlive a recovered image on disk. Rendering that stale manifest causes a 404
    before the batch thumbnail endpoint gets a chance to recover a remote/cached alternative.
    Remote images (`file` is null) are left untouched. Returns whether any card changed.
    """
    changed = False
    for card in cards or []:
        images = card.get("images") or []
        if not images:
            continue
        try:
            pubdir = enrich_display.FIGDIR / enrich_display._canonical_pubkey(card.get("pub"))
        except (TypeError, ValueError):
            pubdir = None
        kept = []
        for image in images:
            filename = image.get("file") if isinstance(image, dict) else None
            if not filename:
                kept.append(image)
                continue
            # A cached manifest is never allowed to escape its validated publication directory.
            if pubdir is not None and isinstance(filename, str) and "/" not in filename \
                    and "\\" not in filename and (pubdir / filename).is_file():
                kept.append(image)
        if len(kept) != len(images):
            card["images"] = kept
            card["n_images"] = len(kept)
            if not kept:
                card["drawings_provenance"] = None
            changed = True
    return changed


# ---- eager lemad-Mongo enrichment of the DISPLAYED cards (iptorch-style) -------------------
# iptorch shows everything instantly because it reads a pre-built patent corpus rather than
# recovering figures/text live. We do the same for the final shown set: one mongo_corpus.get_detail
# per card returns figures (Google-CDN URLs, nothing downloaded) + full claims/description/CPC, and
# we fill ONLY the gaps the local corpus / a federated-only hit left. This is what puts a sketch
# and full content on every card at first paint, including the external PQAI winners that arrive
# carrying just a title + abstract. Never overwrites content the corpus already has.
def _mongo_claim_dicts(claims):
    return [{"claim_no": i + 1, "independent": None, "text": t, "resolved_text": None}
            for i, t in enumerate(claims or []) if t]


def _mongo_para_dicts(desc):
    return [{"para_no": None, "heading": None, "text": t} for t in (desc or []) if t]


def _mongo_images(figures):
    out = []
    for f in (figures or []):
        full = f.get("full")
        if not full:
            continue
        out.append({"file": None, "full": full, "thumbnail": f.get("thumbnail") or full,
                    "src_url": full, "from_mongo": True})
    return out


def mongo_enrich_cards(cards):
    """Fill each displayed card's figures + full text from the lemad Mongo corpus, in place.

    Bounded to the shown set (<=25). get_detail is cheap (bounded pool, short timeouts, on-disk
    cache, never raises) and returns remote-CDN figure URLs, so this adds no download and no OPS
    cost. Gaps only: a field the local corpus already populated is left untouched."""
    try:
        import mongo_corpus
    except Exception:
        return cards
    for c in (cards or []):
        pub = c.get("pub")
        if not pub:
            continue
        try:
            md = mongo_corpus.get_detail(pub)
        except Exception:
            md = None
        if not md:
            continue
        if not c.get("images"):
            imgs = _mongo_images(md.get("figures"))
            if imgs:
                c["images"] = imgs
                c["n_images"] = len(imgs)
                c["drawings_source"] = c.get("drawings_source") or "lemad_mongo"
                if not c.get("drawings_provenance"):
                    c["drawings_provenance"] = ("figures from the lemad patent corpus "
                                                "(Google-CDN facsimile)")
        if md.get("claims") and not c.get("claims"):
            c["claims"] = _mongo_claim_dicts(md["claims"])
        if md.get("description") and not c.get("description"):
            c["description"] = _mongo_para_dicts(md["description"])
        if md.get("classifications") and not c.get("cpc"):
            c["cpc"] = [{"code": x["code"], "first": bool(x.get("first"))}
                        for x in md["classifications"] if x.get("code")][:12]
        if md.get("abstract") and not c.get("abstract"):
            c["abstract"] = md["abstract"]
        if md.get("assignees") and not c.get("assignees"):
            c["assignees"] = [a for a in md["assignees"] if a]
        if md.get("inventors") and not c.get("inventors"):
            c["inventors"] = [i for i in md["inventors"] if i]
        if md.get("title") and not c.get("title"):
            c["title"] = md["title"]
        if md.get("publication_date") and not c.get("publication_date"):
            c["publication_date"] = md["publication_date"]
        if md.get("mongo_key"):
            c["mongo_key"] = md["mongo_key"]
    return cards


def fix_view_office_links(view) -> bool:
    """Rewrite a CACHED view's outbound office links through pubnorm, in place.

    A report cached before the dropped-zero fix baked the DEAD links into <slug>.view.json — a
    Google URL with the US pre-grant leading zero dropped (US2022153556 -> a MISSING page) and an
    un-padded Espacenet lookup. build_view now builds them correctly, but the view cache short-
    circuits build_view, so freshly-built links never reach an already-cached report. This is the
    cheap pure-string backfill that fixes them, mirroring the family-timeline / mongo-figure
    backfills in _build_view_cached. Returns True if anything changed."""
    changed = False
    for c in (view.get("cards") or []):
        pub = c.get("pub")
        if not pub:
            continue
        g = pubnorm.google_url(pub)
        if g and c.get("google_patents") != g:
            c["google_patents"] = g
            changed = True
        e = pubnorm.espacenet_url(pub, c.get("family_id"))
        if e and c.get("espacenet") != e:
            c["espacenet"] = e
            changed = True
    subj = view.get("subject")
    if subj:
        sg = pubnorm.google_url(subj)
        if sg and view.get("subject_google") != sg:
            view["subject_google"] = sg
            changed = True
    return changed


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
    provenance = None
    #  BUILD both office links here rather than trusting the cached ones. Display records cached
    #  before the padding fix hold DEAD forms: the old bare Espacenet full-text search, and a
    #  Google URL with the US pre-grant leading zero dropped (US2022153556 -> a MISSING page vs the
    #  live US20220153556A1). pubnorm is the single link-builder — it zero-pads and adds the kind
    #  code both offices 404 without. Pure string work, no request, family-scoped when known.
    try:
        google_patents = pubnorm.google_url(pub)
    except Exception:
        google_patents = None
    try:
        espacenet = pubnorm.espacenet_url(pub, family_id)
    except Exception:
        espacenet = None
    try:
        cached = enrich_display.load_cached(pub) or {}
        disp = cached.get("_display") or {}
        provenance = disp.get("drawings_provenance")
        if not google_patents:
            google_patents = disp.get("google_patents")
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


# ---- federated hits -> unified list --------------------------------------------------------
# ONE ranked list, whatever the source. A federated hit whose publication is already in the local
# corpus is the SAME reference we already carded from Postgres: we do not draw a second row — we
# record its external API source(s) on that family's card (the per-result source chips already
# support several). A federated hit with NO local row still belongs in the list, so it is built
# into a full card from the hit's own fields and ranked alongside everything else by the listwise
# reranker downstream. Provenance stays visible per card, not in a separate block.
def _resolve_fed_pubs(cur, join_keys):
    """{normalised_pub -> (publication_id, family_key)} for those present in the local corpus."""
    keys = [k for k in dict.fromkeys(join_keys) if k]
    if not keys:
        return {}
    cur.execute(
        "SELECT id, upper(regexp_replace(publication_number, '[^A-Za-z0-9]', '', 'g')) AS k, "
        "COALESCE(NULLIF(simple_family_id,''), publication_number) AS fam "
        "FROM publications "
        "WHERE upper(regexp_replace(publication_number, '[^A-Za-z0-9]', '', 'g')) = ANY(%s)",
        (keys,))
    return {r["k"]: (r["id"], r["fam"]) for r in cur.fetchall()}


def _add_api_prov(card, api):
    """Add an external-API source chip to a card, de-duplicated by label (a hit found by several
    APIs, or a family already tagged from report['family_sources'], must not stack duplicates)."""
    lbl = _src_label(api)
    for p in card.get("prov", []):
        if p.get("label") == lbl:
            return
    card.setdefault("prov", []).append({"label": lbl, "cls": "prov-api"})


def _fed_card(h, qvec):
    """Build a full result card from a federated-only hit (no local Postgres row). Every field is
    guarded so a hit missing a title/date/assignee degrades to a blank rather than raising. The
    expandable tabs hydrate lazily via /api/ref, which enriches an out-of-corpus pub over SerpApi,
    so the card is not a dead end even though nothing is in the local DB."""
    pub = h.get("pub") or ""
    country = (h.get("country") or (pub[:2] if pub[:2].isalpha() else "")).upper()
    title = h.get("title") or "(untitled)"
    abstract = h.get("abstract") or ""
    # A real semantic relevancy so the card sits comparably next to corpus cards and the listwise
    # reranker judges it on the same footing (its channel of origin is NOT a ranking signal).
    score = 0.0
    if qvec is not None and (title or abstract):
        try:
            score = _cosine(qvec, embed.embed_query((title + ". " + abstract)[:8000], 768))
        except Exception:
            score = 0.0
    # Best-effort legal status from the kind code carried in the publication number.
    st = {"code": "external", "label": "External result", "tone": "muted",
          "note": "Found in an external patent database; not in the local corpus."}
    try:
        m = re.search(r"([A-Z]\d?)$", _join_key(pub))
        st = status_mod.classify_status(m.group(1) if m else None, country,
                                        _d(h.get("date")), None, _d(h.get("date")))
    except Exception:
        pass
    prov = []
    for s in dict.fromkeys(h.get("sources") or []):
        prov.append({"label": _src_label(s), "cls": "prov-api"})
    return {
        "rank": 0,                       # rewritten by the listwise reranker / caller
        "family": h.get("family_id") or ("fed:" + pub),
        "pid": None, "pub": pub, "kind": None,
        "title": title, "abstract": abstract, "country": country,
        "flag": FLAG.get(country, "🏳️"),
        "publication_date": h.get("date"), "filing_date": None, "priority_date": None,
        "family_id": h.get("family_id"), "tier": None, "facsimile_path": None,
        "assignees": [h["assignee"]] if h.get("assignee") else [],
        "inventors": [],
        "cpc": [{"code": c, "first": False} for c in (h.get("cpc") or [])[:6]],
        "legal_events": [],
        "match_score": round(score, 3), "match_coord": "", "match_kind": None,
        "relevancy": _relevancy(score),
        "status": st, "basis": "n/a", "sfid": None,
        "channels": [], "prov": prov,
        "covers_elements": [], "n_covers": 0, "has_local_claims": False,
        # office links BUILT via pubnorm (single link-builder, zero-padded/kind-coded) rather than
        # the raw federated hit url, which carries the dropped-zero form Google/Espacenet 404 on.
        "espacenet": pubnorm.espacenet_url(pub, h.get("family_id")) or h.get("espacenet"),
        "google_patents": pubnorm.google_url(pub) or h.get("url"),
        "drawings_provenance": None,
        "claims": [], "description": [], "figure_caps": [], "images": [], "n_images": 0,
        "matched_coord_raw": None, "has_content": bool(abstract),
        "federated_only": True,
    }


def merge_federated_cards(cur, cards, fed_hits, qvec):
    """Fold federated hits into the SAME `cards` list. Returns the (possibly extended) list.

    A hit that resolves to a local family which is already a displayed card -> add its API
    source chip(s) to that card (dedup). A hit that resolves to a local family NOT displayed ->
    skipped (the corpus already ranked that family; it simply fell outside the window). A hit with
    NO local row -> a full federated-only card, appended so the listwise pass can rank it in."""
    if not fed_hits:
        return cards
    by_fam = {c["family"]: c for c in cards if c.get("family")}
    by_key = {}
    for c in cards:
        k = _join_key(c.get("pub"))
        if k:
            by_key.setdefault(k, c)
    resolved = {}
    try:
        resolved = _resolve_fed_pubs(cur, [_join_key(h.get("pub")) for h in fed_hits if h.get("pub")])
    except Exception:
        resolved = {}
    seen_fed = set()
    for h in fed_hits:
        jk = _join_key(h.get("pub"))
        if not jk or jk in seen_fed:
            continue
        seen_fed.add(jk)
        srcs = h.get("sources") or []
        # already carded (by family or by publication number)? -> just record provenance.
        card = None
        r = resolved.get(jk)
        if r and r[1] in by_fam:
            card = by_fam[r[1]]
        elif jk in by_key:
            card = by_key[jk]
        if card is not None:
            for s in srcs:
                _add_api_prov(card, s)
            continue
        if r:
            # In the corpus but ranked below the display window — represented by the corpus, so
            # do not surface a duplicate row.
            continue
        # Genuinely external: render it as a full card in the same list.
        fc = _fed_card(h, qvec)
        cards.append(fc)
        by_key[jk] = fc
    return cards


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
    # per-family federated-API provenance (a result found by both a local channel and an external
    # API records both): {family_key: [api source ids]}, from _attach_fed_family_sources.
    family_sources = report.get("family_sources", {}) or {}

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
        # Per-result source provenance (spec item 5): which channels found THIS reference. Local
        # retrieval channels stay as plain chips (c.channels); the two new channels and every
        # external API get labelled, visually distinct chips (c.prov) so a card shows it was
        # found by e.g. the image search and PQAI, not just "our database".
        allch = fam_channels.get(fam, set())
        local_ch = sorted(x for x in allch if x in _LOCAL_CHANNELS)
        prov = []
        if "docchunks" in allch:
            prov.append({"label": "Semantic chunk match", "cls": "prov-chunk"})
        if "image" in allch:
            prov.append({"label": "Image match", "cls": "prov-image", "icon": "🖼"})
        for api in family_sources.get(fam, []):
            prov.append({"label": _src_label(api), "cls": "prov-api"})
        cards.append({
            "rank": rank, "family": fam, **b,
            "match_score": round(m["score"], 3), "match_coord": _coord_str(m["coord"]),
            "match_kind": m["kind"], "basis": basis,
            "relevancy": _relevancy(m["score"]),         # 0-100 best-passage semantic match
            "status": st,
            "sfid": rep.get("simple_family_id") or None,
            "channels": local_ch,
            "prov": prov,
            "covers_elements": covered, "n_covers": len(covered),
            "has_local_claims": rep["n_claims"] > 0,
            **content,                                    # claims/description/figures/images (from DB+cache)
        })

    # How many of the cards are local-corpus rows — computed BEFORE federated-only cards are
    # folded in, so the "Local corpus" source tag keeps counting the corpus, not the merged total.
    n_local = len(cards)

    # ONE unified list: fold the federated hits into the same `cards` (dedup against the corpus,
    # full cards for external-only hits). The listwise reranker downstream orders the merged set.
    fed = report.get("federation") or {}
    if fed.get("ok") and fed.get("hits"):
        try:
            merge_federated_cards(cur, cards, fed.get("hits") or [], qvec)
        except Exception:
            import traceback
            traceback.print_exc()          # never let provenance-merge failure 500 the page

    _attach_family_members(cur, cards)
    chart = build_claim_chart(report)
    cur.close(); conn.close()
    return {
        "query": query, "mode": report.get("mode"), "subject": s,
        "subject_flag": FLAG.get(subject_obj.jurisdiction, "") if subject_obj else "",
        # zero-padded Google Patents link for the subject header (same dropped-zero fix as the cards)
        "subject_google": (pubnorm.google_url(s) if s else None),
        "rounds": report.get("rounds"), "n_families": report.get("n_families"),
        "channels_used": report.get("channels_used", []),
        "languages": report.get("languages", []),
        "llm_usage": report.get("llm_usage", {}),
        "cross_encoder_rerank": report.get("cross_encoder_rerank", {}),
        "elements": report["elements"],
        "element_coverage": report.get("element_coverage", {}),
        "claim_chart": chart,
        "cards": cards,
        "n_local": n_local,
        "substance_filter": {k: v for k, v in subs_stats.items() if k != "titleonly_ids"},
        "domain": report.get("domain"),
        "federation": report.get("federation"),
        "source_tags": _source_tags(report, n_local),
        "federation_offered": bool(report.get("federation_offered")),
        "coverage_ledger": {
            "cpc_branches": report.get("cpc_branches", []),
            "round_new_families": report.get("round_new_families", []),
            "combination_view": report.get("combination_view", {}),
        },
    }
