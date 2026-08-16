"""Rank references by what they ADD to the legal argument, not by how much they resemble it.

THE PROBLEM WITH RANKING BY SIMILARITY
--------------------------------------
Every prior-art search exists to support one argument: for each thing the subject discloses, was
it already known, and where. The deliverable is therefore a SET of references that between them
attack every disclosure, not a list of the fifty documents most like the invention.

deep_rank scored every reference independently and sorted. That is provably the wrong objective.
If ten disclosures exist and fifty documents each cover the same nine, all fifty outrank the one
document that covers the tenth -- so the report shows the ninety-nine most similar results and is
permanently blind to the only reference that speaks to disclosure ten. Measured on a finished
report (bench-suction_chuck-v13):

    top 50 covered 87% of the weighted disclosure mass
    42 of the 50 displayed documents added NOTHING AT ALL to that coverage
    marginal gain reached zero at display position 9

Forty-two of fifty slots were spent re-proving what was already proven.

WHAT THIS DOES INSTEAD
----------------------
Greedy maximum-coverage. Repeatedly take the reference whose marginal gain is largest, where gain
is the rarity-weighted disclosure mass it covers BETTER THAN ANYTHING ALREADY CHOSEN:

    gain(doc | chosen) = SUM over disclosures d of  idf(d) * max(0, q(doc,d) - best(d))

with q the grounded verdict strength (disclosed 1.0, partial 0.55, uncertain 0.5, absent 0). The
first pick is still the strongest reference overall, because nothing is covered yet, so the head
of the report does not get stranger. What changes is everything after it.

Two knobs, both defaults measured rather than assumed (see eval/coverage_sweep.py):

    CORROBORATION  a disclosure proven once is proven, but an argument is usually safer with a
                   second independent reference, and an examiner cites more than one. This is the
                   credit a document still earns for covering ground already covered. 0 would make
                   the ranking purely about novelty of contribution; 1 would restore the old
                   redundant behaviour. Set to 0.10 because 0.25 was measured to REINTRODUCE the
                   defect: twenty documents corroborating three disclosures earn 0.75 of mass
                   between them, which with any meaningful score weight beats the 1.0 earned by
                   the one document that uniquely answers the tenth. See the test named for it.
    SCORE_WEIGHT   marginal gain says what a document ADDS; the evidence score says how good it
                   is. Once every disclosure is covered every remaining gain is zero, and this is
                   what orders the rest, so it must never be zero -- but it must stay small, or a
                   high-scoring duplicate outbids a low-scoring unique contribution and the whole
                   redesign is undone.
"""
from __future__ import annotations

import os

#  Grounded verdict strength. Mirrors deep_rank._W so one document cannot be worth more here than
#  it is there.
_W = {"disclosed": 1.0, "partial": 0.55, "uncertain": 0.5, "absent": 0.0}

CORROBORATION = float(os.environ.get("COVERAGE_CORROBORATION", "0.10"))
SCORE_WEIGHT = float(os.environ.get("COVERAGE_SCORE_WEIGHT", "0.15"))
#  How deep to re-order. Beyond the display window the order stops mattering and the greedy pass
#  is O(n * disclosures) per pick.
RERANK_DEPTH = int(os.environ.get("COVERAGE_RERANK_DEPTH", "300"))


def quality(entry) -> dict:
    """{disclosure: best grounded strength} for one charted reference."""
    out = {}
    for c in (entry.get("covered") or []):
        item = c.get("item")
        if not item:
            continue
        v = _W.get(c.get("verdict"), 0.0)
        if v > out.get(item, 0.0):
            out[item] = v
    return out


def rank(order, by_pub, idf, depth=None, corroboration=None, score_weight=None,
         score_of=None):
    """Greedy maximum-coverage order over the head of `order`.

    -> (new_order, [marginal_gain per position]) so a caller can show WHY a reference is placed
       where it is, which is the part an attorney actually needs.

    Never raises and never drops a reference: this is a permutation of the input.
    """
    depth = RERANK_DEPTH if depth is None else depth
    corr = CORROBORATION if corroboration is None else corroboration
    sw = SCORE_WEIGHT if score_weight is None else score_weight
    idf = idf or {}
    if not order or not idf:
        return list(order or []), []

    head = [p for p in order if p in by_pub][:depth]
    tail = [p for p in order if p not in set(head)]
    if len(head) < 2:
        return list(order), []

    def _score(p):
        if score_of:
            return float(score_of(p) or 0.0)
        return float((by_pub.get(p) or {}).get("score") or 0.0)

    q = {p: quality(by_pub[p]) for p in head}
    #  normalise the evidence score so its weight means the same thing on every report
    hi = max((_score(p) for p in head), default=0.0) or 1.0
    total_mass = sum(idf.values()) or 1.0

    best = {}
    chosen, gains = [], []
    remaining = set(head)
    while remaining:
        pick, pick_val, pick_gain = None, None, 0.0
        for p in remaining:
            gain = 0.0
            for d, v in q[p].items():
                w = idf.get(d, 0.0)
                if not w:
                    continue
                have = best.get(d, 0.0)
                if v > have:
                    gain += w * (v - have)          # what it newly proves
                    if have > 0.0:
                        gain += w * have * corr     # ...and corroborates
                elif v > 0.0:
                    gain += w * v * corr            # pure corroboration
            #  gain is on the idf mass scale; normalise so score_weight is comparable
            val = (gain / total_mass) + sw * (_score(p) / hi)
            if pick_val is None or val > pick_val:
                pick, pick_val, pick_gain = p, val, gain
        chosen.append(pick)
        gains.append(round(pick_gain, 3))
        for d, v in q[pick].items():
            if v > best.get(d, 0.0):
                best[d] = v
        remaining.discard(pick)
    return chosen + tail, gains


def guarantee(order, by_pub, must_cover, window=60):
    """Promote the best discloser of every `must_cover` item that no visible card discloses.

    -> {"order": permutation, "promoted": {pub: item}} or None when nothing needed promoting.

    WHY A SEPARATE PASS AND NOT A BIGGER WEIGHT. Greedy maximum-coverage optimises TOTAL mass, so
    it will always prefer a document covering three mid-rarity items to one covering a single rare
    one, however the weights are set — and the single rare one is the reference an attorney needs,
    because it is the only art there is for that claim. Raising the weight until greedy picks it
    distorts every other position; promoting it afterwards changes exactly one thing.

    Order within the window is preserved; a promoted reference is inserted at the end of it, so
    the head of the report is untouched and the tail of the visible page becomes the answers
    nothing else on the page gives.
    """
    must = [m for m in (must_cover or []) if m]
    if not must or not order:
        return None
    strength = {p: quality(by_pub[p]) for p in order if p in by_pub}
    visible = set(order[:window])
    covered = set()
    for p in order[:window]:
        for item, v in strength.get(p, {}).items():
            if v > 0.0:
                covered.add(item)
    missing = [m for m in must if m not in covered]
    if not missing:
        return None
    #  Best discloser anywhere in the ranked list, strongest verdict first, then earliest rank —
    #  so a promotion brings in the most defensible evidence, not merely the first one found.
    rank_of = {p: i for i, p in enumerate(order)}
    promoted = {}
    for item in missing:
        best, best_key = None, None
        for p, q in strength.items():
            v = q.get(item, 0.0)
            if v <= 0.0 or p in visible:
                continue
            key = (-v, rank_of.get(p, 10 ** 9))
            if best_key is None or key < best_key:
                best, best_key = p, key
        if best is None:
            continue                       # nothing in the whole run discloses it; that is a finding
        promoted[best] = item
        visible.add(best)
        #  One promotion can answer several missing items at once.
        for it, v in strength.get(best, {}).items():
            if v > 0.0:
                covered.add(it)
    if not promoted:
        return None
    #  MAKE ROOM. Appending the promotions after the window put them at positions 61+ — just
    #  outside the page they were promoted to reach, which is the whole bug this pass exists to
    #  fix, reintroduced one line later. They go at the END of the window and displace the
    #  weakest cards that were in it, which are by construction the ones adding least.
    head = [p for p in order[:window] if p not in promoted]
    room = max(0, window - len(promoted))
    keep, displaced = head[:room], head[room:]
    tail = [p for p in order[window:] if p not in promoted]
    return {"order": keep + list(promoted.keys()) + displaced + tail, "promoted": promoted}


def covered_mass(order, by_pub, idf, cut=50):
    """(weighted disclosure mass the top `cut` covers, total mass, documents adding nothing).

    The product metric. A report that covers more of the invention with the same fifty slots is
    strictly more useful, whatever it does to any recall number.
    """
    idf = idf or {}
    total = sum(idf.values()) or 0.0
    best, dead = {}, 0
    for p in order[:cut]:
        e = by_pub.get(p)
        if not e:
            continue
        new = 0.0
        for d, v in quality(e).items():
            w = idf.get(d, 0.0)
            if v > best.get(d, 0.0):
                new += w * (v - best.get(d, 0.0))
                best[d] = v
        if new < 0.01:
            dead += 1
    return sum(idf.get(d, 0.0) * v for d, v in best.items()), total, dead


# ---------------------------------------------------------------------------
# the page an attorney would want: every claim answered, strongest answers first
# ---------------------------------------------------------------------------
#  A reference "plausibly discloses" a claim when it grounds at least one of that claim's
#  limitations at `disclosed` or `partial`. Deliberately not `uncertain`: an uncertain is a
#  disclosed that an independent refuter would not confirm, and putting one forward as the answer
#  to a claim is how a report ends up asserting something it cannot defend.
PLAUSIBLE = float(os.environ.get("COVERAGE_PLAUSIBLE_MIN", "0.55"))


def claim_of(item) -> str:
    """"claim 12[c]" -> "claim 12". A limitation id carries its claim, so nothing has to be looked
    up to go from one to the other."""
    s = str(item or "")
    i = s.find("[")
    return (s[:i] if i > 0 else s).strip()


def claims_disclosed(entry, minimum=PLAUSIBLE) -> set:
    """The CLAIMS this reference plausibly answers, from its grounded limitation cells."""
    out = set()
    for item, v in quality(entry).items():
        if v >= minimum:
            c = claim_of(item)
            if c:
                out.add(c)
    return out


def claim_first(order, by_pub, claims, window=60, minimum=PLAUSIBLE):
    """Reorder the visible page so the documents that kill the most CLAIMS lead it, then make sure
    every claim has at least one plausible answer on it.

    -> {"order", "promoted": {pub: claim}, "per_claim": {claim: n on the page}} or None.

    WHY CLAIMS AND NOT LIMITATIONS. The greedy coverage pass above optimises limitation mass, which
    is the right objective for "how much of the invention is answered" and the wrong one for "which
    document should I read first". An attorney reads down a list looking for the single reference
    that takes out the most claims — that is what a 102 rejection is built from, and it is what the
    examiner in the measured docket did: one document, thirteen claims. A document answering one
    limitation of each of five claims is worth more to that argument than one answering five
    limitations of a single claim, and limitation mass cannot tell them apart.

    WHY A SEPARATE PASS. Same reason `guarantee` is: weighting claim count inside the greedy score
    would distort every other position to fix the head. This reorders the window and nothing else.
    """
    claims = sorted({claim_of(c) for c in (claims or []) if claim_of(c)})
    if not claims or not order:
        return None
    strength = {p: claims_disclosed(by_pub[p], minimum) for p in order if p in by_pub}
    rank_of = {p: i for i, p in enumerate(order)}

    #  1. STRONGEST FIRST, inside the window. Ties keep their existing order, so the greedy
    #     coverage result survives wherever claim counts are equal and this is a refinement of it
    #     rather than a replacement.
    head = [p for p in order[:window]]
    head.sort(key=lambda p: (-len(strength.get(p, ())), rank_of.get(p, 10 ** 9)))
    tail = list(order[window:])

    #  2. EVERY CLAIM GETS AN ANSWER. A claim with nothing on the page is the one an attorney
    #     cannot argue at all, and it is invisible: an empty row reads as "no such art exists"
    #     rather than "the page was full".
    on_page = set()
    for p in head:
        on_page |= strength.get(p, set())
    missing = [c for c in claims if c not in on_page]
    promoted = {}
    for c in missing:
        best, key = None, None
        for p, cs in strength.items():
            if c not in cs or p in head or p in promoted:
                continue
            #  The reference that answers the most claims overall, then the best-ranked one: a
            #  promotion should bring in the most useful document that answers this claim, not
            #  merely the first one found.
            k = (-len(cs), rank_of.get(p, 10 ** 9))
            if key is None or k < key:
                best, key = p, k
        if best is None:
            continue                     # nothing in the whole run answers it; that is a finding
        promoted[best] = c
        on_page |= strength.get(best, set())

    if promoted:
        #  MAKE ROOM by displacing the weakest cards in the window, never by appending past it —
        #  appending puts a promotion at position window+1, just outside the page it was promoted
        #  to reach, which is the bug this codebase has already had once.
        keep = [p for p in head if p not in promoted]
        room = max(0, window - len(promoted))
        head, displaced = keep[:room] + list(promoted), keep[room:]
        tail = displaced + [p for p in tail if p not in promoted]

    per_claim = {c: sum(1 for p in head if c in strength.get(p, ())) for c in claims}
    return {"order": head + tail, "promoted": promoted, "per_claim": per_claim,
            "claims_answered": sum(1 for c in claims if per_claim[c] > 0), "n_claims": len(claims)}
