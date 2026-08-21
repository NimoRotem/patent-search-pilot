"""WHAT KIND OF SEARCH THIS IS, decided from the input, and what it is allowed to spend.

THE MEASUREMENT THAT FORCED THIS
--------------------------------
Two inputs arrive at the same `/run`:

  A description of an invention. No claims exist yet, because the thing has not been drafted.
  A published or granted patent. Its claims exist, and each one is a separate legal requirement.

Until now both ran the same pipeline at the same budget, and the receipts say what that cost.
Measured on this corpus (app_saved_searches, 2026-08-18..21):

  document WITH claims   3,097 - 7,243 s end to end, one at 36,042 s
  typed description      the run live at 2026-08-21 18:58 was 3,932 s in and had reached
                         "fetching the full text of 400 references", with the reading still ahead

The description search is not doing the expensive work the number implies. `deep_rank` already
skips the claim machinery when there are no claims to split: on the one measured no-claims deep
run (adhoc-1d00e67ae6f1) charting took **72 s** against 1,375-5,067 s for the claims runs, and
the claim rescue -- 495-4,217 s on every claims run -- did not run at all.

So the hour goes on the stages that were never told the input changed: two full retrieval rounds,
a 2,500-candidate screen, a 400-document paid full-text fetch, and 150-180 documents read in full
against **eight to twelve concept phrases**. A claims run reads that deep because it owes an
answer for every limitation of every claim. A concept search owes coverage of the invention, and
`limitations.py` has said so in prose since it was written (TYPE_A vs TYPE_B) without anything
ever acting on it.

WHAT THIS MODULE DOES
---------------------
Names the two searches, decides which one an input is, and carries the budget each is allowed.
The budget is a plain dict of the same knobs `deep_rank` already reads from the environment, so
nothing here invents a second tuning surface: `DEEP_RANK_*` still overrides everything, and
setting `CONCEPT_PROFILE=0` restores the old single-budget behaviour exactly.

It is also the thing the UI reads. A search that will take ninety minutes and a search that will
take twelve must not be started from a page that describes both as "several minutes".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

#  The two searches. `quick` is NOT one of these: it is a DEPTH (the public interactive tier that
#  reads nothing in full) and it cuts across both kinds. Kind answers "what is the question",
#  depth answers "how much are we spending on it".
CONCEPT = "concept"
CLAIMS = "claims"

#  Master switch. Off restores the pre-2026-08-21 behaviour byte for byte: one budget for both
#  kinds, which is what every measurement before that date was taken under.
ENABLED = os.environ.get("CONCEPT_PROFILE", "1") != "0"


@dataclass(frozen=True)
class Profile:
    kind: str
    label: str                 # what the UI calls it
    summary: str               # one line: what this search actually does
    unit: str                  # the unit of work, named for the report's methodology note
    eta_low: int               # seconds, from measured runs
    eta_high: int
    rounds: int                # retrieval rounds (AgentConfig.max_rounds)
    budget: dict = field(default_factory=dict)

    def eta_text(self) -> str:
        """Human ETA. Deliberately a RANGE: every measured run of the same kind differs by 2-3x
        depending on how much text the corpus is missing for the candidates it finds."""
        lo, hi = self.eta_low // 60, self.eta_high // 60
        if hi < 60:
            return f"about {lo}-{hi} minutes"
        return f"about {lo // 60}-{hi // 60} hours" if lo >= 60 else f"about {lo} minutes to {hi // 60} hours"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "summary": self.summary,
                "unit": self.unit, "eta_low": self.eta_low, "eta_high": self.eta_high,
                "eta_text": self.eta_text(), "rounds": self.rounds, "budget": dict(self.budget)}


#  ---- CONCEPT SEARCH -------------------------------------------------------------------------
#  Every number below is a fraction of the claims budget, and each one is cut for a stated reason
#  rather than uniformly:
#
#  rounds 2 -> 1        The second round expands the FIRST round's per-element sub-searches. With
#                       claims, those elements are limitations and the second round is how a
#                       requirement nothing answered gets a second attempt. With 8-12 concept
#                       phrases there is no ledger asking for it, and the measured cost is half
#                       the local channel: 958 s of the 3,932 s run above.
#  SCREEN_TOP 2500->900 The screen depth was measured against an EXAMINER CITATION LIST, where
#                       eleven cited families sat at fusion positions 625-5,731. That measurement
#                       is about finding the specific document that kills a specific claim. A
#                       concept search is answering "what is already out there", and its own
#                       evidence (RECALL_STUDY) is that the top of the fusion order is where the
#                       concept-level matches are. 900 still reaches four times deeper than the
#                       600 the screen used for most of this repo's life.
#  PRESCREEN 400 -> 50  This is the paid one. Fetching full text for 400 references the corpus
#                       holds only an abstract for is bought so the SCREEN can judge them, and a
#                       concept screen judges on the concept, which an abstract carries. Keep a
#                       small budget so a promising abstract-only candidate is still recoverable.
#  CHART_TOP 150 -> 45  Reading is 97% of the bill (72.8M of 112M prompt tokens on the measured
#                       run). 150 is sized to fill a 60-card page after family dedup and screen
#                       misses when each card must answer 60-odd limitations. A concept run's
#                       chart has 8-12 rows, its charting was measured at 72 s for 292 documents,
#                       and the page it feeds is the same size. 45 read in full plus the retrieval
#                       head still puts more than a page of read documents in front of the user.
#  HEAD 60 -> 30        The always-read head exists because the screen scored a good reference 0
#                       three times out of three off a wrong abstract. That failure is real and
#                       the guard stays; it is sized to the read set, so it moves with it.
#  BLIND_RESCUE_MAX     Candidates the screener saw no text for at all. Same argument as the
#  60 -> 20             prescreen: worth keeping, not worth 60 paid fetches on a concept scan.
#
#  NOT CUT, deliberately:
#    * the decomposition itself (5-12 elements) -- it IS the concept search, and it is cheap;
#    * federation / external APIs -- reach is the thing a concept search is worst at, and it runs
#      in parallel with the local channels at 65-101 s measured, so it costs no wall clock;
#    * grounding, the refuter, rarity weighting -- these decide whether an answer is true, and a
#      faster search that overclaims is not a cheaper product, it is a wrong one.
_CONCEPT = Profile(
    kind=CONCEPT,
    label="Concept search",
    summary=("Breaks the description into its distinct technical concepts and searches each one, "
             "then reads the strongest references in full. No claims exist yet, so there is no "
             "claim-by-claim chart."),
    unit="technical concept",
    #  MEASURED, not projected from the cuts. First concept run under this budget
    #  (adhoc-f51e70282e85, 2026-08-21): 900 screened, 90 read in full, 1 round, screen 16.8 s,
    #  charting 38.1 s. Its 3,033 s wall clock is NOT usable, because a 14-hour `CREATE INDEX
    #  ix_chunks_hnsw_half` on `chunks` was holding the lock the text-recovery stage writes
    #  through and that stage alone waited ~2,270 s on it.
    #
    #  So the floor comes from the stages that were not blocked: local retrieval 678 s, external
    #  fan-out 75 s in parallel with it, screen + chart + concept pass ~60 s, and an enrichment of
    #  50 references which measured 63-167 s at ~390 references before the lock. That is ~13
    #  minutes, and the low end is set just under it rather than at the 8 minutes the cuts alone
    #  suggested -- retrieval on its own was 11.3 minutes, so no honest quote starts below that.
    #  RE-MEASURE once the index build is done and tighten this from real end-to-end runs.
    eta_low=12 * 60, eta_high=30 * 60,
    rounds=1,
    budget={
        "SCREEN_TOP": 900,
        "PRESCREEN_ENRICH_TOP": 50,
        "CHART_TOP": 45,
        "CHART_TOP_MAX": 60,
        "ENRICH_TOP": 60,
        "ALWAYS_CHART_RETRIEVAL_HEAD": 30,
        "BLIND_RESCUE_MAX": 20,
        "CONCEPT_PASS_TOP": 60,
    },
)

#  ---- CLAIM ATTACK ---------------------------------------------------------------------------
#  Unchanged. Every constant stays exactly where the measurements in deep_rank.py left it; the
#  empty budget is the point, not an omission.
_CLAIMS = Profile(
    kind=CLAIMS,
    label="Full claim attack",
    summary=("Splits every claim into its separate requirements and searches each one, reads the "
             "strongest references in full against all of them, and rescues any requirement "
             "nothing answered."),
    unit="claim limitation",
    eta_low=50 * 60, eta_high=2 * 60 * 60,
    rounds=2,
    budget={},
)

PROFILES = {CONCEPT: _CONCEPT, CLAIMS: _CLAIMS}


def kind_for(claims) -> str:
    """Which search an input is. The SAME rule as limitations.search_type, decided EARLIER.

    limitations.search_type reads report["query_document"], which does not exist until the search
    is already running. The kind has to be known at /run, because it decides the budget the run
    is given and it is what the page must say before anybody waits an hour for the wrong one.
    """
    return CLAIMS if (claims or []) else CONCEPT


def for_input(claims=None, kind=None) -> Profile:
    """The profile for this input. `kind` overrides the input, for a caller replaying a run."""
    return PROFILES.get(kind or kind_for(claims), _CONCEPT)


def budget_for(profile, depth="deep") -> dict:
    """The knob overrides this run gets, or {} when the split is off or the depth already skips
    the stages being cut.

    QUICK IS LEFT ALONE. The quick tier already reads nothing in full, fetches no paid text and
    runs one round; narrowing its screen on top of that would change a tier whose recall was
    measured at its current width, for no saving that anybody waits on.
    """
    if not ENABLED or depth == "quick":
        return {}
    return dict(getattr(profile, "budget", {}) or {})


def describe(profile, depth="deep") -> dict:
    """What the UI shows. Depth wins the label: a quick run of either kind is the interactive
    tier and must not promise the reading it does not do."""
    d = profile.to_dict()
    if depth == "quick":
        d["label"] = "Fast scan"
        d["summary"] = ("Screens the local corpus against the invention and ranks what it finds. "
                        "Nothing is read in full — escalate for that.")
        d["eta_low"], d["eta_high"] = 3 * 60, 12 * 60
        d["eta_text"] = "about 3-12 minutes"
    d["depth"] = depth
    return d


def catalogue() -> list:
    """Machine-readable list for the UI's explainer. Mirrors search_modes.available_modes()."""
    return [describe(p) for p in (_CONCEPT, _CLAIMS)]
