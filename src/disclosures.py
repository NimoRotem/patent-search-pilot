"""The list of things the subject DISCLOSES, at the granularity a legal argument is made at.

WHY THE OLD LIST WAS TOO COARSE
-------------------------------
Every prior-art search exists to answer, disclosure by disclosure, "was this already known, and
where". The pipeline was answering that against 11 or 12 decomposed features -- a summary of the
invention, not its disclosures. Measured on a finished report, that set is saturated by the ninth
displayed document: the top 50 covered 87% of the weighted mass and 42 of the 50 slots added
nothing at all, because after nine documents there was nothing left to add.

That is what blocks ranking by CONTRIBUTION rather than by similarity (coverage_rank). A marginal
ranker needs something left to be marginal about. With 12 disclosures there is nothing; with the
80-odd a real patent actually discloses there is a great deal.

WHAT A DISCLOSURE IS HERE
-------------------------
Three sources, because a search has to answer for all of them:

  CLAIM LIMITATION    each separately checkable limitation of each independent claim, and the
                      limitation each dependent claim ADDS. These decide novelty and are what an
                      examiner's X and Y citations are written against.
  COMBINATION         an independent claim taken whole. A reference disclosing every limitation
                      separately is not the same as one disclosing the claim, and the difference
                      is the entire novelty argument.
  POTENTIAL CLAIM     a teaching the description supports that the claims do NOT cover. Claims are
                      drafted narrowly and amended; a search that only checks the claims as
                      granted goes blind the moment they are amended, and misses the art that
                      matters for a continuation or an opposition. These are the "claims that
                      could have been drafted".

Each disclosure carries its provenance and a weight, because they are not equally load-bearing:
an independent claim's limitation decides validity, a potential claim is contingency.
"""
from __future__ import annotations

import json
import os
import re
import traceback

import llm

#  Weights by kind. A limitation of an independent claim is what validity turns on; a potential
#  claim is insurance. These multiply the rarity weight, they do not replace it.
KIND_WEIGHT = {
    "independent_limitation": 1.00,
    "combination": 0.90,
    "dependent_limitation": 0.70,
    "potential_claim": 0.55,
}
MAX_DISCLOSURES = int(os.environ.get("DISCLOSURES_MAX", "90"))
#  Chars of source given to the extractor. Claims first, then description.
MAX_CLAIMS_CHARS = int(os.environ.get("DISCLOSURES_CLAIM_CHARS", "22000"))
MAX_DESC_CHARS = int(os.environ.get("DISCLOSURES_DESC_CHARS", "26000"))

_SYS = (
    "You are a patent attorney building the checklist a prior-art search will be run against.\n\n"
    "You will be given a patent's CLAIMS and part of its DESCRIPTION. Produce the list of things "
    "this document DISCLOSES, each one written so that a reader holding another patent can answer "
    "yes or no: does that other document disclose THIS.\n\n"
    "Four kinds, and you must produce all four:\n"
    "  independent_limitation  one separately checkable limitation of an INDEPENDENT claim. Split "
    "each independent claim into its limitations; do not merge them. This is where novelty is "
    "decided.\n"
    "  combination             one whole independent claim, stated as a single combined "
    "requirement. A reference disclosing every limitation separately is NOT the same as one "
    "disclosing the claim as a whole, and that difference is the novelty argument.\n"
    "  dependent_limitation    the limitation each DEPENDENT claim ADDS to its parent. Just the "
    "addition, not the inherited parent text.\n"
    "  potential_claim         something the DESCRIPTION supports that the claims do not cover: a "
    "feature, a variant, a range, a material, a method step that a drafter could have claimed and "
    "did not. These matter because claims get amended, and a search that only checked the granted "
    "claims goes blind the moment they are.\n\n"
    "RULES.\n"
    "1. Each `text` is ONE technical requirement, 5 to 25 words, in plain terms a searcher can "
    "check against another document. Not a claim number, not a quotation of legalese.\n"
    "2. No duplicates and no near-duplicates. If two claims add the same thing, emit it once.\n"
    "3. Do not invent. A potential_claim must be supported by the description you were given.\n"
    "4. Prefer the specific to the generic: 'sealing lip deflects inward under vacuum' beats "
    "'a sealing element'. A disclosure every patent in the field would satisfy is useless.\n\n"
    'Return ONLY JSON: {"disclosures":[{"text":"...","kind":"...","source":"claim 1"}]}'
)


def _clean(s, n=240):
    return " ".join(str(s or "").split())[:n]


def _claims_text(claims) -> str:
    """Claims as numbered text, from either [{claim_no,text}] or [str]."""
    out = []
    for i, c in enumerate(claims or [], 1):
        if isinstance(c, dict):
            no = c.get("claim_no") or i
            t = c.get("text") or ""
        else:
            no, t = i, str(c)
        t = " ".join(str(t).split())
        if t:
            out.append(f"{no}. {t}")
    return "\n".join(out)


def extract(claims=None, description: str = "", title: str = "",
            max_disclosures: int = None) -> list:
    """-> [{text, kind, source, weight}] . Fail-soft: [] when nothing usable comes back.

    Callers must treat [] as "fall back to the old element list", never as "this patent discloses
    nothing".
    """
    cap = max_disclosures or MAX_DISCLOSURES
    ctext = _claims_text(claims)[:MAX_CLAIMS_CHARS]
    desc = " ".join(str(description or "").split())[:MAX_DESC_CHARS]
    if not ctext and not desc:
        return []
    user = (f"TITLE\n{_clean(title, 200)}\n\nCLAIMS\n{ctext or '(none supplied)'}\n\n"
            f"DESCRIPTION (extract)\n{desc or '(none supplied)'}")
    try:
        out = llm.chat_json(_SYS, user, max_tokens=6000) or {}
    except Exception:
        traceback.print_exc()
        return []

    seen, disclosures = set(), []
    for d in (out.get("disclosures") or []):
        if not isinstance(d, dict):
            continue
        text = _clean(d.get("text"))
        if len(text) < 8:
            continue
        key = re.sub(r"[^a-z0-9 ]", "", text.lower())[:70]
        if key in seen:
            continue
        seen.add(key)
        kind = str(d.get("kind") or "").strip()
        if kind not in KIND_WEIGHT:
            kind = "potential_claim"
        disclosures.append({"text": text, "kind": kind,
                            "source": _clean(d.get("source"), 40),
                            "weight": KIND_WEIGHT[kind]})
        if len(disclosures) >= cap:
            break
    #  Independent-claim material first: it is what the report is fundamentally arguing about, and
    #  if anything downstream truncates the list that is what must survive.
    disclosures.sort(key=lambda d: -d["weight"])
    return disclosures


def summary(disclosures) -> dict:
    by = {}
    for d in disclosures or []:
        by[d["kind"]] = by.get(d["kind"], 0) + 1
    return {"n": len(disclosures or []), "by_kind": by}


def texts(disclosures) -> list:
    """Just the checkable statements, for the charting stage."""
    return [d["text"] for d in (disclosures or [])]


def weight_map(disclosures) -> dict:
    """{text: kind weight} so the ranker can multiply rarity by legal load."""
    return {d["text"]: d["weight"] for d in (disclosures or [])}
