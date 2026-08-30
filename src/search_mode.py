"""The two searches this tool offers, and the fact that they are not the same search.

WHY THIS EXISTS
---------------
There was ONE pipeline with a depth knob, and the knob was a lie. Every search, whatever it was
for, paid for the machinery a third-party submission needs: the claim ledger, the limitation
split, the USPTO file wrapper, concept expansion per feature, claim reach, enrichment of
references nobody was going to read. Measured on real runs, the wait before the FIRST document is
read was 161 s, of which roughly 85 s was preparation whose only consumer is the reading stage:

    retrieval fan-out, dossier, disclosures ....... 66-84 s
    screening 600 candidates ..................... 35 s
    enrichment, concept expansion, claim reach ... 43-60 s

A person who typed a paragraph and wanted to see the closest art waited for all of it. And the
funnel it fed was 5,903 candidate families retrieved, 2,502 screened, 143 read, 60 delivered: 58%
of retrieval discarded without being examined, because it was sized for a reader that only ever
consumes the head.

So there are two products here, and they are now two pipelines.

FAST (the default)
    Any input: a description, a draft, a published patent. Reduced to a handful of short search
    descriptions, retrieved on a limited channel set, fused and cross-encoder reranked. That is
    all. No claim ledger, no reading, no dossier, no concept expansion, no claim reach, no
    enrichment, and no submission machinery on the results page. What it owes the reader is a
    strong top 20, quickly.

ATTACK (the third-party build)
    Requires claims, and refuses to start without them. Expands from the claims themselves, their
    limitations, the combinations that carry the novelty, citations and concepts; retrieves wide
    enough for recall; then reads documents in full and reasons over what they disclose. It is
    allowed to take as long as it takes, because coverage of every claim is the deliverable.

    ITS SETTINGS ARE ASKED FOR BEFORE IT STARTS. They used to sit on the results page, so the
    decision that governs an hour of work was made after the first twenty minutes of it.

NOT A LADDER. Attack does not resume a Fast run and Fast is not a truncated Attack. Attack starts
from the claims and does its own retrieval, because the queries it needs are derived from claim
language that Fast never looks at.
"""
from __future__ import annotations

import os

FAST = "fast"
ATTACK = "attack"
MODES = (FAST, ATTACK)

#  What the form and the report call them.
LABEL = {FAST: "Prior-art search",
         ATTACK: "Third-party build"}
BLURB = {
    FAST: "Any description, draft or published patent. The closest prior art, ranked, in about "
          "half a minute. No claim analysis and nothing is read in full.",
    ATTACK: "Needs a draft or application with claims. Finds the art that attacks each claim, "
            "reads the strongest references end to end, and builds the 37 CFR 1.290 papers. "
            "Minutes to hours, depending on how much you ask it to read.",
}

#  ---- ATTACK's settings, asked before the run ------------------------------------------------
#  Every one of these used to be decided after the search had started, or not asked at all.
JURISDICTIONS = ("US", "EP")
JURISDICTION_LABEL = {
    "US": "United States (35 U.S.C. 102)",
    "EP": "European (EPC Art. 54)",
}
JURISDICTION_NOTE = {
    "US": "Secret prior art under 102(a)(2) reaches any application that later published and "
          "names the United States, whatever office it was filed at.",
    "EP": "Art. 54(3) is strict: an unpublished earlier filing only counts against a European "
          "application if it is itself a European filing. Fewer documents qualify.",
}

READ_TOP_DEFAULT = int(os.environ.get("ATTACK_READ_TOP_DEFAULT", "45"))
READ_TOP_CHOICES = (20, 45, 100, 200, 400)


def normalise(value) -> str:
    """-> FAST | ATTACK. Anything unrecognised is FAST, which is the safe and cheap answer."""
    v = str(value or "").strip().lower()
    if v in (ATTACK, "claim_attack", "submission", "packet", "build", "1290"):
        return ATTACK
    return FAST


def requires_claims(mode) -> bool:
    return normalise(mode) == ATTACK


def refusal(mode, claims) -> str:
    """Why this mode cannot run on this input, or "".

    A REFUSAL, NOT A DEGRADE. Silently running the fast search when somebody asked for a claim
    attack would hand back a ranked list with no chart and no packet, which looks like the attack
    failing rather than never having started.
    """
    if not requires_claims(mode):
        return ""
    if claims:
        return ""
    return ("A third-party build works from the claims of a specific application, so it needs a "
            "draft or a published patent that carries them. Paste a patent number or upload the "
            "document, wait for the claims to be extracted, and start it again. To search from a "
            "description instead, use a prior-art search.")


def normalise_jurisdiction(value) -> str:
    v = str(value or "").strip().upper()
    return v if v in JURISDICTIONS else "US"


def normalise_read_top(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return READ_TOP_DEFAULT
    return max(10, min(n, 1000))


# ---- how each mode is spent ------------------------------------------------------------------
def depth_for(mode) -> str:
    """The pipeline depth this mode runs at. FAST has its own; ATTACK reads in full."""
    return "submission" if normalise(mode) == ATTACK else "fast"


def agent_kwargs(mode) -> dict:
    """The retrieval shape, as AgentConfig keyword arguments.

    FAST is 8 passes and no refinement round against ATTACK's ~50. The measured cost of a pass is
    2.9 s wide and 0.6 s narrow, so this is the difference between roughly 20 s and roughly 80 s
    of retrieval, and between one model call and thirty.
    """
    if normalise(mode) == ATTACK:
        return {}                                    # AgentConfig's own defaults: the full shape
    return {
        "max_rounds": 0,          # no refinement rounds: they added 5 to 15 families a pass
        "fast_mode": True,        # essence + a few alternates, no clusters, no limitations
        "elements_per_round": 0,
        "trim_element_channels": True,
        #  NO CROSS-ENCODER, AND THIS IS THE WHOLE OF WHY THE FAST SEARCH IS FAST.
        #
        #  MEASURED, same subject, same corpus, back to back:
        #      with the cross-encoder ... 31.5 s
        #      fusion only .............. 5.8 s
        #  So it was 82% of the run, to score TWELVE documents. And it is not the model load:
        #  scoring the same head a second time in the same process took 17.0 s against 21.2 s
        #  cold, because a 568M-parameter XLM-RoBERTa reading full text pairs on a CPU is simply
        #  that slow. At 25 documents it is 25 s and at 50 it is 39 s.
        #
        #  WHAT IT COSTS, measured on the same pair of runs: the top 20 by fusion and the top 20
        #  after reranking share 18 of 20 members, and the top 40 share 39 of 40. It reorders
        #  within the head rather than changing what is in it. Trading a reordering of the first
        #  page for five sixths of the wall clock is the right trade for a search whose whole
        #  promise is that it comes back before you look away.
        #
        #  It is still there for the attack, which is allowed to take as long as it takes.
        "final_rerank": False,
        "ground": True,
    }


def runs_deep(mode) -> bool:
    """Whether anything is read in full. False for FAST, and that is the whole point of FAST."""
    return normalise(mode) == ATTACK


def shows_claim_grid(mode) -> bool:
    return normalise(mode) == ATTACK


def offers_submission(mode) -> bool:
    return normalise(mode) == ATTACK


def describe(mode, *, read_top=None, jurisdiction=None, third_party=None,
             concept_expansions=None) -> dict:
    """What the report records about the run it came from, and what the page shows."""
    m = normalise(mode)
    out = {"mode": m, "label": LABEL[m], "blurb": BLURB[m],
           "reads_in_full": runs_deep(m), "claim_grid": shows_claim_grid(m)}
    if m == ATTACK:
        out.update({"read_top": normalise_read_top(read_top),
                    "jurisdiction": normalise_jurisdiction(jurisdiction),
                    "third_party": bool(third_party),
                    "concept_expansions": bool(concept_expansions)})
    return out
