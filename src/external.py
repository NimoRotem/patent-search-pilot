"""External art: many parallel keyword and semantic queries against the patent APIs.

WHY THIS EXISTS
---------------
Measured twice, blind, against two real examiner citation lists, the same failure showed up from
two opposite directions:

    EP 3 707 092 B1    most cited families ARE in this corpus. The job was to rank them.
    US 2026/0109053    four of the ten cited documents are classified in acoustics (G10K11/161),
                       exhaust silencers (F01N1/00), vacuum cleaners (A47L7, A47L9) and power
                       tools (B25F5/026). This corpus indexes eight CPC branches of vacuum
                       handling. Those four are not in it and never will be, so no amount of
                       ranking can produce them.

A federation already existed and did not help: it returned 40 hits and none of the four. Three
structural reasons, all fixed here.

1. IT RETURNED 45 FAMILIES, TOTAL.  App A's /api/search is an agentic loop that ends in an
   LLM-picked shortlist sized for its own results page. The pilot ranks several thousand local
   families, so 45 external ones are a rounding error however good they are, and the shortlist
   has already discarded the recall the call was made to buy. This module talks to
   /api/bulk_search instead: raw candidates, no planner, no shortlist, no ranking. Measured, two
   queries returned 172 candidates in 2.9 seconds against 40 families in 242.

2. IT ASKED ONE QUESTION, AND ASKED IT IN THE INVENTION'S OWN WORDS.  A single brief-shaped query
   describing the whole invention retrieves art that looks like the whole invention, which is
   precisely the art this corpus already holds. Remote art is reached by asking about the
   PROBLEM, in the other field's vocabulary: an examiner cites a lawn-mower muffler against a
   vacuum gripper because both attenuate noise in a moving air stream, and no query containing
   the words "vacuum gripper" will ever return it. `plan()` therefore decomposes the invention
   into product-neutral aspects and asks each one separately, in parallel, as BOTH a keyword
   query and a semantic blurb.

3. THE BIGGEST SOURCE WAS BLIND BY CONSTRUCTION.  The BigQuery adapter skips any sub-query that
   carries no CPC hint, and the planner fell back to the plan-level CPC, i.e. the invention's own
   classes. So the one source with hundreds of millions of rows was only ever asked about the
   field we already index. Each aspect here carries ITS OWN candidate CPC subclasses, proposed
   for the problem rather than for the product, which is what lets a query reach G10K or F01N.

AND THEN THEY HAVE TO BE JUDGED.  An external hit used to live in its own block on the report,
outside `ranked_families` -- the list every later stage consumes. It was never screened, never
read in full, never scored on evidence. It could only ever be listed, so it could never win.
`materialise()` writes each genuinely new publication into the corpus as tier 'external', so from
that moment it is an ordinary row: the screen sees it, the reader fetches its full text, the
claim chart grounds quotes in it, and the report renders it as a card.

Nothing in this module is specific to any subject patent. It is given a query set and returns
scored families.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback

import corpus_guard                # is this process allowed to write the live corpus at all
import db
import failclosed
import llm
import pubnorm

#  Where the raw fan-out runs. Same host and key as the federation client.
BASE_URL = os.environ.get("FEDERATION_BASE_URL", "https://rotem.ai/patents-engine").rstrip("/")
INTERNAL_URL = os.environ.get("FEDERATION_INTERNAL_URL", "http://10.128.0.13:8630").rstrip("/")
FED_KEY = os.environ.get("FEDERATION_KEY", "")
ENABLED = os.environ.get("EXTERNAL_ENABLED", "1") != "0"
TIMEOUT = float(os.environ.get("EXTERNAL_TIMEOUT", "120"))

#  How many aspects to decompose the invention into. Each becomes up to four queries.
MAX_ASPECTS = int(os.environ.get("EXTERNAL_MAX_ASPECTS", "9"))
#  Hard ceiling on the fan-out. App A caps at 80; stay under it.
MAX_QUERIES = int(os.environ.get("EXTERNAL_MAX_QUERIES", "78"))
#  SerpApi is paid and shared with the corpus enrichment job, so only the strongest aspects get
#  one. BigQuery is byte-capped but not per-call priced, and PQAI's mediator route is free.
SERPAPI_ASPECTS = int(os.environ.get("EXTERNAL_SERPAPI_ASPECTS", "4"))
#  CPC subclasses queried separately per aspect. Each is its own query with its own row budget.
CPC_PER_ASPECT = int(os.environ.get("EXTERNAL_CPC_PER_ASPECT", "4"))
#  How many external families may be spliced into the ranked list. Every stage below is a fixed
#  size, so this is not free: measured three times, widening a funnel whose next stage does not
#  widen LOWERS recall. This is a quota, not a dump.
MERGE_FAMILIES = int(os.environ.get("EXTERNAL_MERGE_FAMILIES", "400"))
#  RRF constant for fusing the per-aspect channels. Matches retrieval.RRF_K.
RRF_K = 40

_PLAN_SYS = (
    "You are a patent examiner planning a prior-art search. You will be given an invention.\n\n"
    "Examiners routinely cite art from FIELDS THE INVENTION HAS NOTHING TO DO WITH, because the "
    "same technical problem was solved there first. A muffler for a lawn mower is cited against a "
    "vacuum gripper; a hospital bed brake is cited against a camera tripod. Art like that is "
    "unreachable by any query that names the invention's own product, because that product word "
    "does not appear in it.\n\n"
    "So: break the invention into its separate TECHNICAL PROBLEMS, and for each one describe how "
    "it would be written up in OTHER fields.\n\n"
    "Return ONLY JSON:\n"
    '{"aspects":[{\n'
    '  "name":"<3-6 words>",\n'
    '  "problem":"<the generic technical problem, with NO product noun from the invention>",\n'
    '  "devices":["<3-6 NAMED KINDS OF PRODUCT, in any field, that have this problem, e.g. '
    'lawn mower, vacuum cleaner, air compressor, hair dryer>"],\n'
    '  "keywords":["<4-8 words that would literally appear in the TITLE of such a patent>"],\n'
    '  "cpc":["<3-5 four-character CPC subclasses where this problem is solved, e.g. G10K, '
    'F01N, A47L. Include the invention\'s own subclass at most ONCE across all aspects>"],\n'
    '  "blurb":"<2-3 sentences describing a device that solves this problem, product-neutral, '
    'for a semantic search engine>"}]}\n\n'
    "RULES.\n"
    "1. Emit 6 to 9 aspects.\n"
    "2. `keywords` ARE MATCHED AGAINST PATENT TITLES AND NOTHING ELSE. So they must be the "
    "concrete words a title actually uses -- muffler, silencer, blower, exhaust, cleaner, "
    "nozzle, coupling -- and never abstract phrases like 'acoustic damping', 'flow "
    "redirection' or 'component packaging', which appear in no title anywhere. Prefer single "
    "words. Include the product nouns you listed in `devices`.\n"
    "3. Never put the invention's own product name in a `problem` or a `blurb`. At least half "
    "the aspects must be phrased so that someone who had never heard of the invention would "
    "not guess what it is for.\n"
    "4. Prefer the plain physical mechanism (attenuating noise in an air stream, sealing "
    "against an uneven surface, sensing loss of pressure) over the application (gripping a "
    "workpiece)."
)


def _plan_llm(brief: str, claims_text: str) -> list:
    """Ask for the aspect decomposition. Fail-soft: [] on any error."""
    user = f"INVENTION\n{brief[:6000]}"
    if claims_text:
        user += f"\n\nCLAIMS\n{claims_text[:6000]}"
    out = llm.chat_json(_PLAN_SYS, user, max_tokens=2600) or {}
    aspects = []
    for a in (out.get("aspects") or [])[:MAX_ASPECTS]:
        if not isinstance(a, dict):
            continue
        kws = [str(k).strip() for k in (a.get("keywords") or []) if str(k).strip()][:8]
        cpc = []
        for c in (a.get("cpc") or [])[:5]:
            c4 = re.sub(r"[^A-Z0-9]", "", str(c).upper())[:4]
            if len(c4) == 4 and c4 not in cpc:
                cpc.append(c4)
        blurb = " ".join(str(a.get("blurb") or "").split())
        name = " ".join(str(a.get("name") or "").split())[:60]
        problem = " ".join(str(a.get("problem") or "").split())
        devices = [str(d).strip() for d in (a.get("devices") or []) if str(d).strip()][:6]
        #  Title matching is literal, so a multi-word keyword almost never hits. Split the
        #  phrases the model returns anyway into their content words and keep both forms.
        kws = _title_words(kws + devices)
        if not (kws or blurb):
            continue
        aspects.append({"name": name or (kws[0] if kws else "aspect"),
                        "problem": problem, "keywords": kws, "cpc": cpc,
                        "devices": devices, "blurb": blurb or problem})
    return aspects


#  Words that carry no discrimination in a patent title.
_TITLE_STOP = {
    "a", "an", "and", "the", "of", "for", "with", "in", "on", "to", "or", "by", "at", "from",
    "device", "apparatus", "system", "method", "assembly", "arrangement", "unit", "means",
    "having", "using", "same", "thereof", "such", "said", "its", "into", "onto", "via",
}


def _title_words(terms) -> list:
    """Content words suitable for a TITLE LIKE match, most specific first.

    BigQuery and USPTO ODP both match the title column only. A phrase like 'acoustic damping'
    matches no title as a whole string, and 'noise reduction device' matches almost none, so the
    phrases the planner returns are split into their content words and de-duplicated. Order is
    preserved because the adapters take only the first few.
    """
    out: list = []
    for t in terms:
        t = str(t or "").strip().lower()
        if not t:
            continue
        for w in re.split(r"[^a-z0-9\-]+", t):
            w = w.strip("-")
            if len(w) < 3 or w in _TITLE_STOP or w in out:
                continue
            out.append(w)
    return out[:10]


def plan(query_specs, brief: str = "", claims=None) -> dict:
    """The full search plan: aspects (LLM, product-neutral) + whole-invention queries.

    -> {"aspects": [...], "queries": [subquery dicts for /api/bulk_search]}

    The whole-invention queries are the deterministic floor: if the LLM is down the fan-out still
    happens, it is just narrower. Every aspect query is additive.
    """
    specs = list(query_specs or [])
    by_kind = {}
    for s in specs:
        by_kind.setdefault(getattr(s, "kind", ""), []).append(s)
    brief = brief or (by_kind.get("brief") or by_kind.get("essence") or [None])[0]
    if hasattr(brief, "text"):
        brief = brief.text
    brief = str(brief or "")

    claims_text = "\n".join(str(c.get("text") or "") for c in (claims or [])[:3])

    #  THE QUERY PLAN HAS TO BE FROZEN TOO, OR THE EXTERNAL CACHE CANNOT HIT.
    #  The bulk_search cache is keyed on the request payload, and the payload IS these queries.
    #  Decomposition is an LLM call, so two arms of one experiment would generate slightly
    #  different wording, miss the cache, and either fail the run (replay mode) or quietly fetch a
    #  different external world (record mode). Freezing the aspects makes the corpus the only
    #  thing that differs between arms, which is the entire point of cloning the database.
    import replay as _replay
    _pkey = {"brief": brief[:4000], "claims": claims_text[:4000],
             "max_aspects": MAX_ASPECTS, "cpc_per_aspect": CPC_PER_ASPECT}
    aspects = _replay.get("plan", _pkey)
    if aspects is None:
        if _replay.mode() == _replay.REPLAY:
            _replay.miss("plan", _pkey)
        try:
            aspects = _plan_llm(brief, claims_text)
        except Exception:
            traceback.print_exc()
            aspects = []
        _replay.put("plan", _pkey, aspects)

    queries: list[dict] = []

    def add(source, q, element, why, cpc=None):
        q = q if isinstance(q, str) else str(q)
        if len(q.strip()) < 6 or len(queries) >= MAX_QUERIES:
            return
        queries.append({"source": source, "q": q.strip()[:2000], "element": element,
                        "why": why, "cpc": list(cpc or [])})

    #  WHOLE INVENTION. PQAI is a semantic engine and does best on the raw disclosure; the
    #  keyword sources get the essence and the first independent claim.
    whole_cpc = sorted({c for a in aspects for c in a["cpc"]})[:8]
    if brief:
        add("pqai", brief, "whole invention", "raw brief, semantic")
    for s in specs:
        if getattr(s, "kind", "") == "essence":
            add("pqai", s.text, "whole invention", "essence, semantic")
            add("uspto", s.text, "whole invention", "essence, title keywords")
        elif getattr(s, "kind", "") == "claim":
            add("pqai", s.text, "whole invention", f"{s.name}, semantic")
            break
    if whole_cpc:
        add("bigquery_gpatents", brief[:400], "whole invention", "brief keywords in field",
            cpc=whole_cpc)

    #  PER ASPECT. This is the part that reaches outside the indexed field.
    for i, a in enumerate(aspects):
        kw = " ".join(a["keywords"][:8])
        if a["blurb"]:
            add("pqai", a["blurb"], a["name"], "aspect blurb, semantic")
        #  ONE QUERY PER CPC SUBCLASS, not one query listing all of them. The row limit is per
        #  query, so four subclasses in one query share one budget and the largest of them
        #  crowds out the rest -- which is how a document sitting in exactly the subclass we
        #  asked about never came back. Measured at ~10 MB billed per subclass query, so the
        #  extra queries cost about a tenth of a cent between them.
        for c4 in (a["cpc"] or [])[:CPC_PER_ASPECT]:
            if kw:
                add("bigquery_gpatents", kw, f"{a['name']} / {c4}",
                    "aspect keywords, one CPC subclass", cpc=[c4])
        if kw:
            add("uspto", kw, a["name"], "aspect keywords, US titles")
        if i < SERPAPI_ASPECTS and a["keywords"]:
            boolean = " ".join(f'"{k}"' if " " in k else k for k in a["keywords"][:4])
            add("serpapi_gpatents", boolean, a["name"], "aspect keywords, Google Patents")

    return {"aspects": aspects, "queries": queries}


#  A canary that every source can answer, used to prove a source is ALIVE rather than merely
#  reachable. Deliberately generic and in-scope for a mechanical corpus.
HEALTH_QUERY = "suction cup gripper vacuum"
HEALTH_CPC = ["B25J"]


def probe_sources(timeout: float = 45.0) -> dict:
    """Ask every source a REAL question. -> {source: {"ok", "hits", "error"}}.

    An HTTP health endpoint proves the service is up, not that a source works. Lens returned 401
    on every single search for the whole of this project while reporting healthy in /api/health,
    so one entire source contributed nothing and nothing noticed. A health check that does not
    execute a query cannot detect that.
    """
    qs = [{"source": s, "q": HEALTH_QUERY, "element": "health",
           "cpc": HEALTH_CPC if s == "bigquery_gpatents" else []}
          for s in ("pqai", "bigquery_gpatents", "serpapi_gpatents", "uspto",
                    "epo_ops", "lens", "openalex", "ipaustralia")]
    res = bulk(qs, timeout=timeout)
    stats = res.get("stats") or {}
    errs = {}
    for e in (res.get("errors") or []):
        errs.setdefault(e.get("source"), e.get("error"))
    out = {}
    for q in qs:
        s = q["source"]
        st = stats.get(s) or {}
        skipped = next((x for x in (res.get("skipped") or []) if x.get("source") == s), None)
        out[s] = {"ok": bool(st.get("hits")) and not st.get("errors") and not skipped,
                  "hits": st.get("hits", 0),
                  "error": errs.get(s) or (skipped or {}).get("why") or
                           ("no error, zero hits" if st.get("queries") and not st.get("hits")
                            else "")}
    return out


def bulk(queries, timeout: float = TIMEOUT) -> dict:
    """POST /api/bulk_search. Never raises: a failure returns an empty candidate list."""
    if not ENABLED or not queries:
        return {"ok": False, "candidates": [], "stats": {}, "error": "disabled or no queries"}
    import replay
    import requests
    headers = {"X-Patents-Key": FED_KEY} if FED_KEY else {}
    body = {"queries": queries[:MAX_QUERIES], "timeout": timeout}

    #  THE EXPERIMENT'S CONSTANT. Seven APIs whose rankings drift, whose quotas bite at different
    #  times of day, and one that has been 401ing for weeks. Two runs of one subject have differed
    #  by as much as any experimental effect we have produced, so a control-versus-treatment
    #  comparison is not interpretable until this is frozen. See src/replay.py.
    cached = replay.get("bulk_search", body)
    if cached is not None:
        cached = dict(cached)
        cached["replayed"] = True
        return cached
    if replay.mode() == replay.REPLAY:
        #  Raises in benchmark mode. A live call here would let one arm see a different outside
        #  world from the other while the report claims the corpus was the only change.
        replay.miss("bulk_search", body)

    last = ""
    #  PHASE 2b SEAM: the same fan-out, in-process (src/sources, the App A adapter port), so the
    #  external channel stops depending on a second web service entirely. Flag-gated OFF until
    #  the port branch (rebuild/sources-port) is merged and measured; any failure falls through
    #  to the HTTP path unchanged, and the result still lands in the replay cache so benchmark
    #  arms stay frozen.
    if os.environ.get("SOURCES_INPROC", "0") != "0":
        try:
            import sources as _sources
            #  sources.bulk() returns App A's exact /api/bulk_search envelope, by construction
            #  (see SOURCES_PORT.md), so everything downstream is unchanged.
            d = _sources.bulk(queries[:MAX_QUERIES], timeout=timeout)
            if isinstance(d, dict):
                d.setdefault("ok", True)
                d["base_url"] = "in-process"
                replay.put("bulk_search", body, d, raw="")
                return d
            last = "in-process sources: non-dict result"
        except Exception as e:
            last = f"in-process sources: {type(e).__name__}: {str(e)[:160]}"
    for base in [b for b in (INTERNAL_URL, BASE_URL) if b]:
        try:
            r = requests.post(f"{base}/api/bulk_search", json=body, headers=headers,
                              timeout=(10, timeout + 30))
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
                continue
            d = r.json()
            d["base_url"] = base
            replay.put("bulk_search", body, d, raw=r.text)
            return d
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:160]}"
    #  A source that could not be REACHED is not a source that found nothing. Both used to come
    #  back as an empty candidate list, so a benchmark run scored an unreachable fan-out exactly
    #  as it scored a fan-out that ran and returned no art.
    import failclosed
    failclosed.source_failed("bulk_search", last or "unreachable")
    return {"ok": False, "candidates": [], "stats": {}, "error": last or "unreachable"}


# --- turning raw candidates into corpus rows -------------------------------------------------
def _norm(pub) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(pub or "").upper())


def _canonical(pub) -> str:
    """The hyphenated corpus key for a publication number, e.g. US-2014008929-A1."""
    return pubnorm.canonical(pub) or ""


def _resolve_existing(cur, pubs) -> dict:
    """{normalised pub -> (id, family_key)} for the publications already in the corpus.

    Matched across EVERY spelling pubnorm knows, because the corpus stores US pre-grant numbers
    with a leading zero dropped and the external sources do not. A miss here is not cosmetic: it
    inserts a duplicate row for a publication we already hold, which then competes with its own
    twin in the ranking.
    """
    want = {}
    for p in pubs:
        for v in (pubnorm.variants(p) or [p]):
            want.setdefault(_norm(v), set()).add(_norm(p))
    if not want:
        return {}
    cur.execute(
        """SELECT id, publication_number,
                  COALESCE(NULLIF(simple_family_id,''), publication_number) fam,
                  upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) k
           FROM publications
           WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) = ANY(%s)""",
        (list(want),))
    out = {}
    for r in cur.fetchall():
        for original in want.get(r["k"], ()):
            #  first match wins; the query returns at most a handful of rows per key
            out.setdefault(original, (r["id"], r["fam"]))
    return out


_MAT_LOCK = threading.Lock()


def best_records(cands) -> dict:
    """{normalised pub -> the richest record any source returned for it}, junk numbers dropped."""
    by_pub: dict = {}
    for c in cands:
        pn = c.get("pub_number") or ""
        if not pn or not plausible(pn):
            continue
        k = _norm(pn)
        cur = by_pub.get(k)
        #  keep the richest record for a publication several sources returned
        if cur is None or len(str(c.get("abstract") or "")) > len(str(cur.get("abstract") or "")):
            by_pub[k] = c
    return by_pub


#  How many refused external candidates one search may record as demand. The fan-out reduces to a
#  few hundred survivors before it ever reaches `materialise`, so this is a ceiling on a pathology
#  rather than a routine cut, and it is bounded because each row is a write.
_DEMAND_QUEUE_MAX = int(os.environ.get("EXTERNAL_DEMAND_QUEUE_MAX", "400"))


def _queue_external_demand(records, tier="external"):
    """Record external candidates this process may not insert as demand for the next release.

    `corpus_ingest_queue` (sql/009) is where docs/corpus_write_policy.md sends search-time demand.
    A repeat request bumps `request_count`, so a publication four searches wanted outranks one
    that one search wanted when the release is built. Best effort: losing the signal must not
    cost the search the candidates it CAN still use.
    """
    try:
        import runctx
        import runstore
    except Exception:                                                # noqa: BLE001
        return 0
    run_id = getattr(runctx.active(), "run_id", None)
    n = 0
    for c in records[:_DEMAND_QUEUE_MAX]:
        pub = _canonical(c.get("pub_number"))
        if not pub:
            continue
        try:
            runstore.queue_for_ingest(
                pub, run_id=run_id,
                reason=f"external candidate ({tier}) found by a search; not in the corpus",
                source=str(c.get("source") or "external"),
                payload={"title": (c.get("title") or "")[:200],
                         "cpc": list(c.get("cpc") or [])[:12]})
            n += 1
        except Exception:                                            # noqa: BLE001
            traceback.print_exc()
            break
    if n:
        print(f"[external] {n} candidate(s) queued for the next corpus release instead of being "
              f"inserted here", flush=True)
    return n


def materialise(records, tier: str = "external") -> dict:
    """Insert the publications this corpus does not hold, so every later stage can judge them.

    `records` is {normalised pub -> record}, already REDUCED to the ones worth keeping. It is not
    the raw candidate list: a fan-out returns upwards of sixteen thousand candidates and writing
    all of them would put thousands of inserts on the request path to no purpose, since only the
    few hundred that survive fusion can ever be shown. Rank first, then materialise.

    -> {normalised pub -> (publication_id, family_key)} for every record, inserted or already held.

    This is the step that turns "an external API mentioned it" into "the pipeline read it". The
    row carries title, abstract, dates, country, kind and CPC, which is exactly what the screen
    reads; the full text is fetched later, on demand, by the same enrichment the local
    abstract-only records already go through.

    Nothing is embedded here. External candidates enter the ranking through their own retrieval
    channel (by rank, like every other channel), not through a vector search, so paying a Vertex
    round-trip on the request path would buy nothing.
    """
    if not records:
        return {}
    out: dict = {}
    with _MAT_LOCK, db.cursor() as cur:
        have = _resolve_existing(cur, [c["pub_number"] for c in records.values()])
        out.update(have)
        todo = [c for k, c in records.items() if k not in have]
        if todo and corpus_guard.armed() and not corpus_guard.writes_allowed():
            #  THE CORPUS IS READ ONLY IN THIS PROCESS. Every insert below would be refused one
            #  row at a time by the guard, and the per-row SAVEPOINT handler would swallow each
            #  refusal, so the channel would come back short with nothing anywhere saying why.
            #  Decided once, said once, and the demand is recorded where the policy puts it.
            _queue_external_demand(todo, tier)
            failclosed.fallback(
                "external:materialise",
                f"the corpus is read only in this process, so {len(todo)} external candidate(s) "
                f"this search found could not be added to it; they are queued for the next "
                f"corpus release and are not in this run's ranking",
                kind="corpus_read_only")
            return out
        for c in todo:
            pub = _canonical(c["pub_number"])
            if not pub:
                continue
            parsed = pubnorm.parse(c["pub_number"])
            country = (parsed[0] if parsed else "") or (c.get("country") or "")[:2].upper()
            kind = (parsed[2] if parsed else "") or (c.get("kind") or "")
            fam = str(c.get("family_id") or "").strip() or pub
            #  SAVEPOINT PER ROW. Without one, a single rejected insert aborts the whole
            #  transaction and every remaining insert fails with InFailedSqlTransaction -- so
            #  one malformed record from one source silently cost the entire fan-out.
            try:
                cur.execute("SAVEPOINT ins")
                cur.execute(
                    """INSERT INTO publications
                         (publication_number, kind_code, country, publication_date,
                          earliest_priority_date, simple_family_id, title, abstract, tier)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (publication_number, kind_code) DO NOTHING
                       RETURNING id""",
                    (pub, kind or "", country or None,
                     _date(c.get("date")), _date(c.get("priority_date")),
                     fam, (c.get("title") or "")[:2000],
                     (c.get("abstract") or c.get("snippet") or "")[:20000], tier))
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT ins")
            except Exception:
                traceback.print_exc()
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT ins")
                except Exception:
                    pass
                continue
            if not row:
                continue
            pid = row["id"]
            syms = []
            for s in (c.get("cpc") or [])[:12]:
                s = str(s).strip().upper()
                if s and s not in syms:
                    syms.append(s)
            if syms:
                try:
                    cur.execute("SAVEPOINT cls")
                    cur.executemany(
                        "INSERT INTO classifications (publication_id, scheme, symbol, is_first) "
                        "VALUES (%s,'CPC',%s,%s)",
                        [(pid, s, i == 0) for i, s in enumerate(syms)])
                    cur.execute("RELEASE SAVEPOINT cls")
                except Exception:
                    traceback.print_exc()
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT cls")
                    except Exception:
                        pass
            out[_norm(c["pub_number"])] = (pid, fam)
    return out


#  US utility grants are at roughly 12.4 million as of 2026 and advance ~350k a year. Anything far
#  above that is not a grant number. The USPTO adapter falls back to `US<applicationNumber>` when a
#  record carries neither a publication number nor a patent number, which yields ids like
#  US35530491 -- a real application, not a publication. Those match nothing anywhere, and without
#  this they would be INSERTED into the corpus as permanent junk rows with a title and no document
#  behind them.
_US_GRANT_CEILING = int(os.environ.get("EXTERNAL_US_GRANT_CEILING", "14000000"))


def plausible(pub) -> bool:
    """Is this a publication number a corpus could hold, rather than an application number?"""
    p = pubnorm.parse(pub)
    if not p:
        return False
    cc, num, _kind = p
    if len(num) < 4 or len(num) > 13:
        return False
    if cc == "US" and len(num) <= 9 and not num.startswith(("19", "20")):
        try:
            return int(num) <= _US_GRANT_CEILING
        except ValueError:
            return False
    return True


_DATE_RE = re.compile(r"(\d{4})\D?(\d{2})\D?(\d{2})")


def _date(v):
    """A date the corpus will accept, or None. Sources spell it every possible way."""
    m = _DATE_RE.search(str(v or ""))
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        if not (1790 <= int(y) <= 2100 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
            return None
    except ValueError:
        return None
    return f"{y}-{mo}-{d}"


# --- ranking ---------------------------------------------------------------------------------
def family_keys(cur, records) -> dict:
    """{normalised pub -> family key} for ranking, WITHOUT writing anything.

    A candidate the corpus already holds must rank under its LOCAL family key, so cross-system
    agreement collapses onto the row the local channels also found instead of competing with it.
    Everything else ranks under the family id the source gave, or its own number.
    """
    have = _resolve_existing(cur, [c["pub_number"] for c in records.values()])
    out = {}
    for k, c in records.items():
        if k in have:
            out[k] = have[k][1]
        else:
            out[k] = str(c.get("family_id") or "").strip() or _canonical(c["pub_number"]) or k
    return out, have


#  How deep a single query's result list may contribute to the fusion. Uncapped, RRF rewards
#  BREADTH over strength: a document sitting at rank 250 in ten channels out-scores one at rank 3
#  in one, because ten times 1/290 beats 1/43. That is exactly backwards here. The tail of a
#  title-keyword channel is not a weak signal, it is NO signal -- BigQuery orders by how many
#  query terms the title matched and breaks ties with RAND(), so past the first hundred rows the
#  order is literally random. Capping makes a strong single-channel hit win, which is what
#  finding art in a remote field looks like.
CHANNEL_DEPTH = int(os.environ.get("EXTERNAL_CHANNEL_DEPTH", "100"))

#  Per-source weight, mirroring the weighted RRF the local channels already use. A semantic
#  engine's hit and a "this word was in the title" hit are not equally good evidence.
SOURCE_WEIGHT = {
    "pqai": 1.0,                 # semantic over the whole disclosure
    "serpapi_gpatents": 1.0,     # Google's own relevance ranking
    "uspto": 0.7,                # US titles only, but ordered by ODP relevance
    "bigquery_gpatents": 0.5,    # title substring match, ranked only by how many terms hit
}


def channels(cands, fam_of) -> dict:
    """One retrieval channel PER QUERY, keyed by family.

    Not one channel for everything: RRF consumes rank order, so pooling every source's hits into a
    single list buries each aspect's own best find under the whole fan-out. A channel per query is
    what makes a document that only ONE aspect found still competitive, which is the entire point
    of asking nine separate questions.
    """
    chans: dict = {}
    for c in cands:
        fam = fam_of.get(_norm(c.get("pub_number") or ""))
        if not fam:
            continue
        key = f"ext:{c.get('query_i')}:{c.get('source')}"
        chans.setdefault(key, []).append((fam, c.get("source_rank") or 999))
    out = {}
    for k, rows in chans.items():
        rows.sort(key=lambda r: r[1])
        seen, chan = set(), []
        for fam, _rank in rows:
            if fam in seen:
                continue
            seen.add(fam)
            chan.append(fam)
            if len(chan) >= CHANNEL_DEPTH:
                break
        out[k] = chan
    return out


#  Breadth is worth something, but it is capped. See fuse_families.
BREADTH_BONUS = float(os.environ.get("EXTERNAL_BREADTH_BONUS", "0.30"))


def fuse_families(chans, limit=MERGE_FAMILIES) -> list:
    """Weighted rank fusion, scored PER SOURCE rather than per query.

    -> [(family_key, score)] best first.

    Plain RRF assumes the channels are independent evidence. These are not: thirty-odd BigQuery
    title queries with overlapping keyword sets are one source asked thirty ways, and summing them
    counts the same weak signal thirty times. A generic document that matches a common word in
    many aspects then outscores the remote-field document that exactly one aspect was written to
    find -- which is the document the whole fan-out exists to retrieve.

    So each SOURCE contributes once, at its best rank for that family, and genuine cross-source
    agreement (a semantic engine and Google both returned it) still adds up. Breadth across
    aspects is kept as a bounded bonus rather than an unbounded sum, so it can break ties without
    ever overturning a strong hit.
    """
    best: dict = {}          # fam -> {source: best rank}
    aspects: dict = {}       # fam -> set of query indices
    for key, chan in chans.items():
        _, qi, src = key.split(":", 2)
        for rank, fam in enumerate(chan, 1):
            per = best.setdefault(fam, {})
            if rank < per.get(src, 10 ** 9):
                per[src] = rank
            aspects.setdefault(fam, set()).add(qi)

    n_queries = max(1, len({k.split(":", 2)[1] for k in chans}))
    scores = {}
    for fam, per in best.items():
        s = sum(SOURCE_WEIGHT.get(src, 0.7) / (RRF_K + rank) for src, rank in per.items())
        s *= 1.0 + BREADTH_BONUS * (len(aspects[fam]) / n_queries)
        scores[fam] = s
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(fam, round(sc, 6)) for fam, sc in ordered[:limit]]


#  Families semantically scored before the merge quota is applied. Everything above this is
#  ordered by the fan-out alone; below it, by how much the document actually resembles the
#  invention. Embedding is ~200 texts per round trip, so this is a handful of calls.
RESCORE_TOP = int(os.environ.get("EXTERNAL_RESCORE_TOP", "1500"))
RESCORE_WORKERS = int(os.environ.get("EXTERNAL_RESCORE_WORKERS", "8"))
RESCORE_BATCH = 200
#  Share of the final order that comes from semantic similarity rather than the fan-out's own
#  rank fusion. Both matter: fusion says "several independent queries reached this", similarity
#  says "this is about the same thing".
SEMANTIC_SHARE = float(os.environ.get("EXTERNAL_SEMANTIC_SHARE", "0.6"))


def rescore(ranked, records, fam_of, brief: str) -> list:
    """Re-order the fused families by how much each one actually resembles the invention.

    Until this stage an external candidate is ranked purely on WHICH QUERY FOUND IT AND WHERE --
    it has never been compared to the invention at all. That is a real gap: a title-keyword
    channel returns four hundred documents that share a word, and sharing a word with an aspect
    of the problem is not the same as being relevant to the invention. The local corpus channels
    have always had this comparison (it is what the dense channel IS); the external ones did not.

    Cheap on purpose: one embedding per candidate over title + abstract, cosine against the brief,
    blended with the fusion score by rank so neither can dominate. No cross-encoder, no LLM.

    Fail-soft: any error returns the input order, because a fan-out ranked only by fusion is still
    far better than no fan-out.
    """
    if not ranked or not brief.strip():
        return ranked
    head = ranked[:RESCORE_TOP]
    tail = ranked[RESCORE_TOP:]
    #  one representative record per family, preferring the one with the most text
    rep: dict = {}
    for k, c in records.items():
        fam = fam_of.get(k)
        if fam is None:
            continue
        cur = rep.get(fam)
        if cur is None or len(_doc_text(c)) > len(_doc_text(cur)):
            rep[fam] = c
    pairs = [(f, _doc_text(rep[f])) for f, _ in head if f in rep and _doc_text(rep[f])]
    if not pairs:
        return ranked

    try:
        import embed
        from concurrent.futures import ThreadPoolExecutor
        qv = embed.embed_query(brief[:8000])
        batches = [pairs[i:i + RESCORE_BATCH] for i in range(0, len(pairs), RESCORE_BATCH)]

        def one(batch):
            return embed.embed_texts([t for _, t in batch], len(qv),
                                     task_type="RETRIEVAL_DOCUMENT")

        with ThreadPoolExecutor(max_workers=RESCORE_WORKERS) as ex:
            vecs = list(ex.map(one, batches))
    except Exception:
        traceback.print_exc()
        return ranked

    sim: dict = {}
    qn = sum(x * x for x in qv) ** 0.5 or 1.0
    for batch, vs in zip(batches, vecs):
        if not vs or len(vs) != len(batch):
            continue
        for (fam, _), v in zip(batch, vs):
            vn = sum(x * x for x in v) ** 0.5 or 1.0
            sim[fam] = sum(a * b for a, b in zip(qv, v)) / (qn * vn)

    if not sim:
        return ranked
    #  Blend by RANK, not by raw value: a cosine and an RRF score are on different scales, and
    #  rank blending needs no calibration to stay stable as either distribution shifts.
    fus_rank = {f: i for i, (f, _) in enumerate(head)}
    sem_rank = {f: i for i, f in enumerate(sorted(sim, key=lambda f: -sim[f]))}
    unscored = len(sem_rank)

    def key(item):
        f, _ = item
        return (SEMANTIC_SHARE * sem_rank.get(f, unscored)
                + (1 - SEMANTIC_SHARE) * fus_rank[f])

    return sorted(head, key=key) + tail


def _doc_text(c) -> str:
    t = " ".join(str(c.get("title") or "").split())
    a = " ".join(str(c.get("abstract") or c.get("snippet") or "").split())
    return f"{t}. {a}".strip(". ")[:2000]


def subject_from_doc(pub) -> object:
    """Best-effort Subject for a document that was UPLOADED or LINKED rather than typed.

    A link/upload search passes subject=None, so it runs with NO date cutoff at all -- the
    pipeline will happily rank a document published after the invention as prior art against it.
    That was survivable while every candidate came from a corpus of mostly older art. It is not
    survivable now: the external sources skew heavily recent, so an unfiltered fan-out injects
    art that postdates the subject straight into the ranked list.

    Local row first (free, exact), then App A's merged record. None when no date can be
    established, which leaves the previous no-cutoff behaviour rather than inventing one.
    """
    if not pub:
        return None
    from datetime import date as _date_t
    from search_modes import Subject

    def _mk(efd, filing=None, pubd=None, cc=None, number=None):
        if not efd:
            return None
        #  `number` matters as much as the date: retrieval._date_clause uses it to exclude the
        #  subject's OWN family from every channel. Without it the search returns the invention
        #  as its own closest prior art (measured: rank 1 of its own results) and, worse, the
        #  citation-expansion channel expands that family's backward citations into the candidate
        #  pool, which is the answer key.
        return Subject(number=number, efd=efd, filing_date=filing, publication_date=pubd,
                       jurisdiction=cc)

    def _parse(v):
        s = _date(v)
        if not s:
            return None
        y, m, d = s.split("-")
        try:
            return _date_t(int(y), int(m), int(d))
        except ValueError:
            return None

    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT publication_number, earliest_priority_date, filing_date,
                          publication_date, country FROM publications
                   WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g'))
                         = ANY(%s) LIMIT 1""",
                ([_norm(v) for v in (pubnorm.variants(pub) or [pub])],))
            r = cur.fetchone()
        if r:
            s = _mk(r["earliest_priority_date"] or r["filing_date"] or r["publication_date"],
                    r["filing_date"], r["publication_date"], r["country"],
                    number=r["publication_number"])
            if s:
                return s
    except Exception:
        traceback.print_exc()

    try:
        import federation
        d = federation.patent_detail(pub) or {}
        efd = _parse(d.get("priority_date")) or _parse(d.get("filing_date")) \
            or _parse(d.get("date")) or _parse(d.get("publication_date"))
        return _mk(efd, _parse(d.get("filing_date")), _parse(d.get("publication_date")),
                   (d.get("country") or "")[:2].upper() or None)
    except Exception:
        traceback.print_exc()
        return None


def citable(families, subject_obj, mode) -> list:
    """Drop external families that are not prior art under `mode` against `subject_obj`.

    The local channels are date-filtered inside their own SQL (retrieval._date_clause). External
    families arrive AFTER retrieval and are spliced straight into the ranked list, so without this
    they skip that filter entirely -- and a document published after the subject's effective
    filing date is not prior art, whatever a search engine thinks of it. Same rule, same function
    (search_modes.citable_where), so there is one definition of citability in the codebase.

    No subject or no mode means no cutoff is known, which is the existing behaviour for an
    uploaded document that names no publication; the families pass through unchanged.
    """
    if not families or subject_obj is None or mode is None:
        return list(families or [])
    from search_modes import Mode, citable_where
    try:
        m = Mode(mode) if isinstance(mode, str) else mode
        frag, params = citable_where(m, subject_obj, "p")
    except Exception:
        return list(families)          # an unsupported mode must not silently drop everything
    pids = [pid for _, _, pid in families]
    try:
        with db.cursor() as cur:
            cur.execute(f"SELECT id FROM publications p WHERE p.id = ANY(%s) AND {frag}",
                        [pids] + list(params))
            ok = {r["id"] for r in cur.fetchall()}
    except Exception:
        traceback.print_exc()
        return list(families)
    return [f for f in families if f[2] in ok]


def credit_sources(cands, fam_of, kept):
    """Which source put which family in front of the reader. -> ({src: n}, {src: n_unique})

    `stats[src]["hits"]` counts what an adapter RETURNED, which is the wrong unit and flatters the
    noisiest one: measured on a live run, bigquery_gpatents returned 9,979 rows and
    serpapi_gpatents 400, out of 12,480 candidates that fused down to 393 families. What a reader
    is entitled to know is how many families a source contributed to the ranking, and how many of
    those NO OTHER source found, because that second number is the one that says whether a
    subscription is earning its place.

    Counted over `kept`, the families that survived fusion, so a source is never credited with a
    document that was cut. A family two sources both found is credited to both and is unique to
    neither. Its own function so it can be tested without a network fan-out.
    """
    by_source: dict = {}
    for c in cands or ():
        fam = fam_of.get(_norm((c or {}).get("pub_number") or ""))
        if fam in kept:
            by_source.setdefault(str((c or {}).get("source") or "external"), set()).add(fam)
    finders: dict = {}
    for src, fams in by_source.items():
        for fam in fams:
            finders.setdefault(fam, set()).add(src)
    return ({s: len(f) for s, f in by_source.items()},
            {s: sum(1 for fam in fams if len(finders.get(fam) or ()) == 1)
             for s, fams in by_source.items()})


def run(query_specs, brief: str = "", claims=None, on_event=None) -> dict:
    """Plan, fan out, materialise, rank. Never raises.

    -> {"ok", "families": [(fam, score, pid)], "aspects", "queries", "stats", "n_candidates",
        "n_new", "elapsed", "error"}
    """
    t0 = time.time()
    if not ENABLED:
        return {"ok": False, "families": [], "error": "external search disabled",
                "aspects": [], "queries": [], "stats": {}, "n_candidates": 0, "n_new": 0,
                "elapsed": 0.0}
    try:
        p = plan(query_specs, brief=brief, claims=claims)
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "families": [], "error": f"plan failed: {str(e)[:200]}",
                "aspects": [], "queries": [], "stats": {}, "n_candidates": 0, "n_new": 0,
                "elapsed": round(time.time() - t0, 1)}
    if on_event:
        try:
            on_event("plan", {"aspects": len(p["aspects"]), "queries": len(p["queries"])})
        except Exception:
            pass

    res = bulk(p["queries"])
    cands = res.get("candidates") or []
    #  Per-source outcome, recorded so "zero hits" and "the adapter 401d" are never the same fact.
    import failclosed
    for src, st in (res.get("stats") or {}).items():
        if st.get("errors"):
            failclosed.source_failed(src, f"{st['errors']} of {st.get('queries')} queries failed")
        elif not st.get("hits"):
            failclosed.empty_result(src)
    for e in (res.get("errors") or []):
        failclosed.source_failed(e.get("source") or "?", e.get("error") or "")
    if on_event:
        try:
            on_event("fanout", {"candidates": len(cands), "stats": res.get("stats") or {}})
        except Exception:
            pass

    #  RANK FIRST, WRITE SECOND. A fan-out returns tens of thousands of candidates and only the
    #  few hundred that survive fusion can ever be shown, so resolving is a read over all of them
    #  and inserting is a write over the survivors.
    try:
        records = best_records(cands)
        with db.cursor() as cur:
            fam_of, have = family_keys(cur, records)
        chans = channels(cands, fam_of)
        #  Fuse DEEPER than the merge quota, then let the semantic pass decide which of those
        #  actually resemble the invention. Cutting to the quota first would throw away the
        #  candidates the comparison exists to rescue.
        ranked = fuse_families(chans, limit=max(RESCORE_TOP, MERGE_FAMILIES))
        ranked = rescore(ranked, records, fam_of, brief)[:MERGE_FAMILIES]

        keep_fams = {f for f, _ in ranked}
        winners = {k: c for k, c in records.items()
                   if fam_of.get(k) in keep_fams and k not in have}
        placed = dict(have)
        placed.update(materialise(winners))
        n_new = len(placed) - len(have)

        pid_of: dict = {}
        for k, c in records.items():
            fam = fam_of.get(k)
            if fam in keep_fams and fam not in pid_of and k in placed:
                pid_of[fam] = placed[k][0]
        fams = [(f, s, pid_of[f]) for f, s in ranked if f in pid_of]
        families_by_source, unique_by_source = credit_sources(cands, fam_of, pid_of)
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "families": [], "error": f"ranking failed: {str(e)[:200]}",
                "aspects": p["aspects"], "queries": p["queries"],
                "stats": res.get("stats") or {}, "n_candidates": len(cands), "n_new": 0,
                "elapsed": round(time.time() - t0, 1)}

    return {"ok": bool(res.get("ok")), "families": fams,
            "aspects": p["aspects"], "queries": p["queries"],
            "stats": res.get("stats") or {}, "errors": res.get("errors") or [],
            "n_candidates": len(cands), "n_records": len(records), "n_in_corpus": len(have),
            "n_new": n_new, "n_channels": len(chans),
            "families_by_source": families_by_source,
            "unique_families_by_source": unique_by_source,
            "n_families": len(fams), "elapsed": round(time.time() - t0, 1),
            "error": res.get("error") or ""}


def summary(ext: dict) -> dict:
    """A small, display-ready block for the report page."""
    if not ext:
        return {}
    per_source = {k: v.get("hits", 0) for k, v in (ext.get("stats") or {}).items()}
    return {
        "ok": bool(ext.get("ok")),
        #  Families kept, per source, and how many of them nothing else found. `per_source` below
        #  is the raw returned-row count and is a different unit: keep both, never conflate them.
        "families_by_source": ext.get("families_by_source") or {},
        "unique_families_by_source": ext.get("unique_families_by_source") or {},
        "aspects": [{"name": a.get("name"), "cpc": a.get("cpc"),
                     "keywords": a.get("keywords", [])[:6]}
                    for a in (ext.get("aspects") or [])],
        "n_queries": len(ext.get("queries") or []),
        "n_candidates": ext.get("n_candidates", 0),
        "n_new": ext.get("n_new", 0),
        "n_in_corpus": ext.get("n_in_corpus", 0),
        "n_families": ext.get("n_families", 0),
        "per_source": per_source,
        "elapsed": ext.get("elapsed", 0.0),
        "error": ext.get("error") or "",
    }
