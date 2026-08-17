"""One retrieval per claim, used to decide WHAT GETS READ rather than to widen the pool.

THE PROBLEM THIS SOLVES, MEASURED ON adhoc-db64a3dd7c98
-------------------------------------------------------
That search read 694 references in full (42,948,337 characters, 2h21m, ~$125) to show 60 cards. The
read set was chosen by ONE global ordering: the screen score, which is a judgement about the
invention AS A WHOLE. Nothing in that ordering knows that claim 9[b] has three plausible references
in the pool and claim 1[a] has four hundred, so the budget is spent where the field is crowded and
starved exactly where a novelty attack needs help. The evidence for that is in the same run's own
numbers: `claim_df` ranged from 581 references grounding claim 1[a] down to 21 grounding claim 9[b],
and ten limitations still finished the reading with less than two disclosures and had to be rescued
afterwards at 1h51m of extra work.

Reading MORE does not fix it, and that is measured too (see deep_rank.CHART_TOP_MAX): charting 504
references instead of 344 did not improve the order within the read set, it only made the fixed-size
page harder to win. The read set is a shortlist for a 60-card page. So the fix is not a bigger
shortlist, it is a shortlist chosen per claim.

WHAT THIS DOES
--------------
One ANN search per claim, on a short brief plus THE CLAIM'S OWN VERBATIM TEXT, then a round-robin
quota so each claim contributes its best candidates to the read set before any claim contributes its
second-best. The searches run against the families the main retrieval already found: this stage
re-spends the reading budget, it does not grow the candidate pool. Finding documents the main search
never reached is `claim_rescue`'s job and it still runs afterwards.

VERBATIM CLAIM TEXT IS A PRECONDITION, NOT A DETAIL. Until 2026-08-16 the limitation text this kind
of query would have been built from was 47% model paraphrase, and on the measured subject a method
claim had silently become an apparatus claim. Searching per claim with the wrong claim text would
have concentrated the budget on the wrong art with more confidence than before. See
`limitations._snap`.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

#  DENSE ONLY, AND THIS IS THE DIFFERENCE BETWEEN THREE MINUTES AND THREE HOURS.
#
#  The first version copied the orphan rescue's channel list, which includes `bm25` and
#  `claim_bm25`. That is right for the rescue, whose queries are short model-written phrases, and
#  badly wrong here, where the query is a VERBATIM CLAIM of up to 1,200 characters. A claim of that
#  length is roughly 180 terms; as a lexical query that is a 180-term scan over 26.7M chunks, per
#  claim. Dense retrieval embeds a query of any length into ONE vector, so the same long text costs
#  nothing extra.
#
#  MEASURED on adhoc-5972e6042dfa, the first run with this stage enabled:
#      [claim_reach] ... 240 references claimed a read slot (10534s)
#  2h56m, six workers, 40 limitations, against a whole-run baseline of 2h21m. The stage cost more
#  than the entire search it was meant to speed up, and it did it in the lexical channels.
#
#  No CPC and no citation expansion either: both pull back toward the neighbourhood the
#  whole-invention search already covered, and the point here is per-claim reach within the pool.
CHANNELS = [c for c in os.environ.get("CLAIM_REACH_CHANNELS", "dense,claim_dense").split(",")
            if c.strip()]

ENABLED = os.environ.get("CLAIM_REACH", "1") != "0"
#  Families pulled per claim before we intersect with the candidate pool. Lowered 120 -> 80: on the
#  measured run every limitation came back with 49-117 hits and nearly all of them mapped into the
#  pool, so the tail was returning the same neighbourhood 40 times over rather than distinguishing
#  between claims.
TOPK = int(os.environ.get("CLAIM_REACH_TOPK", "80"))
#  A HARD WALL-CLOCK CEILING ON THE WHOLE STAGE. This exists because the first version of this file
#  ran for 2h56m inside a search whose entire previous runtime was 2h21m, and nothing stopped it or
#  even said it was happening until the run finished. A stage that improves ranking must never be
#  able to dominate the run it is improving: past this budget we stop launching searches and use
#  whatever came back, which degrades the quota rather than the search.
BUDGET_S = float(os.environ.get("CLAIM_REACH_BUDGET_S", "600"))
#  How many references each claim may put into the read set. 20 claims x 6 is 120 documents, which
#  is most of a 200-document read budget spent on per-claim winners instead of on the tail of one
#  global ranking.
PER_CLAIM = int(os.environ.get("CLAIM_REACH_PER_CLAIM", "6"))
#  Concurrent searches. Each forks its own PostgreSQL connection; a psycopg connection cannot be
#  shared by concurrent queries, so without fork() this degrades to serial rather than corrupting.
WORKERS = int(os.environ.get("CLAIM_REACH_WORKERS", "6"))
#  Claims are usually far longer than a good ANN query and the tail of a claim is boilerplate.
MAX_CLAIM_CHARS = int(os.environ.get("CLAIM_REACH_CLAIM_CHARS", "1200"))
MAX_BRIEF_CHARS = int(os.environ.get("CLAIM_REACH_BRIEF_CHARS", "400"))


def _query(brief, claim_text):
    """A short blurb plus the claim itself.

    The blurb is what keeps a claim query inside the invention's field: a dependent claim on its own
    ("wherein the maximum acceleration is greater...") retrieves control theory from every industry.
    The claim is what makes the query specific. Neither alone is enough.
    """
    b = " ".join(str(brief or "").split())[:MAX_BRIEF_CHARS]
    c = " ".join(str(claim_text or "").split())[:MAX_CLAIM_CHARS]
    return (b + "\n\n" + c).strip() if b else c


def reach(claim_items, rows, brief="", subject=None, mode="novelty", retriever=None,
          topk=TOPK, emit=None, log=print):
    """-> {claim label: [pub, ...] best first}, restricted to families already in `rows`."""
    if not ENABLED or not claim_items or not rows or retriever is None:
        return {}
    #  Family -> the pool row that represents it. The pool is what the read budget is spent on, so
    #  a per-claim hit outside it is not actionable here; the rescue picks those up later.
    fam_row = {}
    for r in rows:
        fk = r.get("fam")
        if fk is not None and fk not in fam_row:
            fam_row[fk] = r
    specs = [(c["label"], _query(brief, c.get("text"))) for c in claim_items
             if str(c.get("text") or "").strip()]
    if not specs:
        return {}
    if emit:
        emit("claim_reach_start", n=len(specs))

    fork = getattr(retriever, "fork", None)
    workers = min(WORKERS, len(specs)) if callable(fork) else 1
    done, lock = [0], threading.Lock()
    t_start = time.time()
    slowest = [0.0, ""]
    skipped = [0]

    def one(spec):
        label, q = spec
        #  Past the budget, return empty rather than starting another search. Threads already in
        #  flight finish; nothing new is launched. `quota` simply has fewer claims to serve.
        if time.time() - t_start > BUDGET_S:
            with lock:
                skipped[0] += 1
            return label, []
        t_one = time.time()
        worker = fork() if callable(fork) else retriever
        hits = []
        try:
            res = worker.search(q, subject=subject, mode=mode, config=list(CHANNELS),
                                cpc_hints=None, do_rerank=False, wide=False, topk=topk)
            for fk, _pid, _sc, _x in res.family_ranked:
                r = fam_row.get(fk)
                if r is not None:
                    hits.append(r["pub"])
        except Exception:
            traceback.print_exc()
        finally:
            if worker is not retriever:
                close = getattr(worker, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        dt = time.time() - t_one
        with lock:
            done[0] += 1
            if dt > slowest[0]:
                slowest[0], slowest[1] = dt, label
            if emit:
                emit("claim_reach_progress", done=done[0], total=len(specs), claim=label)
        return label, hits

    out = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for label, hits in ex.map(one, specs):
            out[label] = hits
    #  Per-search timing in the log, not just a total. The total alone is what made a 2h56m stage
    #  look like one number at the end of a five-hour run instead of a search costing 26 minutes.
    log(f"[claim_reach] {len(specs)} searches over {len(CHANNELS)} channels in "
        f"{time.time() - t_start:.0f}s at {workers} workers; slowest {slowest[0]:.0f}s "
        f"({slowest[1]})"
        + (f"; {skipped[0]} SKIPPED at the {BUDGET_S:.0f}s budget" if skipped[0] else ""))
    return out


def quota(by_claim, per_claim=PER_CLAIM, exclude=(), cap=None):
    """Round-robin across claims -> [pub, ...] in the order they should be read.

    ROUND-ROBIN, NOT BEST-FIRST, for the same reason `claim_rescue.find_candidates` does it: fusing
    every claim's hits into one list and taking the head hands the budget to whichever claim
    retrieves most confidently, and that is the claim that needed the help least.
    """
    if not by_claim:
        return []
    seen = set(exclude or ())
    queues = {label: list(hits) for label, hits in by_claim.items() if hits}
    labels = [l for l in by_claim if l in queues]
    taken = {l: 0 for l in labels}
    picked = []
    while True:
        progressed = False
        for label in labels:
            if taken[label] >= per_claim or (cap is not None and len(picked) >= cap):
                continue
            q = queues[label]
            while q:
                pub = q.pop(0)
                if pub in seen:
                    continue
                seen.add(pub)
                picked.append(pub)
                taken[label] += 1
                progressed = True
                break
        if not progressed or (cap is not None and len(picked) >= cap):
            break
    return picked
