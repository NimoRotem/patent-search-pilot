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
#  Re-budgeted 2026-08-23 against the owner's targets: find <= 2 min, ledger + grid <= 10
#  more, the 1.290 package <= 5 more. The reading is 97% of the bill, so the read set is what
#  shrinks: 45-60 references instead of 150-240. The claim rescue moves OUT of this phase (it
#  measured 495-4,217 s on every claims run); DEEP_RANK_RESCUE_CLAIMS=1 pins it back on for an
#  A/B, and every other knob stays operator-pinnable through its DEEP_RANK_* variable.
_CLAIMS = Profile(
    kind=CLAIMS,
    label="Claim ledger + prior-art grid",
    summary=("Splits every claim into its separate requirements, verifies the retrieved "
             "passages against each one, reads the strongest references in full, and builds "
             "the ledger and the claim-by-prior-art grid."),
    unit="claim limitation",
    eta_low=6 * 60, eta_high=10 * 60,
    rounds=1,
    budget={
        "PRESCREEN_ENRICH_TOP": 60,
        #  45 was measured too narrow: the recall gate read 0/10 attorney references on the page
        #  against 2/10 at baseline, with two gold references screened 86 and 88 and cut. The
        #  reading costs ~1.5 s/reference at 24 workers, so width is cheap; the wall clock comes
        #  out of the element passes instead.
        "CHART_TOP": 120,
        "CHART_TOP_MAX": 140,
        "ENRICH_TOP": 60,
        "ALWAYS_CHART_RETRIEVAL_HEAD": 24,
        "BLIND_RESCUE_MAX": 12,
        "CONCEPT_PASS_TOP": 60,
        "RESCUE_CLAIMS": 0,
        #  The claim-reach slots were 184 reads outside every other knob on the first budgeted
        #  run. 60 at round-robin still guarantees every claim its strongest candidates.
        "CLAIM_REACH_CAP": 60,
        #  The sweep's cost is CALLS, docs x limitation groups, not documents: at the old caps a
        #  21-claim subject queued a 760-call sweep, which alone is twenty minutes. 4 per
        #  limitation across 60 documents keeps the sweep near the 899-run's measured ~2 s/call
        #  shape without starving any claim.
        "BATCH_PER_LIM": 4,
        "BATCH_TAIL_MAX": 60,
    },
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


#  PHASE 2. The ledger pass verifies the RETRIEVED PASSAGES for every requirement, and reads
#  no document in full: that is phase 3's spend. Against the quick tier it widens only the two
#  knobs that decide how much evidence the sweep may gather, because the measured complaint about
#  quick is not that it reads too little text, it is that it asks about too few references.
LEDGER_BUDGET = {"BATCH_PER_LIM": 12, "BATCH_TAIL_MAX": 300}


#  PHASE 3, THE SHORT WAY. A full-depth claims run reads 211 to 224 references end to end and a
#  37 CFR 1.290 submission FILES TEN of them. Measured on run adhoc-d8c2d44ef969 (871 s total),
#  reading was 580 s of it against 47 s to screen 2,500 candidates: two thirds of the wall clock,
#  spent almost entirely on documents that were never going to be filed.
#
#  So this depth reads a read-set sized for the deliverable instead of for the grid. Measured on
#  run FT-D against the nine references the offices themselves cited against that family, they
#  landed at deep-block positions 1, 2, 5, 14, 41 and 179; a read set of about 45 keeps five of
#  those six, and the one it loses is at 179, which nothing short of reading everything reaches.
#
#  EVERY KNOB SCALES TOGETHER, because the read set is not one knob. The measured deep run read
#  222 with CHART_TOP at 120: the always-read retrieval head, the per-claim reach round-robin and
#  the blind rescue add the other ~100 between them. Cutting CHART_TOP alone would have left ~145
#  documents being read while the page claimed 45, so the setting is the TOTAL and the parts are
#  derived from it in the proportions the measured run actually ran at.
#  HOW LONG A READ SET TAKES, from the measured runs. Retrieval, the external fan-out and the
#  screen are close to fixed; the reading is the part that scales with the number of documents.
#  Measured: adhoc-d8c2d44ef969 read 222 in 580 s (2.6 s each) with retrieval and screening at
#  ~290 s around it. So a quote is that fixed cost plus the per-document one, and it is a RANGE
#  because a 200,000-character grant and a two-page utility model are both one document.
READ_FIXED_SECONDS = int(os.environ.get("READ_FIXED_SECONDS", "290"))
READ_PER_DOC_SECONDS = float(os.environ.get("READ_PER_DOC_SECONDS", "2.6"))


def read_eta(n):
    """(low_seconds, high_seconds, text) for reading `n` references in full."""
    mid = READ_FIXED_SECONDS + READ_PER_DOC_SECONDS * max(0, int(n))
    lo, hi = mid * 0.75, mid * 1.6
    def m(s):
        return max(1, int(round(s / 60.0)))
    return int(lo), int(hi), ("about %d minutes" % m(mid) if m(hi) - m(lo) < 3
                              else "about %d to %d minutes" % (m(lo), m(hi)))


#  The most any one run will read, and the same number `/run` clamps `read_top` to. A ladder that
#  offered more than this would print a depth the search then quietly did not honour.
#  Raised 400 -> 1000. A report that had already read 417 produced a slider with min=417 and
#  max=400: min above max, so the control was stuck and offered "0 more than the 417 already
#  read". The ceiling has to sit above anything a run can already have read, and 400 stopped
#  doing that the moment the read pool became the screened population rather than the page.
READ_TOP_MAX = int(os.environ.get("READ_TOP_MAX", "1000"))


def read_choices(n_surfaced=0):
    """The read depths offered after a find, smallest first. -> [{n, label, eta_text}]

    `n_surfaced` is the SCREENED population, not the number of cards on the page. It used to be
    the page, so a search that screened 601 candidates topped out at "every one of the 60 found":
    the rungs above 60 were dropped for being larger than a number that had nothing to do with how
    much there was to read.
    """
    out = []
    for n in (25, 45, 80, 120, 200, 300):
        if n_surfaced and n >= n_surfaced:
            continue
        if n > READ_TOP_MAX:
            break
        out.append({"n": n, "eta_text": read_eta(n)[2]})
    if n_surfaced:
        #  Clamped, and the label says which it is: "every one of the 601 screened" would be a
        #  promise the run cannot keep, because /run caps read_top at READ_TOP_MAX.
        top = min(int(n_surfaced), READ_TOP_MAX)
        if not out or top > out[-1]["n"]:
            out.append({"n": top, "eta_text": read_eta(top)[2],
                        "all": top >= int(n_surfaced), "pool": int(n_surfaced)})
    for c in out:
        c["label"] = ("every one of the %d screened" % c["n"] if c.get("all")
                      else "the strongest %d%s" % (c["n"],
                                                   " of %d screened" % c["pool"]
                                                   if c.get("pool") else ""))
    return out


def submission_budget(profile, n=None, batched=False) -> dict:
    """The claim-attack budget, re-cut for a run whose product is the filing, not the grid.

    `n` is the read set this particular run was asked for, which is how the depth chooser on a
    finished find carries the reader's answer into the search it starts. Absent, the setting.

    `batched` MOVES THE POPULATION, it does not add a mode. There are already two readers here.
    The per-document reader carries the whole document through up to fourteen READ-tier prompts
    with a grounding gate and an independent refuter between them; the batch reader asks ONE
    requirement of MANY documents in a single call, ordered documents-first so every requirement
    asked of one batch shares one long prefix (83% of prompt tokens served from cache, measured).
    deep_rank already runs the second one over the tail "at ~1/7 the effective token cost".

    So the batched option shifts documents from the first reader to the second: about a fifth as
    many full per-document reads, a tail two and a bit times wider, and more requirements asked of
    each batch. What that buys and what it costs are both measured and both real:

        cheaper   ~1/7 the effective tokens per document covered
        slower    6.7 pairs a second against 26.8, so the same coverage takes about 4x the wall
                  clock. This is why the batched choice is the one that emails you.
        weaker    per document. A batch cell is charted against the claim limitations only, with
                  no refuter pass. The head is still read the expensive way, so the documents that
                  will actually be cited keep the full treatment; the tail gets breadth instead.
    """
    if n is None:
        n = 45
        try:
            import search_settings as _ss
            n = max(10, int(_ss.get("submission_chart_top")))
        except Exception:
            pass
    n = max(10, int(n))

    def share(f, floor=1):
        return max(floor, int(round(n * f)))

    budget = dict(getattr(profile, "budget", {}) or {})
    if batched:
        #  See the docstring: the same n documents get covered, most of them by the cheap reader.
        budget.update({
            "CHART_TOP": share(0.22),
            "CHART_TOP_MAX": share(0.30),
            #  The always-read head is the guard against a wrong abstract scoring a good reference
            #  zero, which was measured three times out of three. It shrinks with the read set but
            #  it never goes away, and it is why the top of the page is still fully read.
            "ALWAYS_CHART_RETRIEVAL_HEAD": share(0.12),
            "CLAIM_REACH_CAP": share(0.22),
            "BLIND_RESCUE_MAX": share(0.09),
            "PRESCREEN_ENRICH_TOP": share(0.90),
            "ENRICH_TOP": share(0.90),
            "BATCH_PER_LIM": 8,
            "BATCH_TAIL_MAX": share(2.2),
            "RESCUE_CLAIMS": 0,
        })
        return budget
    budget.update({
        "CHART_TOP": share(0.55),
        "CHART_TOP_MAX": share(0.70),
        "ALWAYS_CHART_RETRIEVAL_HEAD": share(0.18),
        "CLAIM_REACH_CAP": share(0.22),
        "BLIND_RESCUE_MAX": share(0.09),
        "PRESCREEN_ENRICH_TOP": share(0.90),
        "ENRICH_TOP": share(0.90),
        #  The per-requirement sweep is the ledger's product and the ledger is what ORDERS the
        #  documents for the packet, so it is cut rather than dropped: without it there is no
        #  "what does this document kill" to select the ten on.
        "BATCH_PER_LIM": 3,
        "BATCH_TAIL_MAX": share(0.90),
        #  The claim rescue measured 495-4,217 s on every claims run and is already off at full
        #  depth. A submission run has even less room for it.
        "RESCUE_CLAIMS": 0,
    })
    #  SCREEN_TOP is deliberately NOT cut. Screening 2,500 candidates cost 47 s against 580 s of
    #  reading, and it is precisely what makes a small read set safe: the saving has to come out
    #  of the reading, never out of the choosing.
    return budget


def grid_budget(profile, n=None, batched=False) -> dict:
    """The claim-attack budget for a run whose product is the GRID, at a chosen read depth.

    "Full grid" used to be a second button posting `depth=deep`, which took the profile's fixed
    budget and ignored the depth the reader had just chosen beside it. Once the two controls are
    one form that is a silent lie: pick "the strongest 300" and "+ the claim ledger and grid" and
    the run reads 120, because 120 is what `_CLAIMS` says.

    So the grid scales like the submission does, from the same number, and differs where the
    deliverable differs. The ledger's product IS the per-requirement sweep, so the sweep keeps the
    width `LEDGER_BUDGET` gives it instead of the 3-per-limitation the packet cuts it to; the
    packet only needs the sweep to ORDER the documents it files.
    """
    budget = submission_budget(profile, n=n, batched=batched)
    #  THE SWEEP IS CALLS, NOT DOCUMENTS, and I got this wrong once already.
    #
    #  The first version of this function took BATCH_PER_LIM and BATCH_TAIL_MAX from
    #  LEDGER_BUDGET (12 and 300) on the reasoning that the sweep is the grid's product. The
    #  measurement sitting three inches above `_CLAIMS["BATCH_PER_LIM"] = 4` says exactly why that
    #  is wrong: "at the old caps a 21-claim subject queued a 760-call sweep, which alone is
    #  twenty minutes. 4 per limitation across 60 documents keeps the sweep near ~2 s/call".
    #  Cost is ceil(tail_docs / ~13) batches x one call per limitation, so widening the tail five
    #  times over multiplies the calls five times over. A user watching "batch 180 of 180 . 12m
    #  10s" was watching that.
    #
    #  So the grid scales its READ SET with the chosen depth, like the packet does, and leaves the
    #  sweep at the width that was measured. What the grid still gets over the packet is the
    #  claim-reach floor below: every claim keeps its own strongest candidates, because with the
    #  grid as the deliverable a claim with no row is a hole in the product.
    budget.update({
        "BATCH_PER_LIM": (getattr(profile, "budget", {}) or {}).get("BATCH_PER_LIM", 4),
        #  Half the read set, which is the ratio the measured deep run had (tail 60 against
        #  CHART_TOP 120). The packet's 0.9n is right for the packet, where the cheap reader is
        #  deliberately carrying the breadth; for the grid it put the tail above the read set and
        #  quadrupled the sweep.
        "BATCH_TAIL_MAX": max(20, int(round(0.5 * budget.get("CHART_TOP", 0)))),
        "CLAIM_REACH_CAP": max(int(budget.get("CLAIM_REACH_CAP") or 0), 60),
    })
    return budget


#  EVERY KNOB IN THE PIPELINE THAT REACHES A THIRD PARTY.
#
#  Zeroing them is how "third-party sources: off" is enforced downstream, because deep_rank already
#  checks each one before it spends: `_enrich_missing_text` returns 0 when `enrich_top <= 0`, the
#  pre-screen recovery is behind `if B["PRESCREEN_ENRICH_TOP"] > 0`, and the blind rescue is the
#  set of candidates the screener saw no text for, which is only worth keeping if something can be
#  fetched for them.
#
#  MEASURED, on adhoc-f4e9c4e96449: the outside world produced 319 of 3,470 families for 58.5 of
#  the 65 seconds the retrieval took, while our own corpus produced the other 3,151 in about six.
#  So this is not a crippled mode; it is the fast one, and what it gives up is 296 families that
#  nothing in the corpus had.
_THIRD_PARTY_KNOBS = ("ENRICH_TOP", "PRESCREEN_ENRICH_TOP", "BLIND_RESCUE_MAX")


def local_only_budget(budget) -> dict:
    """Take every knob that would call outside this box down to zero. -> the same dict.

    Retrieval's own external channels are switched off separately, at the point the channel list
    is built, because they are not knobs. Between the two, a local-only run touches nothing but
    `niche_full_v1`.
    """
    out = dict(budget or {})
    for k in _THIRD_PARTY_KNOBS:
        out[k] = 0
    return out


#  FAST's budget. Every knob whose only consumer is the reading stage is zero, so a caller
#  that ignores `search_mode` and looks only at the budget still cannot start a read.
FAST_BUDGET = {"SCREEN_TOP": 0, "CHART_TOP": 0, "CHART_TOP_MAX": 0, "ENRICH_TOP": 0,
               "PRESCREEN_ENRICH_TOP": 0, "ALWAYS_CHART_RETRIEVAL_HEAD": 0, "BLIND_RESCUE_MAX": 0,
               "CONCEPT_PASS_TOP": 0, "RESCUE_CLAIMS": 0, "CLAIM_REACH_CAP": 0,
               "BATCH_PER_LIM": 0, "BATCH_TAIL_MAX": -1}


def lane_for(depth: str) -> str:
    """Which durable lane a depth runs on. Only `quick` and `deep` have workers.

    `submission` reads documents in full, so it belongs on the deep lane whatever it is called;
    putting it on the quick lane would let a five-minute read block the two-minute tier.
    """
    return "deep" if str(depth) in ("deep", "submission") else "quick"


def budget_for(profile, depth="deep", read_top=None, batched=False, local_only=False) -> dict:
    if depth == "fast":
        return dict(FAST_BUDGET)
    """The knob overrides this run gets, or {} when the split is off.

    FIND BUYS RETRIEVAL AND RANKING ONLY. The per-requirement sweep the tail runs is the ledger
    phase's product, and -1 is the explicit "off" the stage checks for. Everything else about
    quick stays untouched, for the measured reason: narrowing its screen would change a tier
    whose recall was measured at its current width, for no saving anybody waits on.
    """
    if not ENABLED:
        return local_only_budget({}) if local_only else {}
    if depth == "quick":
        b = {"BATCH_TAIL_MAX": -1, "SCREEN_TOP": 600}
        return local_only_budget(b) if local_only else b
    if depth == "submission":
        b = submission_budget(profile, n=read_top, batched=batched)
        return local_only_budget(b) if local_only else b
    #  A DEPTH THE READER CHOSE BEATS THE PROFILE'S DEFAULT. Absent, nothing changes: every
    #  caller that never passed `read_top` gets exactly the budget it got before.
    if depth == "deep" and read_top:
        b = grid_budget(profile, n=read_top, batched=batched)
        return local_only_budget(b) if local_only else b
    budget = dict(getattr(profile, "budget", {}) or {})
    if depth == "ledger":
        budget.update(LEDGER_BUDGET)
    return local_only_budget(budget) if local_only else budget


def describe(profile, depth="deep") -> dict:
    """What the UI shows. Depth wins the label: a quick run of either kind is the interactive
    tier and must not promise the reading it does not do."""
    d = profile.to_dict()
    if depth == "quick":
        d["label"] = "Find"
        d["summary"] = ("Searches the corpus and ranks what it finds, with the passages that "
                        "matched each requirement. Nothing is read in full and nothing is called "
                        "a disclosure yet.")
        d["eta_low"], d["eta_high"] = 60, 2 * 60
        d["eta_text"] = "about 2 minutes"
    elif depth == "ledger":
        d["label"] = "Claim ledger"
        d["summary"] = ("Verifies the retrieved passages against every requirement and builds "
                        "the ledger. Still reads no document in full.")
        d["eta_low"], d["eta_high"] = 5 * 60, 20 * 60
        d["eta_text"] = "about 5-20 minutes"
    elif depth == "submission":
        n = 45
        try:
            import search_settings as _ss
            n = max(10, int(_ss.get("submission_chart_top")))
        except Exception:
            pass
        d["label"] = "Third-party submission"
        #  No trailing caveat about what this depth does NOT read. It compared the product to
        #  an earlier version of itself, which is not something the reader is choosing between.
        d["summary"] = ("Reads about %d references in full, builds the ledger from them and "
                        "orders them by what they kill, which is what the 37 CFR 1.290 packet is "
                        "selected on." % n)
        d["eta_low"], d["eta_high"] = 5 * 60, 9 * 60
        d["eta_text"] = "about 5-9 minutes"
    d["depth"] = depth
    return d


def catalogue() -> list:
    """Machine-readable list for the UI's explainer. Mirrors search_modes.available_modes()."""
    return [describe(p) for p in (_CONCEPT, _CLAIMS)]
