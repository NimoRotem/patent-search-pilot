"""How close the art a search found comes to the claims the draft is making.

WHY A NUMBER AT ALL. "Re-search" is only worth doing repeatedly if each round leaves the draft
further from the art than the last one, and nobody can see that from a list of publication numbers.
The claim chart a report already builds carries exactly what is needed: for every element of every
independent claim, whether a given reference DISCLOSES it, and on what evidence. Reduce that to one
honest figure per round and the loop becomes measurable instead of a matter of faith.

THE FIGURE IS THE NEAREST SINGLE REFERENCE, not an average over the field. Novelty under 102 is
decided one reference at a time: a claim falls because ONE document discloses every element of it,
not because ten documents between them do. So the headline is the largest fraction of the
independent claims' elements that any single reference discloses, and a round that finds ten
mediocre references is correctly reported as no worse than one that found three.

WHAT IT IS NOT. It is not a patentability opinion and must never be shown as one. It measures what
this search found and read, on this draft, at this moment; a later search with a different query
can and does find something closer. Obviousness over a combination is a different question
entirely, and `combination` below reports it separately rather than folding it into the headline.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

#  webview renders a verified disclosure as this glyph. A cell that merely "teaches" or is
#  "unchecked" is deliberately NOT counted: the point of the number is how much of the claim is
#  actually shown in one document, and a partial read is not a disclosure.
DISCLOSES = "discloses"


def _rows_of_interest(chart: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The claim elements a novelty reading is about.

    Independent claims only, because a dependent claim cannot survive its parent, and never the
    preamble: "an apparatus comprising" is disclosed by every document in the field and counting it
    would put a floor under every score that has nothing to do with the invention.
    """
    rows = [dict(row) for row in (chart.get("rows") or []) if isinstance(row, Mapping)]
    body = [row for row in rows if not row.get("preamble")]
    independent = [row for row in body if row.get("independent")]
    #  A Type A search (no subject claims) charts features rather than claims and marks nothing
    #  independent. Falling back to every non-preamble row keeps the reading meaningful there
    #  rather than dividing by zero and reporting a spurious 0.0.
    return independent or body


def coverage_by_reference(chart: Mapping[str, Any]) -> dict[str, float]:
    """Fraction of the claim elements each charted reference actually discloses."""
    rows = _rows_of_interest(chart)
    if not rows:
        return {}
    out: dict[str, float] = {}
    for column in chart.get("columns") or []:
        pub = str((column or {}).get("pub") or "")
        if not pub:
            continue
        hits = 0
        for row in rows:
            cell = next((c for c in row.get("cells") or []
                         if isinstance(c, Mapping) and c.get("pub") == pub), None)
            if cell and cell.get("covered") and cell.get("verify") == DISCLOSES:
                hits += 1
        out[pub] = hits / len(rows)
    return out


def reading(chart: Mapping[str, Any] | None,
            titles: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One round's novelty reading, or an explicitly empty one when nothing was charted."""
    chart = chart or {}
    rows = _rows_of_interest(chart)
    per_reference = coverage_by_reference(chart)
    if not rows or not per_reference:
        return {"ok": False, "n_elements": len(rows), "n_charted": 0,
                "closest_coverage": None, "mean_top3": None, "closest_pub": "",
                "closest_title": "", "per_reference": {}, "uncovered_elements": [],
                "combination": None,
                "detail": "The search charted no reference against these claims."}
    ranked = sorted(per_reference.items(), key=lambda kv: (-kv[1], kv[0]))
    top3 = [value for _pub, value in ranked[:3]]
    closest_pub, closest = ranked[0]
    titles = titles or {}
    #  Elements NO reference disclosed. This is the half a drafter acts on: it names the features
    #  the claims can be anchored to, so it is reported alongside the score rather than derived
    #  from it later.
    uncovered = []
    for row in rows:
        if not any(cell.get("covered") and cell.get("verify") == DISCLOSES
                   for cell in row.get("cells") or [] if isinstance(cell, Mapping)):
            uncovered.append(str(row.get("element") or "")[:300])
    #  Obviousness is a different question from novelty and is kept separate on purpose: the union
    #  of every reference is always at least as large as the best single one, so folding it into
    #  the headline would make the number impossible to move.
    union = 0
    for row in rows:
        if any(cell.get("covered") and cell.get("verify") == DISCLOSES
               for cell in row.get("cells") or [] if isinstance(cell, Mapping)):
            union += 1
    return {
        "ok": True,
        "n_elements": len(rows),
        "n_charted": len(per_reference),
        "closest_coverage": round(closest, 4),
        "mean_top3": round(sum(top3) / len(top3), 4),
        "closest_pub": closest_pub,
        "closest_title": str(titles.get(closest_pub) or "")[:200],
        "per_reference": {pub: round(value, 4) for pub, value in ranked[:20]},
        "uncovered_elements": uncovered[:40],
        "combination": round(union / len(rows), 4),
        "detail": (f"The nearest single reference discloses {int(round(closest * len(rows)))} of "
                   f"{len(rows)} independent-claim elements."),
    }


def titles_from_view(view: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for column in ((view.get("claim_chart") or {}).get("columns") or []):
        if column.get("pub"):
            out[str(column["pub"])] = str(column.get("title") or "")
    for card in view.get("cards") or []:
        if card.get("pub") and card["pub"] not in out:
            out[str(card["pub"])] = str(card.get("title") or "")
    return out


def read_view(view: Mapping[str, Any] | None) -> dict[str, Any]:
    view = view or {}
    return reading((view.get("claim_chart") or {}), titles_from_view(view))


def improvement(previous: Mapping[str, Any] | None,
                current: Mapping[str, Any] | None) -> dict[str, Any]:
    """Did this round leave the draft further from the art than the one before it?

    Deliberately reports "not comparable" rather than a number when either round charted nothing:
    a round that found no art is not evidence that the claims got broader OR narrower, and saying
    so is more useful than a zero that reads like a result.
    """
    before = (previous or {}).get("closest_coverage")
    after = (current or {}).get("closest_coverage")
    if before is None or after is None:
        return {"comparable": False, "delta": None,
                "verdict": "One of these rounds charted no reference against the claims."}
    delta = round(float(after) - float(before), 4)
    if delta < -0.001:
        verdict = "The nearest reference is further from the claims than it was last round."
    elif delta > 0.001:
        verdict = ("This round found art CLOSER to the claims than the last round did. The draft "
                   "has not moved away from the field yet.")
    else:
        verdict = "The nearest reference is as close as it was last round."
    return {"comparable": True, "delta": delta, "verdict": verdict}
