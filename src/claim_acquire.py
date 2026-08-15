"""Go and GET the art this corpus does not contain, for the claims nothing covers.

WHY, MEASURED. Ten references were filed against US 2026/0109053 A1 by an attorney. Three of them
are not in this corpus at all, and three more are in it only as citation stubs with no text. Their
classifications say why:

    Sadler       F04D25/02, A47L5/14    blower
    Perlmutter   Y10T408/554, B25H1     drilling
    Quackenbush  B25F5/00, B23B45/04    portable power tools
    Crevling / Sato / Bosch             not held at all

The corpus is seeded from eight vacuum-gripping CPC branches. Schmalz's inventive point is noise
damping in an exhaust airflow, which lives in F01N mufflers, G10K sound absorption, B25F hand
tools and A47L blowers. The attorney went there. Retrieval cannot, because the retrieval universe
IS the corpus — no amount of re-ranking or query rewriting reaches a document that was never
indexed. Widening the funnel does not help either: SCREEN_TOP 2,500 -> 5,000 was already measured
to LOWER recall.

So this module acquires documents rather than ranking them, along the two routes an examiner uses:

  * BY CONCEPT AND CLASS. For each uncovered claim, the model names the CPC subclasses where that
    idea is actually solved — "a muffler inside a handle" is B25F5/02 and F01N1/24, not B66C1 —
    and each becomes its own class-scoped query through App A's fan-out. One query per subclass,
    not one query listing all of them: the row limit is per query, so a combined query lets the
    largest subclass crowd out the rest.
  * BY CITATION. The documents our own best references cite, which this corpus does not hold. This
    is how examiners find art: the art points at its own ancestors, across classification
    boundaries, and those edges are already sitting in the citations table unresolved.

Both end at `external.materialise`, which writes the publications into the corpus, so a document
acquired for one search is indexed for every later one. Nothing here ranks anything; it returns
candidates for the rescue's reader, which applies the same grounding and refutation gates as
everything else. Fail-soft throughout: an unreachable source returns fewer candidates, never an
error, because this runs at the end of a search that already has an answer.
"""
from __future__ import annotations

import json
import os
import traceback

import llm

#  CPC subclasses asked for per uncovered claim. Four characters ("F01N"), which is the level the
#  fan-out queries at.
CPC_PER_CLAIM = int(os.environ.get("ACQUIRE_CPC_PER_CLAIM", "4"))
#  Claims that get a concept-and-class acquisition. The uncovered ones, worst first.
MAX_CLAIMS = int(os.environ.get("ACQUIRE_MAX_CLAIMS", "6"))
#  Ceiling on the fan-out. App A caps bulk_search at 80 queries.
MAX_QUERIES = int(os.environ.get("ACQUIRE_MAX_QUERIES", "40"))
#  New publications written into the corpus per acquisition pass.
MAX_MATERIALISE = int(os.environ.get("ACQUIRE_MAX_NEW", "300"))
#  Candidates handed back to the reader. Every one costs a full-text read.
MAX_CANDIDATES = int(os.environ.get("ACQUIRE_MAX_CANDIDATES", "40"))
#  Citation neighbourhood: how many read references to expand, and how many of their unresolved
#  cited documents to acquire.
CITE_FROM_TOP = int(os.environ.get("ACQUIRE_CITE_FROM_TOP", "40"))
CITE_MAX = int(os.environ.get("ACQUIRE_CITE_MAX", "120"))

ENABLED = os.environ.get("ACQUIRE_ENABLED", "1") != "0"


# ---------------------------------------------------------------------------
# which classes own the idea
# ---------------------------------------------------------------------------
_CPC_SYS = (
    "You are a patent examiner deciding WHERE TO SEARCH for prior art on specific claims.\n"
    "\n"
    "For each claim you are given the claim, the idea behind it, and the invention it belongs to. "
    "Name the CPC subclasses (four characters, e.g. \"F01N\", \"G10K\", \"B25F\") where that "
    "particular idea is ACTUALLY SOLVED AND CLASSIFIED — by anyone, in any field.\n"
    "\n"
    "THIS IS THE WHOLE POINT AND IT IS EASY TO GET WRONG: do NOT name the subclass of the "
    "invention as a whole. The search has already covered that and found nothing for this claim, "
    "which almost always means the art for it sits somewhere else. A muffler inside a handle is "
    "classified in F01N (silencers) and B25F (portable power tool details), not in B66C (load "
    "grabs). A battery charging circuit is H02J. Sound absorbing material is G10K. An audible "
    "alarm is G08B. Go to the field that OWNS the component or the effect.\n"
    "\n"
    "Also give 4-8 keywords in the vocabulary THAT field uses, which is usually not the "
    "vocabulary of the claim.\n"
    "\n"
    'Return ONLY JSON: {"claims":[{"item":"<the claim label verbatim>","cpc":["F01N","B25F"],'
    '"keywords":["muffler","exhaust silencer","baffle"],"why":"one short sentence"}]} with one '
    "entry per claim, in the order given."
)


def classes_for(claims, hints=None, brief="", title=""):
    """{claim label: {"cpc": [...], "keywords": [...], "why": str}} — where to go looking."""
    claims = list(claims or [])[:MAX_CLAIMS]
    if not claims:
        return {}
    hints = hints or {}
    payload = {
        "invention_title": str(title or "")[:200],
        "invention": str(brief or "")[:2000],
        "claims_with_no_prior_art": [
            {"item": c["label"], "text": str(c.get("text") or "")[:1500],
             "idea": hints.get(c["label"]) or ""}
            for c in claims],
    }
    try:
        out = llm.chat_json(_CPC_SYS, json.dumps(payload, ensure_ascii=False),
                            max_tokens=3000) or {}
    except Exception:
        traceback.print_exc()
        return {}
    import deep_analysis
    items = [c["label"] for c in claims]
    plan = {}
    for item, raw in zip(items, deep_analysis._align(out.get("claims"), items)):
        if not isinstance(raw, dict):
            continue
        cpc = []
        for c in (raw.get("cpc") or []):
            c4 = "".join(str(c).split()).upper()[:4]
            #  A CPC subclass is a letter, two digits and a letter. Anything else is the model
            #  inventing a symbol, and a malformed one silently matches nothing.
            if len(c4) == 4 and c4[0].isalpha() and c4[1:3].isdigit() and c4[3].isalpha() \
                    and c4 not in cpc:
                cpc.append(c4)
        kws = [" ".join(str(k).split()) for k in (raw.get("keywords") or []) if str(k).strip()][:8]
        if not cpc and not kws:
            continue
        plan[item] = {"cpc": cpc[:CPC_PER_CLAIM], "keywords": kws,
                      "why": " ".join(str(raw.get("why") or "").split())[:200]}
    return plan


# ---------------------------------------------------------------------------
# route 1: fetch by concept and class
# ---------------------------------------------------------------------------
def by_concept(claims, hints=None, brief="", title="", emit=None):
    """Acquire art for uncovered claims from the classes that own their subject matter.

    -> {"candidates": [{pub, fam, pid, for_claim, title}], "plan": {...}, "n_new": int, ...}
    """
    out = {"candidates": [], "plan": {}, "n_new": 0, "n_candidates": 0, "queries": 0,
           "error": ""}
    if not ENABLED:
        out["error"] = "acquisition disabled"
        return out
    try:
        import external
    except Exception as e:
        out["error"] = f"external unavailable: {e}"
        return out
    if not getattr(external, "ENABLED", False):
        out["error"] = "external search disabled"
        return out

    plan = classes_for(claims, hints=hints, brief=brief, title=title)
    out["plan"] = plan
    if not plan:
        out["error"] = "no classes named"
        return out

    queries, for_claim = [], {}

    def add(source, q, label, why, cpc=None):
        q = " ".join(str(q or "").split())
        if len(q) < 6 or len(queries) >= MAX_QUERIES:
            return
        queries.append({"source": source, "q": q[:2000], "element": label, "why": why,
                        "cpc": list(cpc or [])})

    for label, p in plan.items():
        kw = " ".join(p["keywords"][:8])
        text = next((c.get("text") for c in claims if c["label"] == label), "") or ""
        idea = (hints or {}).get(label) or text
        #  ONE QUERY PER SUBCLASS. A single query naming four subclasses shares one row budget and
        #  the largest of them crowds out the rest, which is how a document sitting in exactly the
        #  subclass we asked about never comes back.
        for c4 in p["cpc"]:
            if kw:
                add("bigquery_gpatents", kw, f"{label} / {c4}",
                    f"{label}: {p['why']}"[:200], cpc=[c4])
                for_claim[f"{label} / {c4}"] = label
        if idea:
            add("pqai", str(idea)[:1200], label, f"{label}, semantic, unclassed")
            for_claim[label] = label
        if kw:
            add("uspto", kw, label, f"{label}, US titles in the owning field")
            for_claim[label] = label
    out["queries"] = len(queries)
    if not queries:
        out["error"] = "no queries built"
        return out
    if emit:
        emit("acquire_start", n=len(queries), claims=len(plan),
             cpc=sorted({c for p in plan.values() for c in p["cpc"]}))

    res = external.bulk(queries)
    cands = res.get("candidates") or []
    out["n_candidates"] = len(cands)
    if not cands:
        out["error"] = res.get("error") or "no candidates returned"
        return out

    #  WRITE THEM INTO THE CORPUS. This is the part that makes the acquisition permanent: the next
    #  search for anything in these classes finds them locally, already indexed.
    try:
        import db
        records = external.best_records(cands)
        #  Which claim each candidate came back for, so the reader can say why it is here.
        claim_of = {}
        for c in cands:
            pn = c.get("pub_number") or ""
            el = c.get("element") or c.get("query_element") or ""
            if not pn:
                continue
            #  Keyed the same way best_records keys its dict, or the lookup silently misses and
            #  every rescued document loses the record of which claim it was fetched for.
            k = external._norm(pn)
            if k not in claim_of:
                claim_of[k] = for_claim.get(el, el)
        with db.cursor() as cur:
            fam_of, have = external.family_keys(cur, records)
        #  Bounded: rank is not available here (these are class-scoped, not fused against the
        #  whole invention), so take the richest records the fan-out returned.
        keys = [k for k in records if k not in have][:MAX_MATERIALISE]
        winners = {k: records[k] for k in keys}
        placed = dict(have)
        placed.update(external.materialise(winners))
        out["n_new"] = len(placed) - len(have)
    except Exception as e:
        traceback.print_exc()
        out["error"] = f"materialise failed: {str(e)[:200]}"
        return out

    #  `materialise`/`_resolve_existing` return {normalised pub: (publication_id, FAMILY)} — the
    #  second element is the family key, NOT the publication number. Taking it as the number would
    #  hand the reader a family id to look up and every rescued document would come back "no text".
    seen_fam = set()
    for k, v in placed.items():
        if not (isinstance(v, (list, tuple)) and len(v) >= 2):
            continue
        pid, fam = v[0], v[1]
        rec = records.get(k) or {}
        pub = rec.get("pub_number") or ""
        if not pub or fam in seen_fam:
            continue
        seen_fam.add(fam)
        out["candidates"].append({
            "pub": pub, "fam": fam or pub, "pid": pid,
            "title": (rec.get("title") or "")[:300],
            "for_claim": claim_of.get(k, ""),
            "acquired": "concept_cpc",
        })
        if len(out["candidates"]) >= MAX_CANDIDATES:
            break
    if emit:
        emit("acquire_done", n=len(out["candidates"]), new=out["n_new"])
    print(f"[acquire] concept/CPC: {len(queries)} class-scoped queries over "
          f"{sorted({c for p in plan.values() for c in p['cpc']})} -> {out['n_candidates']} hits, "
          f"{out['n_new']} publications new to this corpus, {len(out['candidates'])} to read",
          flush=True)
    return out


# ---------------------------------------------------------------------------
# route 2: the citation neighbourhood
# ---------------------------------------------------------------------------
def by_citation(pubs, exclude_pubs=(), limit=CITE_MAX, emit=None):
    """Acquire the documents our own best references cite that this corpus does not hold.

    This is how examiners actually work: art points at its own ancestors, and it points ACROSS
    classification boundaries — which is exactly the direction a CPC-seeded corpus cannot go. The
    edges are already in the citations table with nothing behind them; only the documents are
    missing.
    """
    out = {"candidates": [], "n_edges": 0, "n_new": 0, "error": ""}
    if not ENABLED or not pubs:
        return out
    try:
        import db
        import external
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    heads = [p for p in pubs][:CITE_FROM_TOP]
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT c.dst_pub FROM citations c
                   WHERE c.src_pub = ANY(%s)
                     AND NOT EXISTS (SELECT 1 FROM publications p
                                     WHERE p.publication_number = c.dst_pub)""", (heads,))
            missing = [r["dst_pub"] for r in cur.fetchall() if r["dst_pub"]]
    except Exception as e:
        traceback.print_exc()
        out["error"] = str(e)[:200]
        return out
    out["n_edges"] = len(missing)
    skip = {str(p) for p in (exclude_pubs or ())}
    missing = [m for m in missing if m not in skip][:limit]
    if not missing:
        return out
    if emit:
        emit("acquire_cite_start", n=len(missing))
    #  Fetched one at a time through the same recovery chain the reading stage uses, so a
    #  document acquired here arrives with whatever text any source will give for it.
    try:
        import enrich
        got = 0
        for pub in missing:
            try:
                r = enrich.enrich_publication(pub, reembed=False)
            except Exception:
                continue
            if r and r.get("ok"):
                got += 1
                out["candidates"].append({"pub": pub, "fam": pub, "pid": None, "title": "",
                                          "for_claim": "", "acquired": "citation"})
        out["n_new"] = got
    except Exception as e:
        traceback.print_exc()
        out["error"] = str(e)[:200]
    print(f"[acquire] citation neighbourhood: {out['n_edges']} cited documents this corpus does "
          f"not hold, {out['n_new']} of {len(missing)} recovered", flush=True)
    return out
