# M9 Relevance & Rationale Audit — post-OPS re-run

_2026-07-19. Same 8 queries, same slugs, same judge rubrics in `src/audit.py`, same 28+12 card split
as the original M9 run. Nothing in the judge prompts or the metric definitions was changed._

All 8 audited reports were **regenerated on the deepened corpus first** — the gold reports on disk
were written 2026-07-18 01:25, i.e. before the OPS backfill started (07-19 14:12), so auditing them
would have measured stale output. Rationale caches for the two audited gold slugs were cleared so
rationales were re-generated against current text (the pre-run caches are preserved in
`data/rationale.PRE_OPS/`).

Driver: `src/rerun_audit.py`. Raw output: `data/reports/_audit_{relevance,rationale,cells}_POST_OPS.json`.

---

## 1. Blind relevance audit — precision@10

| Query | kind | strict before | strict now | lenient before | lenient now | rel / bord / irr |
|---|---|--:|--:|--:|--:|:--:|
| grabo_gripper_novelty | gold | 0.60 | 0.50 | 0.80 | 0.60 | 5 / 2 / 3 |
| schmalz_vacuum_clamp | gold | 0.60 | 0.50 | 0.80 | 0.70 | 5 / 4 / 1 |
| probst_kerb_lifter | gold | 0.30 | 0.30 | 0.45 | 0.60 | 3 / 6 / 1 |
| handheld_lifter | free-text | 0.00 | **0.20** | 0.25 | **0.50** | 2 / 6 / 2 |
| robotic_eoat | free-text | 0.30 | 0.30 | 0.50 | 0.45 | 3 / 3 / 4 |
| suction_pnp | free-text | 0.30 | 0.20 | 0.55 | 0.55 | 2 / 7 / 1 |
| broad_vacuumgripper | edge (broad) | 1.00 | **1.00** | 1.00 | 1.00 | 10 / 0 / 0 |
| narrow_multifeature | edge (narrow) | 0.00 | 0.00 | 0.00 | **0.35** | 0 / 7 / 3 |
| **mean** | | **0.39** | **0.375** | **0.54** | **0.594** | |

**Read:** strict precision is flat (−0.015, well inside noise at n=10/query); lenient precision is up
(+0.054). The lenient gain is concentrated exactly where M9 predicted it would be — the two queries
M9 flagged as *judge-understated* because the judge was reading bare titles. `narrow_multifeature`
went 0.00 → 0.35 lenient and `handheld_lifter` 0.25 → 0.50, because those references now carry real
claim text for the judge to read. So the backfill **did** improve the *evidence available for
judging*, even though it did not improve *ranking* (see `data/eval/eval_report.md`).

The two gold anchors dropped slightly on strict (0.60 → 0.50) and picked up `irrelevant` verdicts
they did not have before (grabo 0 → 3 irrelevant). With n=10 per query this is weak evidence, but it
is consistent with deeper text letting the skeptical judge find *absent* features it previously
could not check.

## 2. Rationale-accuracy audit — 38 cards ⚠ REGRESSION

| | accurate | overclaims | hallucinates | vague | **overclaim + hallucinate** |
|---|--:|--:|--:|--:|--:|
| M9 before (original prompt) | 31 | 7 | 2 | 0 | **22.5%** (9/40) |
| M9 after (grounded prompt) | 33 | 2 | 2 | 3 | **10.0%** (4/40) |
| **post-OPS now** | 28 | **7** | **3** | 0 | **26.3%** (10/38) |

Rationale faithfulness has regressed past its own pre-fix baseline. This is the most serious finding
of the re-run. The grounding filter (`webapp._ground_reads_on`) is still in the code path and still
running — it has become *less effective*, not absent.

### Why — and how much of it is a measurement artifact

`webapp._rationale` shows the model **title + abstract + the single best-matching passage**. After
the backfill that best-matching passage is frequently a newly-added deep description paragraph.
`audit.ref_text` shows the judge **title + abstract + claim 1**, and only falls back to body chunks
when abstract *and* claim 1 are both missing — a fallback the backfill made much rarer precisely by
populating the `claims` table. The generator and the judge are therefore now reading **different
text**, and the model can ground a statement in a passage the judge never receives.

Measured, not assumed (`src/diag_rationale.py`): for each of the 10 flagged cards, the grounded
evidence quotes were checked for word-overlap against the judge's snippet and against the full
document text.

| flagged card | evidence in judge snippet | evidence in full doc | verdict |
|---|--:|--:|---|
| US-2005134063-A1 | 0.30 | 0.89 | **artifact** |
| US-3152828-A | 0.33 | 0.85 | **artifact** |
| US-8290624-B2 | 0.62 | 0.88 | real |
| US-4635988-A | 0.70 | 0.89 | real |
| US-2002074703-A1 | 0.44 | 0.74 | real |
| US-10562195-B2 | 0.33 | 0.71 | real |
| US-6171049-B1 | 0.44 | 0.70 | real |
| US-3240525-A | 0.50 | 0.77 | real |
| US-4858975-A | 0.54 | 0.69 | real |
| EP-1077112-A3 | 0.54 | 0.54 | real |

Only **2 of 10** are clean artifacts (evidence ≥0.8 present in the document, <0.6 in what the judge
saw). Correcting for those still leaves **8/38 = 21.1%**, a regression versus 10%. The classifier
threshold is crude and several "real" rows sit at 0.85–0.89 document overlap, so the true artifact
share is probably a little higher than 2 — but not enough to rescue the 10% figure.

**Genuine mechanism behind the residual.** The grounding filter keeps an element when ≥60% of the
model's evidence quote's content words appear in `biblio_txt + matched_txt`. A longer, richer
`matched_txt` makes that bar *easier* to clear with a loose paraphrase, so more marginal elements
survive. Deeper text widened the filter's effective tolerance.

### Recommended fixes (not applied — outside this task's file scope)
1. Show the judge the **same** passage the generator saw (pass `matched_txt` into `audit.ref_text`).
   This removes the artifact class entirely and makes the metric honest again.
2. Tighten `_ground_reads_on` — require the evidence quote to be a near-contiguous span rather than a
   60% bag-of-words overlap, which scales badly as passages get longer.

## 3. Claim-chart cell correctness — 12 coord-backed cells

| report | coord cells | related | weak | unrelated | whole-doc-only |
|---|--:|--:|--:|--:|--:|
| grabo_gripper_novelty | 5 | 3 | 0 | 2 | 4 |
| schmalz_vacuum_clamp | 7 | 2 | 1 | 4 | 2 |
| **total now** | **12** | **5** | **1** | **6** | **6** |
| _M9 baseline_ | _12_ | _6_ | _1_ | _5_ | _8_ |

Coord-backed false-positive rate (weak + unrelated) **50% → 58%**. With 12 cells that difference is
one cell — statistically meaningless. The honest statement is that the claim chart's cell-level
false-positive rate is **unchanged at roughly half**, and the backfill neither helped nor hurt it.

The one genuine improvement: whole-doc-only cells (no citable coordinate at all) fell 8 → 6, because
some references now have paragraphs to point at.

**The claim chart remains the least trustworthy surface in the product.** It was named as a new
hallucination surface and it measures as one: roughly half of the cells that *look* like verified
coverage cite a passage that does not disclose the element. The M9 conclusion stands unchanged —
treat chart cells as "worth reading", not "proven coverage" — and the per-cell LLM verification that
`audit.judge_cell` demonstrably performs correctly is still the reliable fix, still deferred.

## 4. Verdict

- Relevance ranking: **unchanged** (strict flat, lenient up via better judging evidence, not better ranking).
- Rationale faithfulness: **materially worse** (10% → 26.3%; ≥21% after artifact correction).
- Claim-chart cells: **unchanged** at ~50–58% coord-cell FP.

Net: the backfill improved how much text is *available to read and judge*, and did not improve — and
in the rationale layer actively degraded — what the system *tells a user*.
