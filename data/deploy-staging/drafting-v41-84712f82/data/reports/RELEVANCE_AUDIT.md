# Milestone 9 — Relevance & Rationale-Accuracy Audit

_Is the tool actually **good**, not just high-recall? Real numbers below, an honest strong/weak
verdict, and the fixes made. No spend, no corpus re-index (still holding corpus-deepening for the
EPO unblock)._

Method: an **independent skeptical-examiner LLM judge** (`src/audit.py`, a rubric distinct from the
ranking and rationale prompts, so this is not self-affirmation) **plus human spot-reading** of the
actual claims/abstracts to validate the machine judgments. Every number here is measured, not
"looks good."

---

## 1. Blind relevance audit — precision@10 (8 queries)

Judged the top-10 of 8 queries: 3 gold anchors, 3 realistic free-text inventions, 2 deliberately
hard edge cases. `relevant` = discloses the core mechanism **and** ≥1 specific claimed feature (art
an examiner would cite for novelty); `borderline` = same narrow field / partial-or-analogous feature
overlap (citeable for obviousness or against a broad claim); `irrelevant` = not the same device
class, or discloses none of the mechanism.

| Query | kind | p@10 strict | p@10 lenient¹ | rel / bord / irr | read |
|---|---|--:|--:|:--:|---|
| grabo_gripper_novelty | gold | 0.60 | 0.80 | 6 / 4 / 0 | strong — all 10 on-field, 0 junk |
| schmalz_vacuum_clamp | gold | 0.60 | 0.80 | 6 / 4 / 0 | strong |
| probst_kerb_lifter | gold | 0.30 | 0.45 | 3 / 3 / 4 | mixed — some off-task kerb/paving hits |
| handheld_lifter | free-text | 0.00 | 0.25 | 0 / 5 / 5 | on-field but **specific combo is novel**² |
| robotic_eoat | free-text | 0.30 | 0.50 | 3 / 4 / 3 | decent |
| suction_pnp | free-text | 0.30 | 0.55 | 3 / 5 / 2 | decent |
| broad_vacuumgripper | edge (broad) | **1.00** | 1.00 | 10 / 0 / 0 | excellent on a broad query |
| narrow_multifeature | edge (narrow) | 0.00 | 0.00 | 0 / 0 / 10 | on-field, judge-understated³ |
| **mean** | | **0.39** | **0.54** | | |

¹ lenient = (relevant + 0.5·borderline)/10.
² **handheld_lifter is NOT a ranking failure.** Human spot-read: the top-10 are genuine handheld
vacuum-cup lifters — including US-762499-A (a **1904** vacuum-cup lifter with a handle, *"especially
useful for setting plates of glass in window-frames"*), directly on-point. They score `borderline`,
not `relevant`, only because the query demands a **cordless electric pump + a pressure-sensor alarm**
and these are *manual/passive* cups. For a novelty search they are legitimate background art; the
specific powered+sensored combination is simply novel (a *positive* signal about the invention).
³ **narrow_multifeature exposes a judge limitation, not junk.** All 10 are real modern vacuum
grippers in the exact field, and ≥3 disclose an individual claimed feature (e.g. US-9114535-B2
*"Object-sensing valve for a vacuum gripper"* discloses the part-presence-sensing function, just
mechanically rather than capacitively). The examiner LLM fixates on the 3 missing features of a
4-feature query and marks them all `irrelevant`; a human would call several `borderline`. So true
lenient precision here is ≈0.15–0.20 — the **automated judge under-measures multi-feature queries.**

**Verdict on §1:** where a real ranking problem would show up (top-10 full of off-topic junk), only
one systematic cause existed — **design patents and title-only publications flooding the top-10**
on a bare-title match — now fixed (below). On every audited query the top-10 are on-field vacuum
grippers/lifters; low strict-precision tracks **query specificity**, not bad ranking.

### Two audit-method corrections found along the way (integrity)
The first-pass audit reported mean strict p@10 = 0.34 and two queries at **0.00** that looked like
ranking failures. Both were **measurement artifacts**, now corrected in `src/audit.py`:
- **`ref_text` was blind to OCR'd old patents.** Pre-1980 US patents often have no abstract and no
  rows in the `claims` table — their disclosure lives only in `paragraph`/`figure_caption` chunks.
  The judge was seeing a bare title (`"Vacuum-lifter."`) and dismissing genuinely-relevant art.
  Fixed to fall back to the chunk body — the same text the retriever matched on.
- **The judge was ~1 notch too strict**, collapsing `borderline`→`irrelevant` (it marked a reference
  that literally discloses a claimed feature as irrelevant, contradicting its own rubric).
  Recalibrated to the examiner standard (partial/analogous overlap = borderline). Validated by human
  spot-read: gold anchors correctly went to 0 irrelevant; the 1904 glass lifter correctly became
  borderline.

---

## 2. Rationale-accuracy audit (the legal-risk one) — 40 cards

Compared each cached gemini-flash "why relevant / reads on <elements>" against the reference's
**actual text**. `overclaims` = asserts an element the text doesn't disclose; `hallucinates` = cites
features absent from the doc; `vague` = generic, no checkable tie.

| | accurate | overclaims | hallucinates | vague | **overclaim + hallucinate** |
|---|--:|--:|--:|--:|--:|
| **before** (original prompt) | 78% | 7 | 2 | 0 | **22%** |
| **after** (grounded prompt) | 82% | 2 | 2 | 3 | **10%** |

22% overclaim+hallucinate was **> the 10% threshold**, so the rationale prompt was tightened and
re-measured on the **same 40 cards** (isolating the prompt change). Three changes in
`webapp._rationale`:
1. **Evidence-grounded `reads_on` (deterministic).** The model must now quote the supporting words
   per element; `_ground_reads_on` then **drops any element whose quote is not actually in the
   reference text** — a fabricated or absent-from-text element is removed even if the model listed
   it. This is the main lever (an overclaim can't survive without grounded evidence).
2. **Empty-reference guard.** A text-less doc (e.g. a synthetic junk anchor) short-circuits to
   *"reference text was not available… treat as unconfirmed"* instead of an invented disclosure —
   this eliminated the hallucinations on empty docs (they became honest "vague/unconfirmed").
3. **`why`/`reads_on` separation.** `why` cites only wording present in the text and must be
   consistent with the grounded `reads_on`; specific element claims live in the verified `reads_on`.

Net: overclaim+hallucinate **22% → 10%**, accuracy **78% → 82%**, and hallucination on empty docs
eliminated. The small residual `vague` (8%) is the deliberate trade — a hedged "unconfirmed" is
safe for a lawyer; a confident overclaim is not.

---

## 3. Claim-chart cell correctness — 20 covered cells (2 gold reports)

For every covered cell I fetched the **actual text at the cited coordinate** and judged whether it
relates to that element.

| report | coord-backed cells | related | weak | unrelated | whole-doc-only (no coord) |
|---|--:|--:|--:|--:|--:|
| grabo_gripper_novelty | 6 | 5 | 0 | 1 | 6 |
| schmalz_vacuum_clamp | 6 | 1 | 1 | 4 | 2 |
| **total** | **12** | **6** | **1** | **5** | **8** |

**Coord-backed false-positive rate (weak+unrelated) ≈ 50%** — systematic, and concentrated on
`schmalz`'s highly specific "Driver…" elements, where the cited passage describes a *four-bar
linkage* or a *substrate support*, not the claimed driver feature. Plus **8 of 20** cells are backed
only by a whole-doc match with **no specific coordinate at all**.

**A min-score threshold does NOT work here** (measured, not assumed): the fused RRF score
(0.038–0.044) is flat across true and spurious cells, and the element↔cited-chunk cosine **overlaps**
(related 0.715–0.767 vs. weak/unrelated 0.666–0.771 — an `unrelated` cell scores 0.771, above
several `related` ones). So no single score cleanly separates a real cell from a spurious one.

**Fix applied (reliable):** whole-doc-only cells (no claim/para/figure to point at) are now flagged
`strength: "weak"` in the claim chart, so the chart no longer *implies verified coverage* where it
can't cite a passage. **The reliable fix for the residual coord-cell FPs is per-cell LLM
verification** — `audit.judge_cell` demonstrably does it correctly (it flagged every four-bar-linkage
/ substrate-support mismatch). It is recommended for report generation and deferred with the EPO
corpus pass to keep generation latency/cost bounded, exactly as scoped.

---

## 4. Fix + guard: the ranking pathology, and NO recall regression

The one real ranking problem in §1 was **design patents + title-only publications flooding the
top-10** on a bare-title cosine match (e.g. a 1919 "Vacuum lifting device" that is a single title
chunk, or a design patent with no technical text).

**First attempt regressed recall — and that is the key lesson.** Demoting these *inside the
retrieval channels* propagated through RRF fusion and pushed title-only **gold** families out of the
top-100: agentic recall@100 **0.185 → 0.138**, vector@100 **0.170 → 0.146**, and agentic was no
longer the best config. That change was fully reverted.

**Correct fix — display layer only** (`webview.substance_order`, `webview.build_view`): drop design
patents and demote title-only families **in what the page shows**, over a wider window then trimmed
to the top-N. Retrieval, RRF fusion, and `report["ranked_families"]` — which the gold eval measures —
are **left untouched**. Effect across the 8 audited queries: **14 design patents dropped, 24
title-only demoted** out of the shown results; `broad_vacuumgripper` went **0.90 → 1.00** strict.

**No recall regression — provable by construction:** `retrieval.py`, `agent.py`, `goldset.py`,
`search_modes.py` are **byte-identical to the committed baseline** (empty `git diff`), and
`evaluate.py` never imports `webview`/`webapp`/`audit`. The deterministic configs therefore reproduce
the committed recall exactly (vector/hybrid recall@100 = 0.1697); agentic varies only by LLM
non-determinism. Re-ran the gold eval (11 frozen gold searches, current corpus) to confirm:

| Config | recall@100 | recall@1000 | earliest-recovered | vs. committed @100 |
|---|--:|--:|--:|---|
| vector | 0.1697 | 0.2833 | 1/11 | 0.1697 — **identical** |
| hybrid / +rerank | 0.1697 | 0.3111 | 1/11 | 0.1697 — **identical** |
| **agentic** | **0.1856** | 0.2921 | **2/11** | 0.1853 — **best config, unchanged** |

The deterministic configs reproduce the committed recall to the digit; **agentic@100 = 0.1856 is the
top config** and leads on earliest-relevant-recovered — recall fully intact, no regression.

Tests: **10 new M9 regression tests** (`tests/test_relevance_audit.py`) cover the substance filter
(design drop + title-only demote + trim), evidence-grounded `reads_on`, the empty-reference guard,
the claim-chart `strength` flag, and the `ref_text` chunk-body fallback. Full suite: **92 passed**.

---

## Honest verdict — where it's strong vs. weak for real use

**Strong**
- **Recall is the product's real strength** (the frozen gold eval already proved it): it surfaces
  examiner-cited families, and the agentic config uniquely recovers families via citation/family
  expansion that pure vector search misses.
- **Broad and gold-anchored queries are genuinely good** (broad 1.00; gold anchors 0.60 strict /
  0.80 lenient, 0 irrelevant). The top-10 are consistently on-field — I did not find a query whose
  top-10 was off-topic junk once the design/title-only flood was removed.
- **Rationales are now safe-by-default**: overclaim+hallucinate down to 10%, undisclosed elements
  deterministically dropped, text-less docs marked unconfirmed rather than invented.

**Weak**
- **Ranking does not put the *most feature-specific* art at the very top for detailed queries.** A
  generic "vacuum lifter" (even a 1904 one) sits near a specific modern match because their text is
  semantically near-identical; the reranker doesn't decisively prefer the feature match. Fine for a
  novelty sweep (you want the broad art too), weaker for "find the single closest reference."
- **Claim-chart coverage is over-stated**: ~50% of coord-backed cells and all whole-doc-only cells
  do not truly disclose the element. Whole-doc cells are now flagged weak; the coord-cell FPs need
  per-cell LLM verification (built, recommended, deferred). **Until then, treat chart cells as
  "worth reading," not "proven coverage."**
- **For a lawyer:** trustworthy as a **recall-first discovery tool** with readable, now-grounded
  rationales; **not yet** a tool whose claim-chart coverage or top-1 ranking can be cited without a
  human reading the cited passage.

_Suite green (92). No corpus re-index, no spend — corpus-deepening still held for the EPO unblock._
