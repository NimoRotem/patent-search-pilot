# Benchmark, 2026-08-14 — the whole-checklist reading change

Two subjects, two arms, same day, same corpus, same code. The arms differ only in the constants,
set through the environment overrides that already exist for exactly this purpose:

    CONTROL    DEEP_MAX_FEATURES=20 DEEP_FEATURE_BATCH=20 DEEP_RANK_CONCEPT_PASS_TOP=0
               DEEP_RANK_DISCLOSURE_CAP=40 DEEP_RANK_CHART_MIN_SCREEN=75
               DEEP_RANK_CHART_TOP=300 DEEP_RANK_CHART_TOP_MAX=350 DISPLAY_TOP=50
    TREATMENT  the shipped defaults

`REUSE_META_FROM_TAG=ctl20260814` on the treatment arm, so both arms searched a byte-identical
ingested subject and the external replay cache was shared.

## FIRST: the harness was measuring nothing

`generate()` deletes `<slug>.view.json` and `citation_recall` reads exactly that file, so
`displayed` was structurally 0 for every run that ever used this path. v15, abc2 and abt2 all say
"0 / 9 families in the RANKED top 0" — **"top 0" is the tell**: the harness reported a page it had
never rendered, and reported it as a number. Fixed here (`_build_view_cached` after `_generate`),
and no result from before this date should be compared against one after it.

## Result

| | control | treatment |
|---|---|---|
| ep3707092 displayed | 1/16 | **2/16** |
| schmalz displayed | 1/7 | 1/7 |
| TOTAL | 2/23 | **3/23** |
| ep3707092 charted / displayed | 407 / 50 | 463 / 60 |

+1 is inside the documented +/-2 run-to-run variance, so on the headline number this is **no
significant change** — which is the result that mattered, because the risk this benchmark was run
to check is the funnel-width lesson: widening the read set while the page stays fixed LOWERS
visible recall. It did not.

The +1 is not noise, though, and the audit shows the mechanism end to end. DE-3724659-A1:

    control     screen 75 -> "screened, not read"        -> not on the page
    treatment   screen 75 -> read, chart rank 33         -> CARD 38

`CHART_MIN_SCREEN` 75 -> 70 pulled it into the read set — this is the exact case
`deep_rank`'s own comment describes ("cited references the screen had rated 70 were never read") —
and `_DISPLAY_TOP` 50 -> 60 put it on the page. "screened, not read" went 2 -> 0.

## What this benchmark says the bottleneck actually is

For ep3707092, of 27 cited references:

    NOT IN CORPUS        11
    NEVER RETRIEVED      12      <- never entered the 7,308-family candidate pool
    charted, not displayed 2
    DISPLAYED             2

**Twelve of the sixteen in-corpus citations were never retrieved at all.** No change to reading,
ranking, charting or display can reach a document retrieval never returned, and this whole change
is downstream of retrieval. It improves what the page can say about the references it DOES find —
the full checklist charted rather than half of it, quotes and passages per cell, the concept-led
second reading — and it is neutral on which references are found, as measured.

The next real lever on this number is REACH: the twelve never-retrieved, and the eleven the corpus
does not hold. Not the ranking.
