"""One query portfolio per LIMITATION, run as a single scan of the BigQuery working set.

WHY THIS EXISTS, MEASURED
-------------------------
The pipeline builds one query set that describes the whole invention. Every reference an attorney
filed against US 2026/0109053 A1 was cited for ONE requirement, and most of them resemble the
invention barely at all — so a query built from the invention ranks them nowhere. That is not a
tuning problem, it is the wrong question asked once instead of the right question asked per
requirement.

The three shapes measured against the working set that holds all ten of those references
(2,392,628 publications, every one with full text):

    what                                          hits      of the 10 attorney references
    bag of keywords, OR-ed  (what we do today)  111,545      10, at no usable rank
    three facets, literal only                    8,331       6
    three facets WITH PROXIMITY                  14,373       9

`must_any` / `must_all` cannot express the middle shape. A limitation is a THING, in a PLACE, in a
KIND OF APPARATUS — "a sound-damping device, in the exhaust air path, of a hand-held vacuum
gripper" — which is a conjunction of three disjunctions. `must_all` demands every synonym at once
and matches nothing; `must_any` demands none of them and matches a tenth of the corpus.

PROXIMITY IS NOT A REFINEMENT, IT IS THE DIFFERENCE BETWEEN SIX AND NINE
-----------------------------------------------------------------------
Sato's patent is titled "Sound absorption structure for air flow path" and the only damping words
in its body are `sound | noise | absorbed | absorb` — never adjacent. Bosch's are
`damping | noise | dampened`. Neither matches `sound[- ]?absorb` and both match "sound within a
sentence of absorb". Two of the ten are reachable only this way.

And the requirement is that the thing is IN the place, not that the document mentions both
somewhere: co-occurrence inside one sentence cut 18,258 hits to 14,373 while keeping all nine.

ERA IS A SEPARATE COMPETITION
-----------------------------
Old art kills claims and loses on every relevance signal there is, because it is short, it is
written in another century's vocabulary, and it has fewer of everything to match on. Quackenbush
(1960, the attorney's most comprehensive match) ranks 1,971 of 14,373 overall and 152 of 1,403
among documents of its own era. Ranking within era buckets and taking a quota from each is what
makes it reachable; a single ranked list never will.

THE COST SHAPE ALLOWS ALL OF THIS
---------------------------------
BigQuery bills for columns scanned, not rows matched, so a portfolio of thirty queries costs
exactly what one query costs — about $0.21 against a 42 GB working set. The whole design follows
from that: evaluate every limitation's facets in ONE scan, pivot, rank per limitation and era, and
never issue a second query. See worldset.py for the working set itself.

NO SILENT TRUNCATION. Every cap here is per limitation and per era, and what a cap dropped is
logged. A single flat cap over a dict that was filled limitation by limitation gives the first
limitation everything and the rest nothing, which reads on the page as "there is no art for
claim 7".
"""
from __future__ import annotations

import json
import os
import re
import traceback

#  Characters a term from a model may contain. Everything else is dropped rather than escaped: a
#  term with a regex metacharacter in it is a mistake, not an intention, and an escaped one is a
#  term that will never match anything.
_SAFE = re.compile(r"[^a-z0-9 /+-]")
#  Terms shorter than this match inside other words and pull whole classes.
MIN_TERM = 4
MAX_TERMS_PER_FACET = int(os.environ.get("LIMQ_MAX_TERMS", "14"))
#  Sentence window for the THING-in-the-PLACE co-occurrence, in characters. 60 measured; 120 was
#  no better on recall and 2,000 more hits.
WINDOW = int(os.environ.get("LIMQ_WINDOW", "60"))
#  Limitations given their own portfolio per pass. The ledger orders them, weakest first.
MAX_LIMITATIONS = int(os.environ.get("LIMQ_MAX_LIMITATIONS", "8"))
#  Candidates returned per limitation, and per era bucket within it.
#
#  MEASURED, and these are not round numbers. Against the working set holding all ten references an
#  attorney filed, the best era-stratified rank of each was: Sadler 55, Bosch 135, Blatt 200,
#  Quackenbush 219, Hukelmann 334, then Perlmutter 1,045 and a long tail. A per-era quota of 40 —
#  the first number that looked sensible — returned NONE of them. 350 reaches five.
#
#  This is a SCREENING pool, not a read pool. What comes out of here is title-and-abstract, gets
#  screened against the limitation (cheap: 2,480 candidates in 33 seconds), and only the survivors
#  are ingested with their text and read. Sizing it for the reader instead is what makes a quota
#  like 40 look reasonable, and 40 finds nothing.
PER_LIMITATION = int(os.environ.get("LIMQ_PER_LIMITATION", "800"))
PER_ERA = int(os.environ.get("LIMQ_PER_ERA", "350"))
#  Ceiling on the whole pass, applied by round-robin across limitations so no limitation is
#  starved by another's yield. This bounds the SCREEN, not the read, and the screen is the cheap
#  stage — 2,480 candidates in 33 seconds — so it is deliberately generous. Sized below
#  PER_LIMITATION x limitations it silently becomes the binding cap again: at 2,400 over six
#  limitations each gets 400, and a reference at era-rank 200 sits at round-robin position ~800
#  because the four era buckets interleave. Blatt is exactly that reference.
MAX_TOTAL = int(os.environ.get("LIMQ_MAX_TOTAL", "6000"))
#  Kept per limitation after the screen — this IS the read budget, and every one of these costs a
#  full-text read. 8 limitations x 30 is 240 reads, and the main loop's 508 reads took 985s on 24
#  workers, so this is roughly 20 minutes on top of a 40-60 minute search. Raise it and check the
#  90-minute guardrail; it is the most valuable spend in the run and also the slowest.
KEEP_PER_LIMITATION = int(os.environ.get("LIMQ_KEEP", "30"))
MIN_SCREEN = int(os.environ.get("LIMQ_MIN_SCREEN", "45"))

ERAS = [("pre1980", 0, 19800000), ("1980_1999", 19800000, 20000000),
        ("2000_2014", 20000000, 20150000), ("2015_now", 20150000, 99999999)]


# ---------------------------------------------------------------------------
# turning a limitation into a searchable conjunction
# ---------------------------------------------------------------------------
_FACET_SYS = (
    "You are a patent examiner writing the SEARCH for one claim limitation.\n"
    "\n"
    "A limitation is a THING, in a PLACE, in a KIND OF APPARATUS: \"a sound-damping device\", "
    "\"in the exhaust air path\", \"of a hand-held vacuum gripper\". Give those three separately.\n"
    "\n"
    "For each one, list the surface forms DIFFERENT TECHNICAL FIELDS use for it — the art that "
    "discloses a limitation is usually in another field and never uses the claim's words. A "
    "silencer in a handle is written as:\n"
    "  automotive  muffler, exhaust silencer, resonator, expansion chamber\n"
    "  acoustics   sound absorber, acoustic attenuator, noise attenuation, absorptive material\n"
    "  power tools noise suppressor, exhaust deflector, muffling chamber\n"
    "  1960s US    muffling means, sound deadening material, baffle plate\n"
    "  translated  noise reduction part, soundproofing member, silencing structure\n"
    "Include the 1960s and translated-from-German/Japanese forms. Old art is what kills claims.\n"
    "\n"
    "RULES\n"
    "- Single words and short phrases only, lower case, no punctuation, no wildcards. Write the "
    "STEM where a stem is safe: \"silenc\" catches silencer, silencing and silenced; \"absorb\" "
    "catches absorber, absorbing and absorbent.\n"
    "- 6 to 14 forms for `thing` and `place`. 4 to 10 for `apparatus`.\n"
    "- NEVER use a word so general it appears in every mechanical patent: device, member, "
    "element, portion, means, unit, assembly, apparatus, housing, body, surface, opening, "
    "chamber, wall, part. They match everything and select nothing.\n"
    "- `apparatus` says what KIND of machine this is, so the query stays in the right domain: "
    "handle, grip, hand-held, portable, power tool, vacuum, suction, blower.\n"
    "- `cpc` : 2 to 6 CPC subclasses (four characters, e.g. F01N, G10K, B25F) where this "
    "particular idea is ACTUALLY classified — by anyone, in any field. NOT the subclass of the "
    "invention as a whole; the search has already covered that.\n"
    "\n"
    'Return ONLY JSON: {"limitations":[{"item":"<the limitation id verbatim>",'
    '"thing":["muffler","silenc"],"place":["exhaust","air flow"],'
    '"apparatus":["handle","portable"],"cpc":["F01N","B25F"],"why":"one short sentence"}]} '
    "with one entry per limitation, in the order given."
)


def _terms(raw, limit=MAX_TERMS_PER_FACET):
    """Model output -> regex-safe alternation terms. Drops rather than escapes."""
    out = []
    for t in (raw or []):
        s = _SAFE.sub(" ", str(t or "").lower()).strip()
        s = re.sub(r"\s+", " ", s)
        if len(s) < MIN_TERM or s in out:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _alt(terms):
    """[term] -> a regex alternation, longest first so the alternation reports the longest match."""
    if not terms:
        return ""
    return "|".join(re.escape(t).replace(r"\ ", r"[- ]?")
                    for t in sorted(terms, key=len, reverse=True))


def proximity(a_terms, b_terms, window=WINDOW):
    """`a` and `b` inside one sentence, either order. "" when either side is empty.

    `[^.]` rather than `.` bounds the match at a sentence: two facets three sentences apart are a
    document that mentions both, which is the question this module exists NOT to ask.
    """
    a, b = _alt(a_terms), _alt(b_terms)
    if not a or not b:
        return ""
    #  No `(?s)`: `[^.]` already spans newlines, and a mid-pattern inline flag is legal in RE2
    #  (BigQuery) but a hard error in Python's `re`, so the pattern would work in the query and
    #  raise in every local test and fallback that touched it.
    return (rf"(?:{a})[a-z]{{0,10}}[^.]{{0,{window}}}(?:{b})|"
            rf"(?:{b})[a-z]{{0,10}}[^.]{{0,{window}}}(?:{a})")


def facets_for(limitations, brief="", title="", log=print):
    """{lim id: {"thing", "place", "apparatus", "cpc", "why"}} — the conjunction to search for.

    A limitation with no `thing` is dropped rather than searched: a query missing its subject facet
    degenerates to "documents that mention exhaust", which is the failure this replaces.
    """
    lims = list(limitations or [])[:MAX_LIMITATIONS]
    if not lims:
        return {}
    payload = {
        "invention_title": str(title or "")[:200],
        "invention": str(brief or "")[:1500],
        "limitations": [{"item": l["id"], "text": str(l.get("text") or "")[:800],
                         "from_claim": l.get("claim_label") or ""} for l in lims],
    }
    try:
        import llm
        out = llm.chat_json(_FACET_SYS, json.dumps(payload, ensure_ascii=False),
                            max_tokens=4000) or {}
    except Exception:
        traceback.print_exc()
        return {}
    try:
        import deep_analysis
        aligned = deep_analysis._align(out.get("limitations"), [l["id"] for l in lims])
    except Exception:
        aligned = out.get("limitations") or []
    plan = {}
    for lim, raw in zip(lims, aligned):
        if not isinstance(raw, dict):
            continue
        thing = _terms(raw.get("thing"))
        place = _terms(raw.get("place"))
        appar = _terms(raw.get("apparatus"), limit=10)
        if not thing:
            log(f"[limq] {lim['id']}: no subject facet returned, skipped")
            continue
        cpc = []
        for c in (raw.get("cpc") or []):
            c4 = "".join(str(c).split()).upper()[:4]
            if (len(c4) == 4 and c4[0].isalpha() and c4[1:3].isdigit() and c4[3].isalpha()
                    and c4 not in cpc):
                cpc.append(c4)
        plan[lim["id"]] = {
            "thing": thing, "place": place, "apparatus": appar, "cpc": cpc[:6],
            "why": " ".join(str(raw.get("why") or "").split())[:200],
            "text": str(lim.get("text") or "")[:400],
            "claim_label": lim.get("claim_label") or "",
        }
    return plan


# ---------------------------------------------------------------------------
# one scan, every limitation
# ---------------------------------------------------------------------------
def _slug(i):
    return f"q{i}"


def build_sql(plan, table, date_max=None, per_era=PER_ERA, per_limitation=PER_LIMITATION):
    """The whole portfolio as ONE query. -> (sql, {slug: lim_id})

    Structure, and each step is there for a measured reason:
      b       the working set, date-bounded to what is prior art
      f       one boolean and one count per limitation, computed once per document
      p       PIVOTED: each document appears once per limitation it satisfies, so what follows is
              a per-limitation ranking rather than one global list
      r       ranked within (limitation, era) — see the docstring on why era is its own contest

    `rank_era` is the ONLY rank filter applied here, deliberately. Adding `rank_q <= N` as a second
    condition looks harmless and quietly undoes the era quota: measured with per_era 350 and
    per_limitation 800, Blatt sits at era-rank 200 and limitation-rank 1,397, Quackenbush at 219
    and 2,429, Hukelmann at 334 and 1,024 — all three inside the era quota, all three cut by the
    global one, and the portfolio returned 2 of 10 instead of 5. The per-limitation cap is enforced
    by `search`, round robin ACROSS eras, so it can never re-impose a global ranking.
    """
    slugs = {}
    cols, structs = [], []
    for i, (lim_id, p) in enumerate(plan.items()):
        s = _slug(i)
        slugs[s] = lim_id
        core = proximity(p["thing"], p["place"]) or f"(?:{_alt(p['thing'])})"
        ctx = _alt(p["apparatus"])
        thing_alt = _alt(p["thing"])
        cols.append(f"REGEXP_CONTAINS(body, r'''{core}''') AS {s}_hit")
        cols.append(f"ARRAY_LENGTH(REGEXP_EXTRACT_ALL(body, r'''{core}''')) AS {s}_n")
        cols.append(f"REGEXP_CONTAINS(claims, r'''{core}''') AS {s}_clm")
        cols.append(f"REGEXP_CONTAINS(head, r'''{thing_alt}''') AS {s}_hd")
        cols.append(f"{'REGEXP_CONTAINS(body, r' + chr(39)*3 + ctx + chr(39)*3 + ')' if ctx else 'TRUE'} AS {s}_ctx")
        #  CLASS IS A BOOST, NEVER A FILTER. A wrong class guess must not be a wall: measured,
        #  narrowing the CPC channel to the subject's own symbols returned 0 of 12 cited references.
        if p["cpc"]:
            like = " OR ".join(f"c LIKE '{c}%'" for c in p["cpc"])
            cols.append(f"EXISTS (SELECT 1 FROM UNNEST(cpc) c WHERE {like}) AS {s}_cpc")
        else:
            cols.append(f"FALSE AS {s}_cpc")
        #  Deliberately crude. This is a RECALL stage feeding a screener and then a reader; a
        #  clever score here is only a worse version of the judgement they make from the text.
        structs.append(
            f"STRUCT('{s}' AS q, {s}_hit AND {s}_ctx AS ok, "
            f"(LEAST({s}_n, 20) + IF({s}_clm, 25, 0) + IF({s}_hd, 40, 0) "
            f"+ IF({s}_cpc, 15, 0)) AS score, {s}_n AS n_cooc, {s}_clm AS in_claims, "
            f"{s}_hd AS in_title, {s}_cpc AS in_class)")
    date_clause = ""
    if date_max:
        d = str(date_max).replace("-", "")[:8]
        if d.isdigit() and len(d) == 8:
            date_clause = f"AND publication_date < {int(d)}"
    #  `pd`, not `publication_date`: the b CTE has already aliased it, and the original column name
    #  does not exist by the time this CASE is evaluated.
    era_case = " ".join(f"WHEN pd < {hi} THEN '{name}'" for name, _lo, hi in ERAS[:-1])
    sql = f"""
    WITH b AS (
      SELECT publication_number AS pub, publication_date AS pd, family_id, cpc,
             IFNULL(title_en, title_any) AS title,
             IFNULL(abstract_en, abstract_any) AS abstract,
             LOWER(CONCAT(IFNULL(title_en, IFNULL(title_any, '')), ' ',
                          IFNULL(abstract_en, IFNULL(abstract_any, '')), ' ',
                          IFNULL(claims_en, IFNULL(claims_any, '')), ' ',
                          IFNULL(description_en, IFNULL(description_any, '')))) AS body,
             LOWER(IFNULL(claims_en, IFNULL(claims_any, ''))) AS claims,
             LOWER(CONCAT(IFNULL(title_en, IFNULL(title_any, '')), ' ',
                          IFNULL(abstract_en, IFNULL(abstract_any, '')))) AS head
      FROM `{table}`
      WHERE publication_date > 0 {date_clause}
    ),
    f AS (
      SELECT pub, pd, family_id, title, abstract,
             CASE {era_case} ELSE '{ERAS[-1][0]}' END AS era,
             {', '.join(cols)}
      FROM b
    ),
    p AS (
      SELECT pub, pd, family_id, title, abstract, era,
             s.q, s.score, s.n_cooc, s.in_claims, s.in_title, s.in_class
      FROM f, UNNEST([{', '.join(structs)}]) s
      WHERE s.ok
    ),
    r AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY q, era ORDER BY score DESC, pd ASC) AS rank_era,
                ROW_NUMBER() OVER (PARTITION BY q ORDER BY score DESC, pd ASC) AS rank_q,
                COUNT(*) OVER (PARTITION BY q) AS pool_q
      FROM p
    )
    SELECT q, pub, pd, family_id, title, abstract, era, score, n_cooc, in_claims, in_title,
           in_class, rank_era, rank_q, pool_q
    FROM r
    WHERE rank_era <= {int(per_era)}
    ORDER BY q, era, rank_era
    """
    return sql, slugs


def search(limitations, table, date_max=None, brief="", title="", plan=None,
           max_total=MAX_TOTAL, log=print, emit=None):
    """Run the portfolio. -> {"by_limitation": {lim_id: [row]}, "plan": ..., "candidates": [row]}

    `candidates` is the flat list to acquire, filled ROUND ROBIN across limitations so a cap can
    never hand one limitation the whole budget. What the cap dropped is logged per limitation.
    """
    out = {"by_limitation": {}, "candidates": [], "plan": {}, "queries": 0, "pool": {},
           "gb": 0.0, "error": ""}
    if not table:
        out["error"] = "no working set"
        return out
    plan = plan if plan is not None else facets_for(limitations, brief=brief, title=title, log=log)
    out["plan"] = plan
    if not plan:
        out["error"] = "no facets"
        return out
    sql, slugs = build_sql(plan, table, date_max=date_max)
    out["queries"] = len(plan)
    if emit:
        emit("limq_start", limitations=len(plan))
    try:
        import bqclient
        import worldset
        res = bqclient.run_guarded(sql, worldset.QUERY_CEILING_GB,
                                   label="limitation portfolio", log=log)
        rows = res[0] if isinstance(res, tuple) else list(res or [])
        if isinstance(res, tuple) and len(res) > 2:
            out["gb"] = float(res[2] or res[1] or 0.0)
    except Exception as e:
        traceback.print_exc()
        out["error"] = f"portfolio query failed: {str(e)[:200]}"
        return out

    by_era, pool = {}, {}
    for r in rows:
        lim_id = slugs.get(r["q"])
        if not lim_id:
            continue
        pool[lim_id] = int(r.get("pool_q") or 0)
        by_era.setdefault(lim_id, {}).setdefault(r.get("era") or "?", []).append({
            "pub": r["pub"], "fam": str(r.get("family_id") or r["pub"]),
            "title": (r.get("title") or "")[:300],
            "abstract": (r.get("abstract") or "")[:2000],
            "publication_date": r.get("pd"), "era": r.get("era"),
            "score": float(r.get("score") or 0.0), "n_cooc": int(r.get("n_cooc") or 0),
            "in_claims": bool(r.get("in_claims")), "in_title": bool(r.get("in_title")),
            "in_class": bool(r.get("in_class")),
            "for_limitation": lim_id, "acquired": "limitation_portfolio",
            "why": (plan.get(lim_id) or {}).get("why") or "",
        })

    #  PER LIMITATION, ROUND ROBIN ACROSS ERAS. Taking the top `per_limitation` of a limitation's
    #  rows by score would re-impose the single ranked list the era buckets exist to escape: the
    #  modern era is the biggest bucket and the highest-scoring one, so it would take the whole
    #  allowance and the 1960s art — which is what kills claims — would never be offered.
    by_lim = {}
    for lim_id, eras in by_era.items():
        buckets = [eras[name] for name, _lo, _hi in ERAS if eras.get(name)]
        picked, depth = [], 0
        while len(picked) < PER_LIMITATION:
            progressed = False
            for bucket in buckets:
                if depth >= len(bucket):
                    continue
                progressed = True
                picked.append(bucket[depth])
                if len(picked) >= PER_LIMITATION:
                    break
            if not progressed:
                break
            depth += 1
        by_lim[lim_id] = picked
    out["by_limitation"], out["pool"] = by_lim, pool

    #  ROUND ROBIN. A flat `list(found)[:N]` over a dict filled limitation by limitation gives the
    #  first limitation the entire budget and every later one nothing — and an empty row for
    #  claim 7 reads on the page as "no such art exists". Take one from each in turn instead.
    seen_fam, taken = set(), {k: 0 for k in by_lim}
    i = 0
    while len(out["candidates"]) < max_total:
        progressed = False
        for lim_id, rows_l in by_lim.items():
            if i >= len(rows_l):
                continue
            progressed = True
            row = rows_l[i]
            if row["fam"] in seen_fam:
                continue
            seen_fam.add(row["fam"])
            out["candidates"].append(row)
            taken[lim_id] += 1
            if len(out["candidates"]) >= max_total:
                break
        if not progressed:
            break
        i += 1
    for lim_id, rows_l in by_lim.items():
        if taken.get(lim_id, 0) < len(rows_l):
            log(f"[limq] {lim_id}: {pool.get(lim_id, 0):,} in the working set, "
                f"{len(rows_l)} ranked, {taken.get(lim_id, 0)} taken "
                f"(cap {max_total} reached)")
        else:
            log(f"[limq] {lim_id}: {pool.get(lim_id, 0):,} in the working set, "
                f"{taken.get(lim_id, 0)} taken across "
                f"{len({r['era'] for r in rows_l})} eras")
    log(f"[limq] {len(plan)} limitation portfolios in one scan "
        f"({out['gb']:.0f} GB) -> {len(out['candidates'])} candidates "
        f"across {len(by_lim)} limitations")
    if emit:
        emit("limq_done", n=len(out["candidates"]), limitations=len(by_lim))
    return out


# ---------------------------------------------------------------------------
# screening against the requirement, not against the invention
# ---------------------------------------------------------------------------
_SCREEN_SYS = (
    "You are a patent examiner screening candidate references against ONE CLAIM LIMITATION — a "
    "single technical requirement — not against a whole invention.\n"
    "\n"
    "Score each candidate 0-100 on one question: how likely is it that reading this document in "
    "full would produce a citable disclosure OF THAT REQUIREMENT. 90-100 = it plainly teaches "
    "this requirement; 70-89 = very likely teaches it; 40-69 = same mechanism, plausibly teaches "
    "it; 1-39 = related field, probably not this requirement; 0 = about something else.\n"
    "\n"
    "THIS IS WHERE A SCREENER GOES WRONG HERE, and it is the opposite of the usual mistake. The "
    "candidate does NOT have to resemble the invention the requirement came from. A muffler in a "
    "pneumatic drill handle, a sound absorber in a vacuum cleaner duct and an aircraft exhaust "
    "attenuator are all excellent answers to \"a sound-damping device in the exhaust air path\" "
    "even though none of them is a vacuum gripper. Score the REQUIREMENT. Penalising a document "
    "for being in another field is exactly how the art that invalidates a claim gets discarded.\n"
    "\n"
    "Many of these are old, foreign or machine-translated and all that exists is a title and a "
    "short abstract. A forty-word abstract cannot demonstrate a requirement in detail; judge what "
    "the document plainly IS. Old art is what kills claims — never score it down for being brief "
    "or archaic.\n"
    'Return ONLY JSON {"results":[{"id":<batch number>,"score":<0-100>}]} with one entry per '
    "candidate and every batch id exactly once."
)


def screen_and_select(found, plan, keep=KEEP_PER_LIMITATION, min_screen=MIN_SCREEN,
                      log=print, emit=None):
    """Screen each limitation's candidates against THAT limitation. -> [candidate] to acquire.

    The portfolio hands back a few thousand title-and-abstract rows; this is the cheap judgement
    that turns them into a read list. Screening is ~2,500 candidates in half a minute, so it can
    afford a pool the reader never could — which is the whole reason the pool is sized the way it
    is (see PER_ERA).

    Fail-soft: if the screener returns nothing for a limitation, its candidates keep their lexical
    order and the top `keep` are taken anyway. An unscored candidate is not evidence of a bad
    candidate.
    """
    by_lim = (found or {}).get("by_limitation") or {}
    if not by_lim:
        return []
    try:
        import deep_rank
    except Exception:
        traceback.print_exc()
        return [c for rows in by_lim.values() for c in rows[:keep]]

    out, per_lim = [], {}
    for lim_id, rows in by_lim.items():
        p = plan.get(lim_id) or {}
        text = p.get("text") or lim_id
        #  The screener's row shape: `title` and `text`. Abstract only — the full text is not in
        #  the corpus yet and fetching it before the screen is the circularity this whole design
        #  avoids paying twice.
        shaped = [{"pub": c["pub"], "title": c["title"], "text": (c.get("abstract") or "")[:1600]}
                  for c in rows]
        scores = {}
        try:
            scores = deep_rank.screen(
                shaped, f"{text}\n\n(from {p.get('claim_label') or lim_id}"
                        + (f"; {p['why']}" if p.get("why") else "") + ")",
                sys_prompt=_SCREEN_SYS, header="CLAIM LIMITATION TO FIND ART FOR") or {}
        except Exception:
            traceback.print_exc()
        for c in rows:
            c["screen"] = scores.get(c["pub"])
        ranked = sorted(rows, key=lambda c: (-(c["screen"] if c["screen"] is not None else -1),
                                             -c["score"]))
        chosen = [c for c in ranked if (c["screen"] is None or c["screen"] >= min_screen)][:keep]
        if not chosen:
            chosen = ranked[:keep]
        per_lim[lim_id] = (len(rows), len(chosen),
                           sum(1 for c in rows if (c["screen"] or 0) >= min_screen))
        out.extend(chosen)
        log(f"[limq] {lim_id}: screened {len(scores)}/{len(rows)}, "
            f"{per_lim[lim_id][2]} at or above {min_screen}, {len(chosen)} to read")
    #  Dedup by family across limitations, keeping the limitation that scored it highest, so one
    #  document is not read twice and its `for_limitation` says why it is here.
    best = {}
    for c in out:
        k = c["fam"]
        cur = best.get(k)
        if cur is None or (c.get("screen") or -1) > (cur.get("screen") or -1):
            best[k] = c
    sel = sorted(best.values(), key=lambda c: -(c.get("screen") if c.get("screen") is not None
                                                else -1))
    log(f"[limq] {len(sel)} distinct families selected to read across {len(per_lim)} limitations")
    if emit:
        emit("limq_screened", n=len(sel), limitations=len(per_lim))
    return sel
