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
#  CPC subclasses taken from the references the reading already trusts, as working-set seeds.
#  Cheap: the build's cost is the text-column scan, not the class count.
SEED_CLASS_MAX = int(os.environ.get("ACQUIRE_SEED_CLASSES", "24"))


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
# route 0: the world, with its text
# ---------------------------------------------------------------------------
#  How many rounds of the vocabulary loop. Each one re-queries with the terms the art ITSELF uses
#  and the classes it ACTUALLY lives in, which is the only reliable way to stop matching our own
#  vocabulary. Two is where new families stopped appearing in testing.
WS_ROUNDS = int(os.environ.get("ACQUIRE_WS_ROUNDS", "2"))
WS_PER_QUERY = int(os.environ.get("ACQUIRE_WS_PER_QUERY", "300"))
WS_INGEST_MAX = int(os.environ.get("ACQUIRE_WS_INGEST", "200"))
WS_CANDIDATES = int(os.environ.get("ACQUIRE_WS_CANDIDATES", "60"))


#  Terms so common in any patent that learning them widens a query to nothing. Not a stop-word
#  list for text: these are specifically the words `top_terms` returns for almost every mechanical
#  document, and each one alone will pull an entire CPC class.
_GENERIC = {
    "assembly", "apparatus", "device", "system", "member", "portion", "element", "unit", "means",
    "chamber", "chambers", "housing", "casing", "body", "wall", "walls", "surface", "opening",
    "hole", "inlet", "outlet", "conduit", "duct", "pipe", "tube", "passage", "flow", "air",
    "gas", "gases", "fluid", "engine", "combustion engine", "combustion engines", "cylinder",
    "vehicle", "aircraft", "motor", "application", "embodiment", "invention", "figure",
}


def _anchor_terms(brief, title, claims):
    """Two or three words that say what field this invention is in, for anchoring learned terms."""
    text = " ".join([str(title or ""), str(brief or "")[:1200],
                     " ".join(str(c.get("text") or "")[:300] for c in (claims or []))]).lower()
    #  Deliberately a fixed vocabulary rather than an LLM call: this runs inside a loop, it must be
    #  deterministic, and getting it wrong silently narrows every query in the round.
    candidates = [
        "vacuum", "suction", "gripper", "grip", "handle", "handheld", "hand-held", "portable",
        "lifting", "lifter", "workpiece", "pump", "blower", "power tool", "hand tool",
        "robot", "end effector", "conveyor", "clamp", "chuck", "seal", "cup",
    ]
    return [c for c in candidates if c in text]


def by_worldset(claims, hints=None, brief="", title="", subject=None, seeds=(), emit=None):
    """Search all 170M patents, in the classes that own each uncovered claim, and INGEST WITH TEXT.

    This is the route that fixes the failure the other two only work around. Measured against the
    ten references an attorney filed: three were absent from the local corpus entirely and three
    more were text-less stubs — and NINE OF THE TEN are in a BigQuery working set with their full
    text, including all three that were unreachable. The seeded corpus was never the world; it was
    eight CPC branches, and the art that invalidates a claim is routinely somewhere else.

    Three loops, in order of what they buy:

      1. CLASS-TARGETED LEXICAL over the working set, per claim, in the classes the model says own
         that claim's idea.
      2. THE VOCABULARY LOOP. Take what round 1 found, read the terms Google already extracted for
         those documents (top_terms) and the CPC groups they actually carry, and query again with
         THOSE. A model asked what else a thing might be called will not produce Blatt's real
         words, "porous frequency-distorter insert"; the documents that disclose it will.
      3. THE SIMILAR GRAPH. Google's own precomputed nearest neighbours of every seed, over all
         170M publications — free query-by-example with no index of ours to maintain. On the
         reference the attorney called the best match it returns 1950s art, which is the era that
         kills claims and loses on every relevance ranking there is.

    Everything selected is ingested WITH ITS TEXT or not at all.
    """
    out = {"candidates": [], "plan": {}, "table": "", "rows": 0, "n_new": 0, "with_text": 0,
           "terms": [], "classes": [], "queries": 0, "error": ""}
    if not ENABLED:
        out["error"] = "acquisition disabled"
        return out
    try:
        import worldset
    except Exception as e:
        out["error"] = f"worldset unavailable: {e}"
        return out

    plan = classes_for(claims, hints=hints, brief=brief, title=title)
    out["plan"] = plan
    if not plan:
        out["error"] = "no classes named"
        return out
    cpc = sorted({c for p in plan.values() for c in (p.get("cpc") or [])})
    #  The subject's own classes belong in the working set too: the art that anticipates a claim
    #  outright usually IS in the invention's field, and only the awkward limitations are not.
    for extra in (getattr(subject, "cpc", None) or []):
        c = worldset.valid_cpc(extra)
        if c and c not in cpc:
            cpc.append(c)
    if not cpc:
        out["error"] = "no valid classes"
        return out
    date_max = getattr(subject, "efd", None)
    if emit:
        emit("worldset_build_start", cpc=cpc, date_max=str(date_max or ""))
    try:
        ws = worldset.build(cpc, date_max=date_max)
    except Exception as e:
        traceback.print_exc()
        out["error"] = f"working set failed: {str(e)[:200]}"
        return out
    out["table"], out["rows"] = ws.get("table", ""), ws.get("rows")
    out["classes"] = ws.get("cpc") or cpc
    if not out["table"]:
        out["error"] = ws.get("error") or "no working set"
        return out
    if emit:
        emit("worldset_built", rows=out["rows"], cpc=len(out["classes"]),
             cached=ws.get("cached"), gb=round(float(ws.get("gb") or 0)))

    found = {}                       # pub -> {"for_claim", "why"}
    per_claim = {}                   # label -> [pub], in the order that label's queries found them
    terms_used = set()
    #  What this invention IS, in two or three words the art would also use. Every learned-vocabulary
    #  query must still match one of these, or the loop walks out of the field: see the comment on
    #  `sweep(..., anchors, ...)` below.
    anchors = [a for a in _anchor_terms(brief, title, claims) if a][:6]
    if anchors:
        print(f"[acquire] domain anchors for the vocabulary loop: {', '.join(anchors)}", flush=True)

    def sweep(label, must_any, must_all, classes, why):
        if not must_any:
            return
        out["queries"] += 1
        bucket = per_claim.setdefault(label, [])
        for h in worldset.lexical(out["table"], must_any=must_any, must_all=must_all,
                                  cpc=classes, limit=WS_PER_QUERY):
            if h["pub"] not in found:
                found[h["pub"]] = {"for_claim": label, "why": why, "title": h.get("title")}
                bucket.append(h["pub"])

    #  ROUND 1 — what the model thinks this claim is about, in the classes it named.
    for label, p in plan.items():
        kws = list(p.get("keywords") or [])
        terms_used.update(kws)
        sweep(label, kws, [], p.get("cpc") or [], "class-targeted keywords")
        #  and the same terms with NO class filter, because a class guess that is wrong should
        #  not also be a wall.
        sweep(label, kws, [], [], "keywords, unclassed")

    #  ROUND 2+ — the vocabulary and the classes the ART uses, not the ones we guessed.
    for rnd in range(max(0, WS_ROUNDS - 1)):
        if not found:
            break
        heads = list(found)[:120]
        try:
            learned = [t["term"] for t in worldset.top_terms(heads, limit=120)
                       if len(t["term"]) > 3][:40]
            classes = [c["code"][:8] for c in worldset.classes_of(heads, limit=24)]
        except Exception:
            traceback.print_exc()
            break
        fresh = [t for t in learned if t.lower() not in {x.lower() for x in terms_used}
                 and t.lower() not in _GENERIC]
        if not fresh:
            break
        terms_used.update(fresh)
        out["terms"] = sorted(terms_used)[:80]
        if emit:
            emit("worldset_vocab", round=rnd + 2, terms=fresh[:12], classes=classes[:8])
        print(f"[acquire] vocabulary round {rnd + 2}: the art calls it "
              f"{', '.join(fresh[:10])} — and lives in {', '.join(classes[:6])}", flush=True)
        before = len(found)
        for label in plan:
            #  ANCHORED. Learned vocabulary is not free of its class: run this unanchored against
            #  F01N and "exhaust, chamber, silencer, engine" retrieves the whole automotive muffler
            #  field, which is enormous, dense, and not the invention. MEASURED on the first live
            #  run of this loop: round 2 returned diesel silencers, aircraft attenuators and model
            #  airplane mufflers, and not one hand-tool or vacuum document. The subject's own
            #  domain words go in as must_all so a learned term can WIDEN the query without being
            #  allowed to relocate it.
            sweep(label, fresh, anchors, classes,
                  f"vocabulary learned from the art, round {rnd + 2}")
            if not anchors:
                sweep(label, fresh, [], classes,
                      f"vocabulary learned from the art, unanchored, round {rnd + 2}")
        if len(found) == before:
            break

    #  ROUND 3 — Google's own similarity graph from every seed we trust.
    seed_pubs = [p for p in (seeds or [])][:40]
    if seed_pubs:
        try:
            for r in worldset.similar_to(seed_pubs, limit=200, date_max=date_max):
                if r["pub"] not in found:
                    found[r["pub"]] = {"for_claim": "", "why": "similar-graph neighbour",
                                       "title": ""}
                    per_claim.setdefault("~similar", []).append(r["pub"])
        except Exception:
            traceback.print_exc()

    if not found:
        out["error"] = "no hits"
        return out
    print(f"[acquire] worldset: {out['queries']} queries over {len(out['classes'])} classes of "
          f"{out['rows'] or '?'} publications -> {len(found)} distinct hits", flush=True)

    #  INGEST WITH TEXT. Bounded, because each one is a row plus claims plus paragraphs plus
    #  embeddings in the local corpus.
    #
    #  ROUND ROBIN, NOT `list(found)[:200]`. `found` is filled claim by claim and each query
    #  contributes up to WS_PER_QUERY=300 publications, so a flat slice of the first 200 is the
    #  first claim's first query and nothing else: every later claim, both vocabulary rounds and
    #  the whole similarity graph were being collected and then silently discarded. An empty row
    #  for claim 7 reads on the page as "no such art exists", which is the opposite of what
    #  happened. Take one from each bucket in turn instead, and say what the cap dropped.
    pubs, taken = [], {k: 0 for k in per_claim}
    depth = 0
    while len(pubs) < WS_INGEST_MAX and per_claim:
        progressed = False
        for label, bucket in per_claim.items():
            if depth >= len(bucket):
                continue
            progressed = True
            pubs.append(bucket[depth])
            taken[label] += 1
            if len(pubs) >= WS_INGEST_MAX:
                break
        if not progressed:
            break
        depth += 1
    dropped = {l: len(b) - taken.get(l, 0) for l, b in per_claim.items()
               if len(b) > taken.get(l, 0)}
    if dropped:
        print(f"[acquire] ingest capped at {WS_INGEST_MAX}: took "
              + ", ".join(f"{taken[l]} of {len(per_claim[l])} for {l}" for l in per_claim),
              flush=True)
    if emit:
        emit("worldset_ingest_start", n=len(pubs))
    try:
        res = worldset.ingest(pubs, table=out["table"])
        out["n_new"], out["with_text"] = res.get("rows", 0), res.get("with_text", 0)
    except Exception as e:
        traceback.print_exc()
        out["error"] = f"ingest failed: {str(e)[:200]}"
        return out

    import db
    have = {}
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT publication_number pub, title,
                                  COALESCE(NULLIF(simple_family_id,''), publication_number) fam, id
                           FROM publications WHERE publication_number = ANY(%s)""", (pubs,))
            have = {r["pub"]: r for r in cur.fetchall()}
    except Exception:
        traceback.print_exc()
    seen_fam = set()
    for pub in pubs:
        row = have.get(pub)
        if not row or row["fam"] in seen_fam:
            continue
        seen_fam.add(row["fam"])
        meta = found.get(pub) or {}
        out["candidates"].append({
            "pub": pub, "fam": row["fam"], "pid": row["id"],
            "title": (row.get("title") or meta.get("title") or "")[:300],
            "for_claim": meta.get("for_claim") or "",
            "acquired": "worldset", "why": meta.get("why") or "",
        })
        if len(out["candidates"]) >= WS_CANDIDATES:
            break
    if emit:
        emit("worldset_done", n=len(out["candidates"]), with_text=out["with_text"])
    return out


# ---------------------------------------------------------------------------
# route 0b: ONE QUERY PORTFOLIO PER LIMITATION
# ---------------------------------------------------------------------------
def by_limitation(limitations, brief="", title="", subject=None, seeds=(), emit=None,
                  log=print):
    """Search the world for each REQUIREMENT separately, screen against it, ingest what survives.

    `by_worldset` above asks one keyword question per uncovered claim and ORs its terms together.
    That shape cannot express what a limitation is — a thing, in a place, in a kind of apparatus —
    and measured against the ten references an attorney filed it returns 111,545 hits with no
    usable rank. This route asks the faceted question with proximity instead: 14,373 hits holding
    nine of the ten, and per-limitation ranking that moves Bosch from position 8,810 under one
    invention-wide query to 135 under the query for the requirement it was actually cited for.

    See limitation_query for the measurements and for why the pool is sized for a screener rather
    than for the reader.
    """
    out = {"candidates": [], "plan": {}, "table": "", "rows": 0, "n_new": 0, "with_text": 0,
           "classes": [], "queries": 0, "pool": {}, "screened": 0, "error": ""}
    if not ENABLED:
        out["error"] = "acquisition disabled"
        return out
    lims = [l for l in (limitations or []) if str(l.get("text") or "").strip()]
    if not lims:
        out["error"] = "no limitations"
        return out
    try:
        import limitation_query
        import worldset
    except Exception as e:
        out["error"] = f"unavailable: {e}"
        return out

    plan = limitation_query.facets_for(lims, brief=brief, title=title, log=log)
    out["plan"] = plan
    if not plan:
        out["error"] = "no facets returned"
        return out

    #  The working set's classes: every class the facets named, plus the subject's own, plus the
    #  subclasses the references the reading ALREADY TRUSTS actually carry.
    #
    #  The last of those is the one that is not a guess. A model asked where an idea is classified
    #  answers from what it knows; the documents that turned out to disclose something answer from
    #  what is true, and they are already in front of us. It also makes the class list far more
    #  stable between runs, which matters more than it looks: the working set is keyed on a hash of
    #  (classes, date), so a class list that wobbles builds a fresh $9.38 table every search
    #  instead of reusing one.
    #
    #  Widening this is nearly free. The build scans ~1,500 GB of text columns whatever the WHERE
    #  says — that is the whole cost — so ten classes and forty cost the same. Only MAX_ROWS bounds
    #  the result.
    cpc = sorted({c for p in plan.values() for c in (p.get("cpc") or [])})
    for extra in (getattr(subject, "cpc", None) or []):
        c = worldset.valid_cpc(extra)
        if c and c not in cpc:
            cpc.append(c)
    if seeds:
        try:
            import db
            with db.cursor() as cur:
                cur.execute(
                    """SELECT substring(cl.symbol from 1 for 4) sub, count(*) n
                       FROM classifications cl JOIN publications p ON p.id = cl.publication_id
                       WHERE p.publication_number = ANY(%s)
                       GROUP BY sub ORDER BY n DESC LIMIT %s""",
                    ([str(p) for p in seeds][:60], SEED_CLASS_MAX))
                learned = [r["sub"] for r in cur.fetchall()]
            fresh = [c for c in (worldset.valid_cpc(s) for s in learned) if c and c not in cpc]
            if fresh:
                cpc.extend(fresh)
                log(f"[limq] the art already trusted is classified in "
                    f"{', '.join(fresh[:12])} — added to the working set")
        except Exception:
            traceback.print_exc()
    cpc = sorted(set(cpc))
    if not cpc:
        out["error"] = "no valid classes"
        return out
    date_max = getattr(subject, "efd", None)
    if emit:
        emit("limq_build_start", cpc=cpc, date_max=str(date_max or ""))
    try:
        ws = worldset.build(cpc, date_max=date_max, log=log)
    except Exception as e:
        traceback.print_exc()
        out["error"] = f"working set failed: {str(e)[:200]}"
        return out
    out["table"], out["rows"] = ws.get("table", ""), ws.get("rows")
    out["classes"] = ws.get("cpc") or cpc
    if not out["table"]:
        out["error"] = ws.get("error") or "no working set"
        return out

    found = limitation_query.search(lims, out["table"], date_max=date_max, plan=plan,
                                    brief=brief, title=title, log=log, emit=emit)
    out["queries"], out["pool"] = found.get("queries", 0), found.get("pool") or {}
    if found.get("error"):
        out["error"] = found["error"]
    #  GOOGLE'S OWN SIMILARITY GRAPH, from the references the reading already trusts. One query,
    #  and it is the cheapest way out of the neighbourhood any keyword walks in circles inside.
    seed_pubs = [p for p in (seeds or [])][:40]
    if seed_pubs and found.get("by_limitation"):
        try:
            known = {c["pub"] for c in found["candidates"]}
            extra = [{"pub": r["pub"], "fam": r["pub"], "title": "", "abstract": "",
                      "score": 0.0, "screen": None, "era": "", "for_limitation": "",
                      "acquired": "similar_graph", "why": "Google similarity neighbour"}
                     for r in worldset.similar_to(seed_pubs, limit=200, date_max=date_max,
                                                  log=log) if r["pub"] not in known]
            if extra:
                #  Its own bucket, so the round robin gives it a share rather than the tail.
                found.setdefault("by_limitation", {})["similar_graph"] = extra
                log(f"[limq] similarity graph added {len(extra)} neighbours of "
                    f"{len(seed_pubs)} trusted references")
        except Exception:
            traceback.print_exc()
    if not found.get("by_limitation"):
        out["error"] = out["error"] or "no hits"
        return out

    chosen = limitation_query.screen_and_select(found, plan, log=log, emit=emit)
    out["screened"] = len(chosen)
    if not chosen:
        out["error"] = out["error"] or "nothing survived the screen"
        return out

    #  INGEST WITH TEXT, and only what survived the screen. `by_worldset` ingests 200 documents
    #  chosen lexically; this ingests what a screener judged worth reading, which is the same
    #  budget spent on a better list.
    if emit:
        emit("limq_ingest_start", n=len(chosen))
    try:
        res = worldset.ingest([c["pub"] for c in chosen], table=out["table"], log=log)
        out["n_new"], out["with_text"] = res.get("rows", 0), res.get("with_text", 0)
    except Exception as e:
        traceback.print_exc()
        out["error"] = f"ingest failed: {str(e)[:200]}"
        return out

    import db
    have = {}
    pubs = [c["pub"] for c in chosen]
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT publication_number pub, title,
                                  COALESCE(NULLIF(simple_family_id,''), publication_number) fam,
                                  id
                           FROM publications WHERE publication_number = ANY(%s)""", (pubs,))
            have = {r["pub"]: r for r in cur.fetchall()}
    except Exception:
        traceback.print_exc()
    seen_fam = set()
    for c in chosen:
        row = have.get(c["pub"])
        if not row or row["fam"] in seen_fam:
            continue
        seen_fam.add(row["fam"])
        out["candidates"].append({
            "pub": c["pub"], "fam": row["fam"], "pid": row["id"],
            "title": (row.get("title") or c.get("title") or "")[:300],
            #  The LIMITATION it was fetched for, not the claim: that is what the reader is asked
            #  about and what the ledger keys on.
            "for_claim": c.get("for_limitation") or "",
            "acquired": c.get("acquired") or "limitation_portfolio",
            "screen": c.get("screen"), "why": c.get("why") or "",
        })
    log(f"[limq] acquired {len(out['candidates'])} references with text for "
        f"{len(out['pool'])} limitations "
        f"({out['n_new']} new rows, {out['with_text']} with full text)")
    if emit:
        emit("limq_acquired", n=len(out["candidates"]), with_text=out["with_text"])
    return out


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
    #  MATERIALISE THE ROW FIRST, THEN RECOVER THE TEXT.
    #
    #  enrich._persist_full_text opens with "SELECT id FROM publications WHERE
    #  publication_number = %s" and returns {"ok": False, "reason": "not_in_corpus"} when there is
    #  no row. These are by definition the documents with no row — that is how they were selected —
    #  so calling enrich_publication on them could only ever return zero, and did: 0 of 19 on the
    #  first live run, which looked like an unreachable source and was a guaranteed no-op.
    #  Measured after inserting the row first: US-10000000-B2 recovered 3 claims and 20 paragraphs.
    try:
        import enrich
        import external
        got = 0
        for pub in missing:
            try:
                placed = external.materialise(
                    {external._norm(pub): {"pub_number": pub, "title": "", "abstract": ""}})
            except Exception:
                continue
            if not placed:
                continue
            pid = next((v[0] for v in placed.values()
                        if isinstance(v, (list, tuple)) and v), None)
            try:
                r = enrich.enrich_publication(pub, reembed=False)
            except Exception:
                r = None
            #  Kept even when no text came back: the row now exists, so a later search can enrich
            #  it, and the reader reports "no text" rather than the document being invisible.
            out["candidates"].append({"pub": pub, "fam": pub, "pid": pid, "title": "",
                                      "for_claim": "", "acquired": "citation"})
            if r and r.get("ok"):
                got += 1
        out["n_new"] = got
    except Exception as e:
        traceback.print_exc()
        out["error"] = str(e)[:200]
    print(f"[acquire] citation neighbourhood: {out['n_edges']} cited documents this corpus does "
          f"not hold, {out['n_new']} of {len(missing)} recovered", flush=True)
    return out
