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
#  Raised from 90 after three of the six benchmark subjects hit it exactly, which means the list
#  was a prefix rather than the subject's disclosures. validate() now reports truncation instead
#  of letting a truncated denominator pass silently.
MAX_DISCLOSURES = int(os.environ.get("DISCLOSURES_MAX", "160"))
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


def validate(items, had_claims: bool, had_description: bool) -> list:
    """Structural problems that make a disclosure list unusable as a metric denominator.

    -> [] when the list is sound, else a list of reasons.

    Measured on the first freeze of six benchmark subjects, all three of these fired:
        suction_chuck     0 disclosures returned despite 17,682 characters of description
        suction_display   90 disclosures, every one a potential_claim, no claim limitations
        three subjects    exactly MAX_DISCLOSURES, i.e. silently truncated

    A denominator that is empty, structurally impossible, or truncated is worse than no metric,
    because it still produces a number.
    """
    problems = []
    if not items:
        problems.append("EMPTY: the extractor returned nothing usable")
        return problems
    kinds = {}
    for d in items:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    if had_claims and not kinds.get("independent_limitation"):
        problems.append(
            f"NO_INDEPENDENT_LIMITATIONS: claims were supplied but none were decomposed "
            f"(kinds present: {sorted(kinds)})")
    if had_claims and not kinds.get("combination"):
        problems.append("NO_COMBINATION: no independent claim was stated as a whole")
    if had_description and not kinds.get("potential_claim"):
        problems.append("NO_POTENTIAL_CLAIMS: a description was supplied but nothing was found "
                        "in it that the claims do not cover")
    if len(items) >= MAX_DISCLOSURES:
        problems.append(f"TRUNCATED: hit the cap of {MAX_DISCLOSURES}, so the list is a prefix "
                        f"rather than the subject's disclosures")
    return problems


def extract(claims=None, description: str = "", title: str = "",
            max_disclosures: int = None, retries: int = 1, strict: bool = False) -> list:
    """-> [{text, kind, source, weight}] .

    `strict=True` RAISES on a structurally invalid list instead of returning it. Benchmark and
    freezing paths must use it: a denominator that is empty or has no claim limitations still
    produces a number, and a number from a broken checklist is worse than no metric at all.

    Fail-soft otherwise: [] means "fall back to the element list", never "this patent discloses
    nothing".
    """
    cap = max_disclosures or MAX_DISCLOSURES
    ctext = _claims_text(claims)[:MAX_CLAIMS_CHARS]
    desc = " ".join(str(description or "").split())[:MAX_DESC_CHARS]
    if not ctext and not desc:
        if strict:
            raise ValueError("no claims and no description supplied")
        return []
    user = (f"TITLE\n{_clean(title, 200)}\n\nCLAIMS\n{ctext or '(none supplied)'}\n\n"
            f"DESCRIPTION (extract)\n{desc or '(none supplied)'}")

    best, best_problems = [], []
    for attempt in range(max(1, retries + 1)):
        items = _extract_once(user, cap)
        problems = validate(items, bool(ctext), bool(desc))
        if not problems:
            return items
        #  `>` alone never fires when EVERY attempt returns zero items, so the placeholder reason
        #  survived and two subjects reported "never ran" instead of why they actually failed.
        if attempt == 0 or len(items) > len(best):
            best, best_problems = items, problems
    if strict:
        raise ValueError("; ".join(best_problems))
    print(f"[disclosures] returning a list with known problems: {'; '.join(best_problems)}",
          flush=True)
    return best


def _extract_once(user: str, cap: int) -> list:
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


#  Where eval/freeze_disclosures.py writes the frozen lists. Read from src/ so the pipeline can
#  load one without importing the eval harness.
FROZEN_DIR = os.environ.get(
    "DISCLOSURES_FROZEN_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "eval", "disclosures_frozen"))


def load_frozen(subject_id: str):
    """The frozen disclosure list for a benchmark subject, or None.

    A benchmark run must be scored against a denominator fixed BEFORE it ran. Generating the list
    during the run means two runs of one subject are measured against two different checklists,
    and a retrieval change moves the denominator underneath the numerator. Callers on the
    benchmark path must fail rather than fall back to generating.
    """
    if not subject_id:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(subject_id))[:64]
    p = os.path.join(FROZEN_DIR, f"{safe}.json")
    if not os.path.exists(p):
        return None
    try:
        rec = json.load(open(p))
    except Exception:
        traceback.print_exc()
        return None
    return rec if (rec.get("usable") and rec.get("disclosures")) else None


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
