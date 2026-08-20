"""After the main search has finished: go back for the claims nothing was found for.

WHY THIS EXISTS
---------------
A prior-art search that starts from a patent is answering one question per claim: does a document
exist that discloses it. The main loop never asks that question. It retrieves against the invention
AS A WHOLE, screens the candidates as a whole, reads them as a whole, and the claim table falls out
of the reading as a by-product. So a claim whose subject matter sits a little outside the
invention's own neighbourhood — the mounting bracket, the charging circuit, the alarm buzzer — gets
no reference at all, and the report says "nothing discloses this" when the truth is that nothing was
ever searched for it. Those two are indistinguishable on the page and they are opposite findings.

This module separates them. It reads the finished chart, takes the claims with little or no
evidence, and runs a second, narrower search that is about THOSE CLAIMS and nothing else:

  * the claim is restated as the idea it is, in the words other patents would use, WITH the
    independent claim it depends from and the invention's own description as context — a dependent
    claim read alone ("the blower of claim 1, wherein the baffle is curved") is not a searchable
    thought;
  * the queries run with NO classification channel. The main search leans on CPC and on citation
    and query-by-example expansion, all three of which pull back toward the neighbourhood the
    invention is already in. An orphaned claim is orphaned precisely because its art is somewhere
    else, so this pass is pure text: dense over the whole document, dense over claims, and lexical
    over both;
  * every reference already screened is excluded, so this can only ADD;
  * what comes back is read in full against the same checklist as everything else, so a rescued
    reference is comparable to the rest of the report rather than parked in a separate list;
  * and the references the search ALREADY read are asked again about the orphaned claims alone,
    with the claim's concept in hand. That is usually where the cheapest wins are: the document is
    already in front of us and a first reading that had forty-eight features and thirty claims to
    answer in one breath is measurably conservative about each of them.

PARTIAL MATCHES ARE THE POINT. A claim with one "partial" from a 1974 utility model in another
class is a far better answer than an empty row, and it is what an examiner would cite. The verdicts
are not relaxed to get them — every cell still passes the same grounding, location and refutation
gates — but the SEARCH deliberately goes wider than the main loop is allowed to.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import deep_analysis
import llm

#  A claim is an ORPHAN when this many or fewer references ground it. 1, not 0: a single match is
#  one document away from being no match at all, and a claim standing on one reference is exactly
#  the one a searcher needs a second opinion about.
ORPHAN_MAX_MATCHES = int(os.environ.get("RESCUE_ORPHAN_MAX", "1"))
#  How many orphaned claims get their own search. Independent claims first, then the emptiest.
MAX_CLAIMS = int(os.environ.get("RESCUE_MAX_CLAIMS", "10"))
QUERIES_PER_CLAIM = int(os.environ.get("RESCUE_QUERIES", "3"))
#  New candidates read per orphaned claim, and the ceiling over all of them. Every one of these is
#  a full-text read, so this is the cost knob.
PER_CLAIM = int(os.environ.get("RESCUE_PER_CLAIM", "6"))
MAX_NEW = int(os.environ.get("RESCUE_MAX_NEW", "48"))
#  How far down the already-read order to re-ask about the orphaned claims. One model call each,
#  and the head is what the page shows and what an argument is built from.
REREAD_TOP = int(os.environ.get("RESCUE_REREAD_TOP", "150"))
#  Concurrent ANN passes. Each forks its own PostgreSQL connection; the box has four cores and the
#  main search is already finished by the time this runs.
SEARCH_WORKERS = int(os.environ.get("RESCUE_SEARCH_WORKERS", "4"))
#  RAISED 12 -> 24, to match `deep_rank.CHART_WORKERS`. These workers do not spend the box's cores,
#  they wait on model round-trips, and the real ceiling is the per-provider semaphore in model_pool
#  (48 for Vertex, 24 for Anthropic) which was measured not to throttle at 24 concurrent calls.
#
#  MEASURED, and the gap is the reason this number is here at all: on adhoc-db64a3dd7c98 the main
#  read sustained 9.3 model calls/s at CHART_WORKERS=24, while the rescue — the same work, the same
#  providers, the same box, minutes later — managed 0.44 calls/s at 12. The rescue was 79% of a
#  2h21m search. Half of that pool was simply never asked for.
READ_WORKERS = int(os.environ.get("RESCUE_READ_WORKERS", "24"))
#  Families pulled per query before dedup against what was already screened.
SEARCH_TOPK = int(os.environ.get("RESCUE_SEARCH_TOPK", "120"))
#  Below this many candidates already acquired WITH TEXT, fall back to the text-less App A route.
#  See where it is used: it buys unreadable rows, so it is a last resort rather than a supplement.
CONCEPT_FALLBACK_BELOW = int(os.environ.get("RESCUE_CONCEPT_BELOW", "12"))
#  LOCAL FIRST. When the local corpus has already produced this many candidates for the orphaned
#  limitations, the BigQuery acquisition below is skipped entirely.
#
#  MEASURED, and it is the clearest waste in the pipeline. The local corpus holds 4,975,809
#  publications with 8.5M claims, 13.3M description paragraphs and 53.5M CPC classifications,
#  current to within days. On adhoc-5972e6042dfa the local pass returned 48 candidates and the
#  pipeline then spent $10.50 and 18 minutes building a 6,227,398-row BigQuery working set whose
#  entire yield was 150 families read, 123 new rows, 63 with usable text. On the run before it,
#  1,590,484 of that working set's 5,828,904 rows (27%) were ALREADY in the local corpus.
#
#  The BigQuery route is not wrong: the corpus is seeded from one field's CPC branches and cannot
#  reach art outside them, which is a real measured failure. It is just not worth paying for when
#  what we already own has answered. RESCUE_LOCAL_ENOUGH=0 always acquires.
LOCAL_ENOUGH = int(os.environ.get("RESCUE_LOCAL_ENOUGH", "40"))
#  NO CPC, NO CITATION, NO QBE. See the module docstring: all three pull back toward the
#  neighbourhood the main search already covered, and an orphaned claim is orphaned because its art
#  is not there.
CHANNELS = ["dense", "claim_dense", "bm25", "claim_bm25"]

_MATCH_VERDICTS = ("disclosed", "partial")


def second_look(ref, labels, hints, texts):
    """Ask ONE reference the narrow question about a few claims. -> cells changed.

    WHY A SECOND PASS IS NEEDED EVEN ON A DOCUMENT WE JUST READ FOR THIS.

    Measured on adhoc-584455f78ae2: the rescue searched for three orphaned claims, found 17
    unread references in the right technical area, read all 17 in full — and grounded ONE claim
    cell out of 221, while grounding 25 feature cells in the same reading. That is not a broken
    reader, it is the arithmetic of a dependent claim: "the gripper of claim 1, wherein X" is a
    CONJUNCTION, so a document that teaches X and nothing else is correctly "absent" for the whole
    claim. Searching for the thing a claim ADDS therefore retrieves documents that must then fail
    the whole-claim test.

    So the reference is asked again with the prompt written for exactly this
    (deep_analysis._REREAD_CLAIM_SYS): what does it teach of the idea this claim adds, and a
    partial is a real and wanted answer. Same grounding, location and refutation gates — the bar
    on what may be ASSERTED does not move, only what the reader is asked to look for.
    """
    try:
        rows = [r for r in (ref.get("claims") or []) if r.get("item") in labels]
        if not rows:
            return 0
        n = deep_analysis.reread_absent(ref["pub"], rows, hints=hints, kind="claim", texts=texts)
        if n:
            #  Hand the refuter the CLAIM TEXT: given a bare "claim 7" it answers that the
            #  assertion is not one that can be refuted and downgrades every row (see _refute).
            deep_analysis._refute([r for r in rows if r.get("second_pass")], ref["pub"], texts)
            ref["counts"] = deep_analysis._counts(
                (ref.get("features") or []) + (ref.get("claims") or []))
        return n
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# which claims the search failed
# ---------------------------------------------------------------------------
def claim_matches(charts, labels):
    """{claim label: how many read references ground it}, counting only real evidence.

    "Ground" means the same thing here as everywhere else in the pipeline: a verbatim quote that
    was found in the reference and located in a specific passage. An "uncertain" — a "disclosed"
    an independent refuter would not confirm — is counted separately rather than as a match,
    because a claim whose only support is a cell the refuter rejected is not covered.
    """
    got = {l: 0 for l in labels}
    weak = {l: 0 for l in labels}
    for ref in charts:
        if ref.get("method") != "llm":
            continue
        for row in (ref.get("claims") or []):
            if row.get("grounding") not in ("verified", "teaches-unquoted"):
                continue
            item = str(row.get("item") or "")
            if item not in got:
                continue
            #  A teaches-bar cell is WEAK support: it must not satisfy an orphaned limitation on
            #  its own, or the rescue would stop searching for verbatim art the moment a reader
            #  asserted an unquotable teaching.
            if row.get("grounding") == "teaches-unquoted":
                weak[item] += 1
            elif row.get("verdict") in _MATCH_VERDICTS:
                got[item] += 1
            elif row.get("verdict") == "uncertain":
                weak[item] += 1
    return got, weak


def orphans(charts, claim_items, max_matches=ORPHAN_MAX_MATCHES, limit=MAX_CLAIMS):
    """The claims worth a second search, worst first. -> ([claim dict], {label: n_matches}).

    Ordering is deliberate. An INDEPENDENT claim with no art is the most serious hole in a search —
    it is the claim the whole patent stands on — so it outranks a dependent with the same count.
    Below that, emptiest first.
    """
    labels = [c["label"] for c in claim_items]
    got, weak = claim_matches(charts, labels)
    short = [c for c in claim_items if got.get(c["label"], 0) <= max_matches]
    short.sort(key=lambda c: (not c.get("independent"),
                              got.get(c["label"], 0),
                              weak.get(c["label"], 0),
                              c.get("claim_no") or 0))
    return short[:limit], got


# ---------------------------------------------------------------------------
# what to search for
# ---------------------------------------------------------------------------
_PLAN_SYS = (
    "You are a patent examiner who has finished a prior-art search and found nothing for a few of "
    "the subject patent's claims. You are now going back for those claims specifically.\n"
    "\n"
    "For EACH claim you are given, write what would actually find its prior art:\n"
    '  "concept": the technical idea the claim covers, in 8-20 words, stripped of claim '
    "scaffolding (\"at least one\", \"said\", \"wherein\", reference numerals) and of this "
    "applicant's private naming. For a DEPENDENT claim, state the idea IT ADDS, in the context of "
    "the independent claim it depends from — not the whole combination again.\n"
    '  "other_words": 4-8 terms and phrasings the same thing goes by elsewhere — the older term, '
    "the generic term, the industrial term, the term another field would use.\n"
    '  "counts_as": one sentence saying what ELSE in a reference would disclose this claim — a '
    "species of a genus it states, an equivalent structure performing the same function.\n"
    f'  "queries": {QUERIES_PER_CLAIM} SHORT search queries, 6-15 words each, that would retrieve '
    "documents teaching this idea. Write them the way a DIFFERENT patent would describe the thing, "
    "not the way this claim does. Make them DIFFERENT FROM EACH OTHER: one plain and functional, "
    "one using the older or industrial vocabulary, one from the neighbouring field where this "
    "component is ordinary. Do NOT repeat the invention as a whole in every query — the search "
    "already did that and found nothing for this claim, which usually means the art for it sits "
    "outside this invention's own field.\n"
    "\n"
    'Return ONLY JSON: {"claims":[{"item":"<the claim label verbatim, e.g. \\"claim 7\\">",'
    '"concept":"...","other_words":["..."],"counts_as":"...","queries":["..."]}]} with one entry '
    "per claim, in the order given."
)


def plan(claims, brief="", title="", independents=(), description=""):
    """{label: {concept, other_words, counts_as, queries}} for the orphaned claims, in one call.

    `independents` and `description` are the CONTEXT, and without them this does not work. A
    dependent claim read alone is a fragment: "the apparatus of claim 1, wherein the seal is
    annular" names no apparatus and no seal, and a query built from it retrieves annular things.
    The model is given the independent claims it hangs from and the invention's own description so
    it can write the idea the claim actually adds.
    """
    if not claims:
        return {}
    payload = {
        "invention_title": title[:200],
        "invention": (brief or "")[:3000],
        "description_context": (description or "")[:6000],
        "independent_claims": [{"item": c["label"], "text": str(c.get("text") or "")[:2500]}
                               for c in independents][:4],
        "claims_with_no_prior_art": [{"item": c["label"], "text": str(c.get("text") or "")[:2500]}
                                     for c in claims],
    }
    try:
        out = llm.chat_json(_PLAN_SYS, json.dumps(payload, ensure_ascii=False),
                            max_tokens=6000) or {}
    except Exception:
        traceback.print_exc()
        return {}
    plans = {}
    items = [c["label"] for c in claims]
    for item, raw in zip(items, deep_analysis._align(out.get("claims"), items)):
        if not isinstance(raw, dict):
            continue
        queries = [" ".join(str(q).split()) for q in (raw.get("queries") or [])
                   if str(q or "").strip()][:QUERIES_PER_CLAIM]
        concept = " ".join(str(raw.get("concept") or "").split())[:240]
        words = [" ".join(str(w).split()) for w in (raw.get("other_words") or [])
                 if str(w or "").strip()][:8]
        counts = " ".join(str(raw.get("counts_as") or "").split())[:300]
        #  A claim with no usable query is not searchable; fall back to its own text rather than
        #  skipping it, because a literal claim query is still better than no second look.
        if not queries:
            text = next((c["text"] for c in claims if c["label"] == item), "")
            queries = [" ".join(str(text).split()[:40])] if text else []
        if not queries:
            continue
        bits = [b for b in (concept,
                            ("also called: " + "; ".join(words)) if words else "",
                            counts) if b]
        plans[item] = {"concept": concept, "other_words": words, "counts_as": counts,
                       "queries": queries, "hint": " — ".join(bits)[:600]}
    return plans


# ---------------------------------------------------------------------------
# the search itself
# ---------------------------------------------------------------------------
def find_candidates(plans, subject, mode, exclude_pubs, exclude_families, retriever,
                    per_claim=PER_CLAIM, cap=MAX_NEW, emit=None, texts=None):
    """Run the orphan queries and return new candidates, round-robin by claim.

    ROUND-ROBIN, NOT BEST-FIRST. Fusing every orphan claim's results into one list and taking the
    head hands the whole budget to whichever claim happens to retrieve most confidently, which is
    the claim that needed this least. Each claim gets its own share.
    """
    if not plans:
        return []
    specs = []
    for label, p in plans.items():
        for q in p["queries"]:
            specs.append((label, q))
    if emit:
        emit("rescue_search_start", n=len(specs), claims=len(plans))

    #  A psycopg connection cannot be shared by concurrent queries, so without fork() this runs
    #  serially. Silently fanning out over one connection is a corruption, not a slowdown.
    fork = getattr(retriever, "fork", None)
    workers = min(SEARCH_WORKERS, len(specs)) if callable(fork) else 1
    done = [0]
    lock = threading.Lock()

    def one(spec):
        label, q = spec
        worker = fork() if callable(fork) else retriever
        try:
            res = worker.search(q, subject=subject, mode=mode, config=list(CHANNELS),
                                cpc_hints=None, do_rerank=False, wide=False, topk=SEARCH_TOPK)
            out = [(fk, pid, float(sc)) for fk, pid, sc, _ in res.family_ranked]
        except Exception:
            traceback.print_exc()
            out = []
        finally:
            if worker is not retriever:
                close = getattr(worker, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        with lock:
            done[0] += 1
            if emit:
                emit("rescue_search_progress", done=done[0], total=len(specs), claim=label)
        return label, out

    by_claim = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for label, out in ex.map(one, specs):
            #  Fuse this claim's own queries by best rank across them, so a family found by two of
            #  the three phrasings beats one found by a single lucky query.
            agg = by_claim.setdefault(label, {})
            for rank, (fk, pid, sc) in enumerate(out):
                cur = agg.get(fk)
                if cur is None or rank < cur[0]:
                    agg[fk] = (rank, pid, sc)

    #  THE GRAPH FIRST (evidence flywheel). A document an earlier run PROVED to hold evidence for
    #  this limitation is a better lead than a fresh ANN hit, and it costs nothing. Injected at
    #  rank -1 so it sits ahead of every search hit in that claim's queue; the round-robin and
    #  the exclusions treat it like any other candidate. Only documents the local corpus holds
    #  are injected — a graph row for an unheld pub is a lead for the acquisition, not a read.
    if texts:
        try:
            import db
            import evidence
            graph_pubs = {}
            for label in list(plans):
                t = texts.get(label) or ""
                if not t:
                    continue
                for e in evidence.known_disclosers(t, limit=6):
                    graph_pubs.setdefault(e["publication_number"], []).append(label)
            if graph_pubs:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT publication_number, id, "
                        "COALESCE(NULLIF(simple_family_id,''), publication_number) AS fam "
                        "FROM publications WHERE publication_number = ANY(%s)",
                        (list(graph_pubs),))
                    held = cur.fetchall()
                injected = 0
                for r in held:
                    for label in graph_pubs.get(r["publication_number"], []):
                        agg = by_claim.setdefault(label, {})
                        if r["fam"] not in agg:
                            agg[r["fam"]] = (-1, r["id"], 1.0)
                            injected += 1
                if injected:
                    print(f"[rescue] evidence graph supplied {injected} proven leads across "
                          f"{len(graph_pubs)} documents before any search ran", flush=True)
        except Exception:
            traceback.print_exc()

    seen_fam = set(exclude_families or ())
    picked, order = [], []
    queues = {}
    for label, agg in by_claim.items():
        queues[label] = [fk for fk, _ in sorted(agg.items(), key=lambda kv: kv[1][0])]
    #  Deterministic claim order: the order `plans` was built in, which is orphans() order.
    labels = [l for l in plans if l in queues]
    taken = {l: 0 for l in labels}
    while len(picked) < cap:
        progressed = False
        for label in labels:
            if taken[label] >= per_claim or len(picked) >= cap:
                continue
            q = queues[label]
            while q:
                fk = q.pop(0)
                if fk in seen_fam:
                    continue
                seen_fam.add(fk)
                rank, pid, sc = by_claim[label][fk]
                picked.append({"fam": fk, "pid": pid, "for_claim": label, "score": sc})
                order.append(fk)
                taken[label] += 1
                progressed = True
                break
        if not progressed:
            break
    #  Publication numbers and titles come from the same family-representative resolver the main
    #  path uses, so a rescued family is represented by the member with full text rather than by
    #  whichever member the ANN happened to rank first.
    import db
    import webview
    reps = {}
    try:
        conn = db.connect()
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                reps = webview.resolve_family_reps(
                    cur, order, subject_efd=getattr(subject, 'efd', None))
        finally:
            conn.close()
    except Exception:
        traceback.print_exc()
        return []
    out, seen_pub = [], set(exclude_pubs or ())
    for c in picked:
        r = reps.get(c["fam"])
        if not r:
            continue
        pub = r["publication_number"]
        if pub in seen_pub:
            continue
        seen_pub.add(pub)
        out.append({"pub": pub, "fam": c["fam"], "title": r.get("title") or "",
                    "for_claim": c["for_claim"]})
    return out


# ---------------------------------------------------------------------------
# the whole pass
# ---------------------------------------------------------------------------
def run(charts, claim_items, features, hints, *, subject, mode, retriever, brief="", title="",
        description="", exclude_pubs=(), exclude_families=(), enrich=None, ledger=None,
        emit=None):
    """Rescue the orphaned claims. Returns (new_charts, summary). Never raises.

    `charts` is mutated in place by the re-read of already-read references; `new_charts` are extra
    references for the caller to merge before it scores and orders anything, so a rescued document
    competes on the same evidence as everything else.
    """
    started = time.time()
    summary = {"ran": False, "orphans": [], "n_orphans": 0, "queries": 0, "candidates": 0,
               "read": 0, "reread_refs": 0, "reread_cells": 0, "covered": [], "seconds": 0.0}
    if not claim_items:
        return [], summary

    #  THE LEDGER DECIDES WHAT IS MISSING, when there is one. It tracks LIMITATIONS — the
    #  separate requirements inside each claim — so "uncovered" here means a specific requirement
    #  no document has been shown to teach, not a whole conjunction that failed because one part
    #  of it did. Without a ledger (Type A, or a run whose claims could not be split) fall back to
    #  counting matches per claim.
    lim_rows = {}
    if ledger is not None:
        #  EMPTY FIRST, THEN THIN — not one or the other. An `if not rows` fallback only ever
        #  searched for limitations with NOTHING against them, and a live run had 2 of those
        #  against 15 that were "partial": evidence exists but fewer than `cover_min` documents
        #  disclose it. Partial is where an attorney's references sit, and the KPI this ledger
        #  reports is the count of limitations with two grounded disclosures, not the count with
        #  one. Both are ordered weakest-and-most-load-bearing first by `uncovered`.
        rows = ledger.uncovered(include_partial=False)
        if len(rows) < MAX_CLAIMS:
            have = {l["id"] for l in rows}
            rows = rows + [l for l in ledger.uncovered(include_partial=True)
                           if l["id"] not in have]
        rows = rows[:MAX_CLAIMS]
        short = [{"label": l["id"], "claim_no": l.get("claim_no"),
                  "independent": bool(l.get("independent")), "text": l["text"]} for l in rows]
        #  Kept whole: the limitation portfolio needs the claim it came from and its id, which the
        #  chart-item shape above drops.
        lim_rows = {l["id"]: l for l in rows}
        matched = {l["id"]: len(ledger.evidence.get(l["id"]) or [])
                   for l in ledger.limitations}
        summary["driver"] = "ledger"
        summary["ledger_before"] = ledger.summary()["counts"]
    else:
        short, matched = orphans(charts, claim_items)
        summary["driver"] = "claim_matches"
    summary["claim_matches"] = dict(matched)
    if not short:
        summary["seconds"] = round(time.time() - started, 1)
        return [], summary
    summary["ran"] = True
    summary["orphans"] = [c["label"] for c in short]
    summary["n_orphans"] = len(short)
    if emit:
        emit("rescue_start", n=len(short), claims=[c["label"] for c in short])
    if summary["driver"] == "ledger":
        n_empty = sum(1 for l in rows if ledger.status(l["id"]) == "uncovered")
        print(f"[rescue] {len(short)} limitations to search for — {n_empty} with no evidence at "
              f"all, {len(short) - n_empty} with less than {ledger.cover_min} disclosures: "
              f"{', '.join(c['label'] for c in short)}", flush=True)
    else:
        print(f"[rescue] {len(short)} claims with <= {ORPHAN_MAX_MATCHES} grounded matches: "
              f"{', '.join(c['label'] for c in short)}", flush=True)

    independents = [c for c in claim_items if c.get("independent")] or claim_items[:1]
    plans = plan(short, brief=brief, title=title, independents=independents,
                 description=description)
    summary["queries"] = sum(len(p["queries"]) for p in plans.values())
    claim_hints = {l: p["hint"] for l, p in plans.items() if p.get("hint")}
    claim_texts = {c["label"]: c["text"] for c in claim_items}

    # ---- 1. ask the documents we ALREADY read, about these claims alone ---------------------
    #  The cheapest evidence in the pipeline. These references are in the corpus, were judged worth
    #  reading, and their first reading answered thirty claims and forty-eight features in one
    #  breath — which is exactly the condition under which a reader economises. Asking again about
    #  five claims, with the concept in hand, is one call and no retrieval at all.
    read_head = [r for r in charts if r.get("method") == "llm"][:REREAD_TOP]
    if read_head and claim_hints:
        if emit:
            emit("rescue_reread_start", n=len(read_head))
        orphan_labels = set(summary["orphans"])

        def second(ref):
            return second_look(ref, orphan_labels, claim_hints, claim_texts)

        with ThreadPoolExecutor(max_workers=min(READ_WORKERS, max(1, len(read_head)))) as ex:
            gained = list(ex.map(second, read_head))
        summary["reread_cells"] = sum(gained)
        summary["reread_refs"] = sum(1 for g in gained if g)
        print(f"[rescue] claim re-read: {summary['reread_cells']} cells recovered across "
              f"{summary['reread_refs']} of {len(read_head)} references already read", flush=True)

    # ---- 2. search for what is still uncovered ----------------------------------------------
    labels = [c["label"] for c in claim_items]
    before = summary.get("claim_matches") or {}

    def _covered(all_charts):
        after, _ = claim_matches(all_charts, labels)
        return after, [l for l in summary["orphans"] if after.get(l, 0) > before.get(l, 0)]

    #  WHAT THE CHEAP PASS DID NOT FIX. The ledger is the driver when there is one, so it is what
    #  gets asked — after re-ingesting the cells the re-read just added, or it answers from before
    #  the re-read ran. `orphans()` would silently undo the "empty first, then thin" selection
    #  above: it counts a limitation with two PARTIAL matches as answered, and a limitation with
    #  two partials and no disclosure is exactly the one still worth searching for.
    if ledger is not None:
        try:
            ledger.ingest_charts(charts)
        except Exception:
            traceback.print_exc()
        keep = {l["id"] for l in rows if ledger.status(l["id"]) != "covered"}
    else:
        still, _ = orphans(charts,
                           [c for c in claim_items if c["label"] in set(summary["orphans"])])
        keep = {c["label"] for c in still}
    plans = {l: p for l, p in plans.items() if l in keep}
    if not plans:
        summary["claim_matches_after"], summary["covered"] = _covered(charts)
        summary["seconds"] = round(time.time() - started, 1)
        print(f"[rescue] the re-read covered {len(summary['covered'])} of "
              f"{len(summary['orphans'])} orphaned claims; no extra search needed", flush=True)
        return [], summary

    cands = []
    try:
        cands = find_candidates(plans, subject, mode, exclude_pubs, exclude_families, retriever,
                                emit=emit,
                                texts={c["label"]: c.get("text") or "" for c in claim_items})
    except Exception:
        traceback.print_exc()
    summary["local_candidates"] = len(cands)

    #  LOCAL FIRST — see LOCAL_ENOUGH. The block below costs ~$10.50 and ~18 minutes of BigQuery
    #  per search; it is worth that only when the corpus we already own came up short.
    if LOCAL_ENOUGH and len(cands) >= LOCAL_ENOUGH:
        summary["skipped_acquisition"] = f"{len(cands)} local candidates >= {LOCAL_ENOUGH}"
        print(f"[rescue] the local corpus returned {len(cands)} candidates for {len(plans)} "
              f"uncovered limitations; skipping the BigQuery acquisition "
              f"(saves ~$10.50 and ~18 min)", flush=True)
    else:
        # ---- 2b. GO AND GET WHAT THIS CORPUS DOES NOT HOLD --------------------------------------
        #  Searching harder cannot find a document that was never indexed, and the corpus is seeded
        #  from one field's CPC branches. Measured on US 2026/0109053: of ten references an attorney
        #  filed, three are absent from this corpus entirely and three more are text-less stubs, and
        #  every one of those six is classified OUTSIDE the seeded branches — mufflers, sound
        #  absorption, portable power tools, blowers. See claim_acquire.
        still_short = [c for c in claim_items if c["label"] in set(plans)]
        try:
            import claim_acquire
            #  THE WORLD FIRST. All 170M patents, in the classes that own these claims, ingested WITH
            #  THEIR TEXT — which is the difference between finding a document and being able to read
            #  it. Measured: of ten references an attorney filed, three were absent from this corpus
            #  and three more were text-less stubs; nine of the ten are in a BigQuery working set with
            #  full text. Seeded from the references the reading already trusts, so Google's own
            #  similarity graph expands from evidence rather than from a guess.
            #
            #  ONE PORTFOLIO PER LIMITATION when the ledger gave us limitations, because a limitation
            #  is a thing in a place in a kind of apparatus and an OR of keywords cannot say that.
            #  Measured on the same working set: the keyword shape returns 111,545 hits with no usable
            #  rank; the faceted shape returns 14,373 holding nine of the ten, and asking per
            #  requirement moved one reference from rank 8,810 to 135. by_worldset stays as the
            #  fallback for a run with no ledger, and for the case where no facets come back.
            seeds = [r["pub"] for r in read_head[:40]]
            ws = {}
            lims_to_search = [lim_rows[c["label"]] for c in still_short if c["label"] in lim_rows]
            if lims_to_search:
                #  Its own try. This whole block runs at the end of a search that already has an
                #  answer, so a new route failing must cost its own candidates and nothing else —
                #  sharing the outer handler would take the keyword worldset, the concept fan-out and
                #  the citation neighbourhood down with it.
                try:
                    ws = claim_acquire.by_limitation(
                        lims_to_search, brief=brief, title=title, subject=subject, seeds=seeds,
                        emit=emit)
                except Exception:
                    traceback.print_exc()
                    ws = {"error": "limitation portfolio raised"}
                summary["limitation_portfolio"] = {
                    k: ws.get(k) for k in ("rows", "queries", "screened", "n_new", "with_text",
                                           "error")}
                summary["limitation_pool"] = ws.get("pool") or {}
                summary["limitation_classes"] = ws.get("classes") or []
            if not (ws.get("candidates") if ws else None):
                if lims_to_search:
                    print(f"[rescue] the limitation portfolio returned nothing "
                          f"({(ws or {}).get('error') or 'no reason given'}); "
                          f"falling back to the keyword worldset", flush=True)
                ws = claim_acquire.by_worldset(
                    still_short, hints=claim_hints, brief=brief, title=title, subject=subject,
                    seeds=seeds, emit=emit)
                summary["worldset"] = {k: ws.get(k) for k in
                                       ("rows", "queries", "n_new", "with_text", "error")}
                summary["worldset_classes"] = ws.get("classes") or []
                summary["worldset_terms"] = (ws.get("terms") or [])[:40]
            seen0 = {c["pub"] for c in cands} | set(exclude_pubs or ())
            for c in ws.get("candidates") or []:
                if c["pub"] not in seen0:
                    seen0.add(c["pub"])
                    cands.append(c)

            #  by_concept ONLY WHEN THE WORLD ROUTES CAME BACK EMPTY-HANDED. It reaches App A's
            #  fan-out, which returns bibliographic records, and `external.materialise` writes them
            #  with no text at all. Measured on the run that prompted this: 300 publications acquired,
            #  0 of 40 readable, and those 40 unreadable candidates were 40 of the 46 the rescue then
            #  "read" — the whole rescue read six documents. A candidate the reader cannot read is not
            #  a cheap candidate, it is a slot taken from one that could have been read. It stays as a
            #  fallback because it is the only route that does not need BigQuery.
            seen = {c["pub"] for c in cands} | set(exclude_pubs or ())
            if len(cands) < CONCEPT_FALLBACK_BELOW:
                got = claim_acquire.by_concept(still_short, hints=claim_hints, brief=brief,
                                               title=title, emit=emit)
                summary["acquire"] = {k: got.get(k) for k in
                                      ("queries", "n_candidates", "n_new", "error")}
                summary["acquire_classes"] = sorted({c for p in (got.get("plan") or {}).values()
                                                     for c in (p.get("cpc") or [])})
                for c in got.get("candidates") or []:
                    if c["pub"] not in seen:
                        seen.add(c["pub"])
                        cands.append(c)
            else:
                summary["acquire"] = {"skipped": f"{len(cands)} candidates already acquired with text"}
            #  And the art our own best references point at, which crosses classification boundaries
            #  in the one direction a CPC-seeded corpus cannot.
            cited = claim_acquire.by_citation([r["pub"] for r in read_head], exclude_pubs=seen,
                                              emit=emit)
            summary["acquire_citations"] = {k: cited.get(k) for k in ("n_edges", "n_new", "error")}
            for c in cited.get("candidates") or []:
                if c["pub"] not in seen:
                    seen.add(c["pub"])
                    cands.append(c)
        except Exception:
            traceback.print_exc()

    summary["candidates"] = len(cands)
    if not cands:
        summary["seconds"] = round(time.time() - started, 1)
        return [], summary
    print(f"[rescue] {len(cands)} candidates for {len(plans)} claims that no reference covers, "
          f"from queries with no classification filter", flush=True)

    if enrich:
        try:
            enrich(cands)
        except Exception:
            traceback.print_exc()

    # ---- 3. read them, against the SAME checklist as everything else -------------------------
    #  Not against the orphaned claims alone. A rescued reference has to be comparable to the rest
    #  of the report or it cannot be ranked with it, and a document found for claim 7 routinely
    #  turns out to disclose three features nobody was looking for it to.
    if emit:
        emit("rescue_read_start", n=len(cands))
    done = [0]
    lock = threading.Lock()

    def one(c):
        try:
            ref = deep_analysis.analyse_reference(c["pub"], features, claim_items,
                                                  c.get("title") or "", hints=hints)
        except Exception as exc:
            ref = {"pub": c["pub"], "title": c.get("title") or "", "found": False,
                   "features": [], "claims": [], "method": "error", "error": str(exc)[:200],
                   "chars": 0}
        ref["screen"] = None
        ref["retrieval_rank"] = None
        ref["family"] = c["fam"]
        #  Stamped so the page can say WHY this document is in the report: it was not retrieved by
        #  the search, it was gone back for on behalf of one claim.
        ref["rescue"] = True
        ref["rescue_for"] = c["for_claim"]
        with lock:
            done[0] += 1
            if emit and (done[0] % 5 == 0 or done[0] == len(cands)):
                emit("rescue_read_progress", done=done[0], total=len(cands), pub=c["pub"])
        return ref

    with ThreadPoolExecutor(max_workers=min(READ_WORKERS, max(1, len(cands)))) as ex:
        new_charts = list(ex.map(one, cands))
    summary["read"] = sum(1 for r in new_charts if r.get("method") == "llm")

    # ---- 4. and ask THEM the narrow question too ---------------------------------------------
    #  Not optional, and not redundant with the read above. See second_look: a full read of a
    #  rescued reference against all thirty claims grounded 1 claim cell in 221 on the run that
    #  prompted this, because a dependent claim is a conjunction and these documents were found
    #  for the part it ADDS. Without this pass the rescue finds the right art and then reports
    #  that it discloses nothing.
    fresh = [r for r in new_charts if r.get("method") == "llm"]
    if fresh and claim_hints:
        labels_now = set(plans)
        with ThreadPoolExecutor(max_workers=min(READ_WORKERS, len(fresh))) as ex:
            gained = list(ex.map(
                lambda r: second_look(r, labels_now, claim_hints, claim_texts), fresh))
        summary["rescue_reread_cells"] = sum(gained)
        summary["rescue_reread_refs"] = sum(1 for g in gained if g)
        print(f"[rescue] narrow re-ask of the {len(fresh)} rescued references: "
              f"{summary['rescue_reread_cells']} claim cells grounded across "
              f"{summary['rescue_reread_refs']} of them", flush=True)

    summary["claim_matches_after"], summary["covered"] = _covered(charts + new_charts)
    summary["seconds"] = round(time.time() - started, 1)
    print(f"[rescue] read {summary['read']} rescued references in full; "
          f"{len(summary['covered'])} of {len(summary['orphans'])} orphaned claims now have "
          f"prior art ({summary['seconds']}s)", flush=True)
    return new_charts, summary
