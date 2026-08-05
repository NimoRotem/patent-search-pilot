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
