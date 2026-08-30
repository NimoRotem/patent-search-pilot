"""Order the read references by COMPARING them, never by scoring them.

MEASURED AND REFUTED, 2026-08-05. NOT WIRED INTO THE PIPELINE. Kept, with its harness, so the
idea is not proposed again without this evidence.

    eval/order_sweep.py, six subjects, 69 cited families, pointwise baseline 10

        criteria          0.00  0.15  0.30  0.50  0.75  1.00      <- weight on the tournament
        mixed               10    10    11    10     9     7
        coverage-only       10     9     8    10     9    11
        novelty-only        10     9    10    10    11    11

    Eighteen variants. The best is 11 against a baseline of 10, and the run-to-run variance on
    this benchmark is +/-2 families, so taking the maximum over eighteen variants and calling +1 a
    gain is a multiple-comparisons artefact, not a result. Replacing the score outright (share
    1.00, mixed criteria) is 7: clearly worse.

    WHY IT LOSES, which is the part worth keeping. The pointwise score is grounded in MEASURED
    feature coverage with located, grounded quotes. A comparison is an impression, and an
    impression favours the document that LOOKS like the invention -- same product category, same
    vocabulary -- which is precisely the bias that buries art from a neighbouring field. Swapping
    evidence for impression is a downgrade however the votes are counted.

THE ORIGINAL REASONING, which was sound and still did not survive contact
-------------------------------------------------------------------------
deep_rank is a pointwise ranker: it scores each reference alone, against an abstract standard, by
a model that never sees the alternatives. Two of its three terms (`overall`, `screen`) are
absolute LLM judgements, and absolute LLM judgements are not calibrated across calls. That was
measured directly on this pipeline: the same publication scored 85, then 60, then 75, purely from
which other candidates shared its batch. Sorting by uncalibrated numbers sorts partly by noise.

The cost is visible in the benchmark. Of 69 cited families this corpus holds, 33 were retrieved
and not shown, and 15 of those were READ IN FULL, cover to cover, with their features charted and
quoted. The evidence was gathered and the ordering wasted it.

A relative judgement does not need calibration. "Which of these six is closer prior art" has a
stable answer that does not depend on what the model currently thinks 75 means. So this module
never asks for a score. It runs a Swiss-style tournament: small groups, each ranked internally,
points awarded by within-group position, regrouped by running total, repeated.

AND IT ASKS A DIFFERENT QUESTION EACH ROUND
-------------------------------------------
A single criterion, applied repeatedly, just re-expresses one opinion. Each round therefore judges
a different aspect of prior-art relevance, and the standings are the consensus across them:

    round 1   which discloses MORE OF THE INVENTION'S FEATURES, on the evidence quoted
    round 2   which would an examiner cite FIRST AGAINST CLAIM 1, i.e. novelty
    round 3   which is closest in TECHNICAL MECHANISM, whatever it is applied to

Documents that win on every question rise; documents that win on one stay in contention, which is
what a claim chart needs, because a reference that anticipates one element decisively is worth
showing next to one that is broadly similar.

Bounded on purpose: only the head of the list is re-ordered (TOURNAMENT_TOP), because the tail
cannot reach the page whatever it scores, and the LLM budget belongs where the decision is.
"""
from __future__ import annotations

import json
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor

import llm

#  How many of the already-read references enter the tournament. The page shows 50; ranking the
#  top 150 is enough to decide those 50 while leaving room for a document to climb a long way.
TOURNAMENT_TOP = int(os.environ.get("TOURNAMENT_TOP", "150"))
#  References compared in one call. Small enough that the model can hold every card in view and
#  give a real ordering, large enough that a round is a handful of calls.
GROUP = int(os.environ.get("TOURNAMENT_GROUP", "6"))
ROUNDS = int(os.environ.get("TOURNAMENT_ROUNDS", "3"))
WORKERS = int(os.environ.get("TOURNAMENT_WORKERS", "8"))
#  Chars of evidence per reference in a comparison card.
CARD_CHARS = int(os.environ.get("TOURNAMENT_CARD_CHARS", "900"))
MAX_FEATURES_SHOWN = 6

#  One question per round. Order matters: the first is the one the report is fundamentally about,
#  so it breaks ties in the final standings.
CRITERIA = [
    ("feature coverage",
     "Which of these references DISCLOSES MORE OF THE INVENTION'S FEATURES? Judge only on the "
     "quoted evidence shown; a confident quote for a rare feature is worth more than several for "
     "obvious ones."),
    ("novelty against claim 1",
     "Which of these would a patent examiner cite FIRST against claim 1 of the invention? That is "
     "the reference that comes closest to disclosing the claim as a whole, in one document."),
    ("technical mechanism",
     "Which of these is closest in TECHNICAL MECHANISM to the invention -- the same physical "
     "principle, structure or method -- regardless of what it is applied to or what industry it "
     "comes from?"),
]

_SYS = (
    "You are a patent examiner ordering candidate prior-art references against one invention.\n\n"
    "You will be given the invention's features and several candidate references, each with the "
    "evidence a reader already extracted from its full text: which features it was found to "
    "disclose, and verbatim quotes.\n\n"
    "RANK THEM. Do not score them. Return the reference ids from MOST to LEAST relevant.\n\n"
    "Judge on the evidence shown, not on how familiar the wording sounds. A reference whose "
    "quotes show it actually doing the thing beats one that merely uses the same vocabulary.\n\n"
    'Return ONLY JSON: {"order":[<id>,<id>,...]}. Every id given to you must appear exactly once.'
    "\n\nReturn NOTHING ELSE. No explanation, no commentary: a free-text field of unbounded length "
    "is what truncates the JSON and loses the whole comparison."
)


def card(pub: str, d: dict, idx: int) -> str:
    """One reference as the comparator sees it: what it was measured to disclose, with quotes."""
    rows = [c for c in (d.get("covered") or [])
            if c.get("verdict") in ("disclosed", "partial")]
    rows.sort(key=lambda c: (-(c.get("idf") or 0)))
    if not rows:
        rows = (d.get("covered") or [])[:2]
    lines = []
    for c in rows[:MAX_FEATURES_SHOWN]:
        q = " ".join(str(c.get("quote") or "").split())[:200]
        lines.append(f"    - {c.get('item')}: {c.get('verdict')}"
                     + (f' "{q}"' if q else ""))
    body = "\n".join(lines) or "    (no grounded evidence)"
    title = " ".join(str(d.get("title") or "").split())[:110]
    head = (f"[{idx}] {pub} -- {title}\n"
            f"    read {int(d.get('chars_read') or 0):,} chars; "
            f"grounded {d.get('n_disclosed', 0)} disclosed / {d.get('n_partial', 0)} partial "
            f"of {d.get('n_features', 0)} features")
    return (head + "\n" + body)[:CARD_CHARS]


def _ask(features, cards, criterion, ids, retries=1):
    """One group comparison -> ordered ids, or None if the model did not answer.

    None, NOT the incoming order. A failed comparison that returns its input looks like a real
    result to the caller, which then awards Borda points by incoming position -- and that actively
    SCRAMBLES the global ranking, because the top of group two ties with the top of group one.
    Measured: with the comparisons silently failing, the tournament turned 10 cited references in
    the top 50 into 6. A group that could not be judged must score nothing at all.
    """
    feat = "\n".join(f"  - {f}" for f in (features or [])[:14])
    user = (f"INVENTION FEATURES\n{feat}\n\nCRITERION\n{criterion}\n\n"
            f"CANDIDATES\n" + "\n\n".join(cards) +
            f"\n\nRank these {len(ids)} ids from most to least relevant. JSON only.")
    for _ in range(retries + 1):
        out = llm.chat_json(_SYS, user, max_tokens=400) or {}
        got = [str(x).strip() for x in (out.get("order") or [])]
        seen, order = set(), []
        for g in got:
            m = re.search(r"\d+", g)
            key = m.group(0) if m else g
            for i in ids:
                if str(i) == key and i not in seen:
                    seen.add(i)
                    order.append(i)
        #  A partial answer is still usable; a wholly empty one is not.
        if order:
            return order + [i for i in ids if i not in seen]
    return None


def rank_with_points(features, by_pub, order, top=None, rounds=None, group=None,
                     criteria=None, on_progress=None):
    """The tournament, returning (head, points) so a caller can BLEND rather than replace.

    Replacing the pointwise score outright is measurably worse (see the module docstring), which is
    why this exists: the pointwise score is grounded in measured feature coverage with located
    quotes, and a comparison is an impression. The useful question is whether the impression adds
    anything ON TOP of the evidence, not whether it can stand in for it.
    """
    top = top or TOURNAMENT_TOP
    rounds = rounds or ROUNDS
    group = group or GROUP
    crit = criteria or CRITERIA
    head = [p for p in (order or []) if p in by_pub][:top]
    if len(head) <= group:
        return head, {}
    points = {p: 0.0 for p in head}
    pos = {p: i for i, p in enumerate(order)}
    try:
        for rnd in range(rounds):
            name, criterion = crit[rnd % len(crit)]
            ranked = sorted(head, key=lambda p: (-points[p], pos[p]))
            groups = [ranked[i:i + group] for i in range(0, len(ranked), group)]
            groups = [g for g in groups if len(g) > 1]

            def one(g):
                ids = list(range(len(g)))
                cards = [card(p, by_pub[p], i) for i, p in enumerate(g)]
                return g, _ask(features, cards, criterion, ids)

            judged = 0
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for g, won in ex.map(one, groups):
                    if won is None:
                        continue
                    judged += 1
                    n = len(g)
                    for place, idx in enumerate(won):
                        points[g[idx]] += (n - 1 - place)
            print(f"[tournament] round {rnd + 1}/{rounds} ({name}): "
                  f"{judged}/{len(groups)} groups judged", flush=True)
            if judged < len(groups) * 0.5:
                print("[tournament] too many comparisons failed; abandoning", flush=True)
                return head, {}
            if on_progress:
                try:
                    on_progress("tournament_round",
                                {"round": rnd + 1, "of": rounds, "criterion": name,
                                 "groups": len(groups), "judged": judged})
                except Exception:
                    pass
    except Exception:
        traceback.print_exc()
        return head, {}
    return head, points


def blend(order, head, points, share=0.5):
    """Re-order `head` by mixing its POINTWISE rank with its TOURNAMENT rank.

    Mixed by RANK, not by raw value: a Borda point total and an evidence score are on different
    scales and neither is calibrated against the other. `share` is the weight on the tournament,
    so 0.0 is the pointwise order untouched and 1.0 is the tournament alone.
    """
    if not points:
        return list(order)
    pos = {p: i for i, p in enumerate(order)}
    tour = {p: i for i, p in enumerate(sorted(head, key=lambda p: (-points[p], pos[p])))}
    new_head = sorted(head, key=lambda p: share * tour[p] + (1 - share) * pos[p])
    return new_head + [p for p in order if p not in set(head)]


def rank(features, by_pub, order, top=None, rounds=None, group=None, on_progress=None):
    """Swiss tournament over the head of `order`. -> re-ordered list of publication numbers.

    Never raises: any failure leaves the incoming order untouched, because a pointwise order is
    still a real order and losing the report to a ranking experiment would be worse than a
    mediocre ranking.
    """
    top = top or TOURNAMENT_TOP
    rounds = rounds or ROUNDS
    group = group or GROUP
    head = [p for p in (order or []) if p in by_pub][:top]
    tail = [p for p in (order or []) if p not in head]
    if len(head) <= group:
        return list(order or [])

    points = {p: 0.0 for p in head}
    #  index by position so the model handles short ids, never long publication numbers
    try:
        for rnd in range(rounds):
            name, criterion = CRITERIA[rnd % len(CRITERIA)]
            #  regroup by running total so evenly-matched references meet (Swiss pairing)
            ranked = sorted(head, key=lambda p: (-points[p], order.index(p)))
            groups = [ranked[i:i + group] for i in range(0, len(ranked), group)]
            groups = [g for g in groups if len(g) > 1]

            def one(g):
                ids = list(range(len(g)))
                cards = [card(p, by_pub[p], i) for i, p in enumerate(g)]
                won = _ask(features, cards, criterion, ids)
                return g, won

            judged = 0
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for g, won in ex.map(one, groups):
                    if won is None:
                        continue          # unjudged: score nothing, never positional points
                    judged += 1
                    n = len(g)
                    for place, idx in enumerate(won):
                        #  Borda: first in a group of 6 gets 5 points, last gets 0
                        points[g[idx]] += (n - 1 - place)
            print(f"[tournament] round {rnd + 1}/{rounds} ({name}): "
                  f"{judged}/{len(groups)} groups judged", flush=True)
            if judged < len(groups) * 0.5:
                #  More than half the comparisons failed: the standings are mostly the pointwise
                #  order with noise on top, which is worse than the pointwise order.
                print("[tournament] too many comparisons failed; keeping the pointwise order",
                      flush=True)
                return list(order or [])
            if on_progress:
                try:
                    on_progress("tournament_round",
                                {"round": rnd + 1, "of": rounds, "criterion": name,
                                 "groups": len(groups), "judged": judged})
                except Exception:
                    pass
    except Exception:
        traceback.print_exc()
        return list(order or [])

    #  Ties broken by the pointwise order, so the tournament only ever REARRANGES what it judged.
    pos = {p: i for i, p in enumerate(order)}
    head.sort(key=lambda p: (-points[p], pos[p]))
    return head + tail


def standings(features, by_pub, order, **kw):
    """rank(), plus the points, for diagnostics and for the report's ranking rationale."""
    out = rank(features, by_pub, order, **kw)
    return out
