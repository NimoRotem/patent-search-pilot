# Pilot Evaluation — post-OPS re-run (5-config ablation, spec §8)

_Generated 2026-07-19 · 11 frozen gold searches · 79 distinct gold families · corpus 107,795 pubs_

## TL;DR — the text-depth backfill did NOT improve recall

**`recall@100` is bit-identical to the committed pre-OPS baseline for every deterministic config
(keyword 0.0152, vector/hybrid/hybrid_rerank 0.1697).** Nine of the eleven per-query rows are
unchanged to the digit. The only movement is in `agentic`, which is LLM-non-deterministic and moved
by +0.0018 (noise). At deeper cut-offs recall slightly **regressed** (keyword `r@1000`
0.1356 → 0.1078).

This is a negative result and it is reported as such. The EPO OPS backfill did exactly what it was
supposed to do at the *data* layer — it added real claims and description text to 3,475+ publications
and deepened 12 gold families — and that produced **no measurable retrieval gain**. The hypothesis in
`REACHABILITY.md` ("the ceiling is text depth; OPS should move `reachable@100` from ~0.18 toward
0.4–0.5") is **not supported by this measurement**.

Nothing in the gold set, the matching logic, or the metric definitions was changed. `goldset.json` is
byte-identical to the committed version (`md5 e2e381c5…`), and `evaluate.py` / `goldset.py` have no
uncommitted diffs.

---

## 1. Headline — mean family recall@k (macro-avg over 11 gold searches)

| Config | r@100 before | r@100 after | Δ | r@500 before | r@500 after | Δ | r@1000 before | r@1000 after | Δ | earliest before | after |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|:--:|:--:|
| keyword | 0.0152 | 0.0152 | 0 | 0.1056 | 0.0926 | **−0.0130** | 0.1356 | 0.1078 | **−0.0278** | 0/11 | 0/11 |
| vector | 0.1697 | 0.1697 | 0 | 0.2603 | 0.2603 | 0 | 0.2833 | 0.2833 | 0 | 1/11 | 1/11 |
| hybrid | 0.1697 | 0.1697 | 0 | 0.2747 | 0.2467 | **−0.0280** | 0.3111 | 0.3111 | 0 | 1/11 | 1/11 |
| hybrid_rerank | 0.1697 | 0.1697 | 0 | 0.2747 | 0.2467 | **−0.0280** | 0.3111 | 0.3111 | 0 | 1/11 | 1/11 |
| agentic | 0.1856 | **0.1874** | +0.0018 | 0.2630 | 0.2616 | −0.0014 | 0.2921 | 0.3107 | +0.0186 | 2/11 | **3/11** |

`reachable_recall@100`: keyword 0.0152 → 0.0152 · vector/hybrid/+rerank 0.1774 → 0.1774 ·
agentic 0.1948 → 0.1969.

`hybrid_rerank` still equals `hybrid` at every k — the cross-encoder continues to change recall by
**exactly 0**, as previously measured. It only reorders the top-25.

## 2. Per-query family recall@100 (before → after)

| query | gold | families given NEW text | keyword | vector | hybrid | +rerank | agentic |
|---|--:|--:|--:|--:|--:|--:|--:|
| `grabo_gripper_novelty` | 44 | 8 | 0.0 → 0.0 | 0.0682 → 0.0682 | 0.0682 → 0.0682 | 0.0682 → 0.0682 | 0.0909 → 0.0909 |
| `grabo_gripper_inventive` | 44 | 8 | 0.0 → 0.0 | 0.0682 → 0.0682 | 0.0682 → 0.0682 | 0.0682 → 0.0682 | 0.1364 → 0.1364 |
| `grabo_extended_frame` | 49 | 8 | 0.0 → 0.0 | 0.0408 → 0.0408 | 0.0408 → 0.0408 | 0.0408 → 0.0408 | 0.0408 → 0.0612 * |
| `grabo_de_utility_xling` | 7 | 2 | 0.0 → 0.0 | **0.0 → 0.0** | **0.0 → 0.0** | **0.0 → 0.0** | **0.0 → 0.0** |
| `schmalz_sauggreifsystem` | 4 | 1 | 0.0 → 0.0 | 0.25 → 0.25 | 0.25 → 0.25 | 0.25 → 0.25 | 0.5 → 0.5 |
| `schmalz_vacuum_clamp` | 7 | 2 | 0.0 → 0.0 | 0.4286 → 0.4286 | 0.4286 → 0.4286 | 0.4286 → 0.4286 | 0.4286 → 0.4286 |
| `probst_stone_lifter_xling` | 6 | 1 | 0.0 → 0.0 | 0.1667 → 0.1667 | 0.1667 → 0.1667 | 0.1667 → 0.1667 | 0.0 → 0.0 |
| `probst_kerb_lifter` | 9 | 0 | 0.0 → 0.0 | 0.1111 → 0.1111 | 0.1111 → 0.1111 | 0.1111 → 0.1111 | 0.1111 → 0.1111 |
| `nl_handheld_vacuum_seal_sensor` | 5 | 2 | 0.0 → 0.0 | 0.4 → 0.4 | 0.4 → 0.4 | 0.4 → 0.4 | 0.4 → 0.4 |
| `nl_porous_surface_gripper` | 6 | 0 | 0.1667 → 0.1667 | 0.3333 → 0.3333 | 0.3333 → 0.3333 | 0.3333 → 0.3333 | 0.3333 → 0.3333 |
| `nl_robot_eoat_vacuum` | 3 | 2 | 0.0 → 0.0 | **0.0 → 0.0** | **0.0 → 0.0** | **0.0 → 0.0** | **0.0 → 0.0** |

`*` = the only cell that changed (agentic, LLM non-determinism).

**The two queries that previously scored 0.0 everywhere still score 0.0 everywhere** — even though
`grabo_de_utility_xling` now has 6 of its 7 gold families carrying real claims (2 added by this
backfill) and `nl_robot_eoat_vacuum` had 2 of its 3 gold families deepened today.

---

## 3. Attribution — was the deepening even applied to the families that matter?

Yes. And it still did nothing.

### 3a. What actually changed in the corpus

Provenance timestamps separate this backfill from earlier enrichment. This matters, because the
SerpApi/DE enrichment (910 pubs, 07-17 21:00 → 07-18 02:31) landed **before** the committed baseline
was measured at 07-18 06:56 — it is already priced into the 0.1697 number and cannot be credited here.

| source | ingested | gold families touched | in baseline? |
|---|---|--:|---|
| `bigquery:patents-public-data` | 07-17 19:46 | — | yes |
| `serpapi:google_patents` | 07-17 21:00 → 07-18 02:31 | 12 | **yes** |
| `epo:ops` | **07-19 14:12 → ongoing** | 11 | **no — new** |
| `local:sibling_recovery` | **07-19 14:52** | 1 | **no — new** |

Gold families by text-depth stratum (79 total): **12 given genuinely new text today**
(`deepened_NEW`), 8 enriched pre-baseline, 24 already carried BigQuery claims, 24 still thin,
11 absent from the corpus.

The backfill is real: corpus chunks went 1,838,952 → 2,311,784, and 11 of the 12 newly-deepened gold
families now carry fully-embedded claims (the 12th, `36011303`, returned no OPS text).

### 3b. Was the new text actually searchable? (yes — verified, not assumed)

A null result is only meaningful if the new vectors are live. Self-retrieval test: take OPS-added
claim chunks from newly-deepened gold families, query with each chunk's own text under the app's
exact retrieval settings, check the chunk's own publication returns top-1. **8/8 passed**
(cosine 0.917–0.948). Gold families also have **0 unembedded chunks** — they were fully embedded
before the eval ran, while ~450k non-gold chunks are still awaiting vectors. The gold set therefore
had a *privileged* position in this measurement and still did not improve.

### 3c. Candidate-level before/after (the attributable 2×2)

Both result files record, per channel, which gold families were actually retrieved. Comparing that
set before vs after, split by stratum:

| stratum | vector gained / lost | hybrid gained / lost | agentic gained / lost |
|---|:--:|:--:|:--:|
| `deepened_NEW` (n=12 fams) | **0 / 0** | **0 / 0** | 2 / 0 |
| `deepened_pre` | 0 / 0 | 0 / 0 | 0 / 1 |
| `bq_deep` | 0 / 0 | 0 / 0 | 3 / 5 |
| `thin` | 0 / 0 | 0 / 0 | 1 / 1 |
| `absent` | 0 / 0 | 0 / 0 | 0 / 0 |

For the deterministic configs the retrieved gold sets are **literally identical** — not merely equal
in aggregate. Deepening 12 gold families moved exactly zero of them into or out of the candidate
pool. Agentic's ±few are LLM sampling noise and net negative overall (net −1).

### 3d. Rank-level stratified recall (hybrid, post-OPS)

Per query-family pair, where each gold family actually ranked:

| stratum | n | recall@100 | recall@500 | recall@1000 | found at all | median rank when found |
|---|--:|--:|--:|--:|--:|--:|
| `deepened_NEW` | 34 | **0.0294** | 0.2941 | 0.3529 | 0.3529 | 245 |
| `deepened_pre` | 30 | 0.2000 | 0.2000 | 0.3000 | 0.3000 | 64 |
| `bq_deep` | 65 | 0.1231 | 0.2615 | 0.4000 | 0.4000 | 350 |
| `thin` | 34 | 0.0882 | 0.2059 | 0.2353 | 0.2353 | 142 |
| `absent` | 21 | 0 | 0 | 0 | 0 | — |

Newly-deepened families have the **worst** `recall@100` of any in-corpus stratum (0.029) while
having a *better* `found_at_all` rate than thin families (0.353 vs 0.235). Read plainly: the new text
does make these documents findable *somewhere* in the top-1000, but it does not push them into the
top-100.

**Do not read this table as a causal effect size.** The strata are not randomly assigned: OPS
deliberately targeted claimless EP/WO documents, which are systematically the hardest, most
non-English, thinnest-metadata families in the set. `deepened_NEW` scoring worst at rank 100 is
substantially *selection*, not *damage*. The clean causal statement is 3c's `0 / 0`.

---

## 4. Why deepening didn't help — diagnosis

1. **Text depth was never the binding constraint at rank 100.** The dense channel already returns
   ~1,120 distinct publications per query from a 4,000-chunk budget (so `PUB_CAP = 1000` is the
   binding limit, not the chunk budget). Within that pool, only a handful of gold families appear at
   all: 11, 13, 14, 2, 1, 5, 2, 1, 2, 2 and **0** across the eleven queries. For
   `nl_robot_eoat_vacuum` **not one gold family is anywhere in the dense candidate pool** — which is
   why no amount of text depth can fix its 0.0. The failure is query↔document *semantic* distance,
   not missing text.

2. **More text makes lexical ranking worse, not better.** `tsv` is a generated column, so BM25 sees
   OPS text the instant it lands, with no embedding step. The backfill added ~52k claims and ~360k
   description paragraphs to *existing, mostly non-gold* publications, giving non-gold documents far
   more surface to match query terms. That is the most likely cause of the keyword `r@1000`
   regression (0.1356 → 0.1078) and the `hybrid r@500` drop. Deepening a corpus non-uniformly
   dilutes a term-frequency ranker.

3. **The corpus grew no wider.** Publications stayed at exactly 107,795 — OPS adds text to documents
   already present. The 11 gold families absent from the corpus (14%) remain absent and remain
   unreachable at any k. That ceiling is untouched.

---

## 5. Honest caveats

- **The corpus was mutating during the run.** The OPS backfill was live throughout (position
  2,875 → 4,700 of 8,638), so later queries saw slightly more text than earlier ones. The drift adds
  text only to **non-gold** publications, which biases measured recall slightly *pessimistic* for
  later queries — it cannot manufacture the null result.
- **The remaining backfill cannot change these numbers.** `ops.py` orders gold-relevant families
  first and logged `(0 in gold families -> done first)` for the 8,638 still pending — every
  gold-family claimless publication was already filled. The remaining ~4,000 are all non-gold.
- **`agentic` is not reproducible to the digit** (≈40 LLM calls/query). Treat ±0.02 as noise.
- **`nl_robot_eoat_vacuum`'s gold set is questionable** — reported separately in §7, not netted into
  the headline.

---

## 6. Relevance / rationale audit (M9 re-run) — summary

Full detail in `data/reports/RELEVANCE_AUDIT_POST_OPS.md`. All 8 audited reports were regenerated on
the deepened corpus first (the on-disk gold reports predated the backfill).

| metric | M9 before | M9 after (grounded) | **post-OPS now** |
|---|--:|--:|--:|
| mean p@10 strict | 0.34 | 0.39 | **0.375** |
| mean p@10 lenient | — | 0.54 | **0.594** |
| rationale overclaim+hallucinate | 22% | **10%** | **26.3%** ⚠ |
| claim-chart coord-cell FP (weak+unrelated) | — | 50% (6/12) | **58%** (7/12) |

**The rationale-faithfulness regression is the most serious finding in this run** — it is worse than
the pre-grounding-filter baseline. Diagnosis and the partial measurement artifact behind ~2 of the 10
flagged cards are in §7 and the audit report.

---

## 7. Metric concerns — reported separately from the headline, as required

These are places where I believe the *measurement* is wrong. They are **not** netted into any number
above, and I did not adjust the gold set or the metrics to exploit them.

1. **A judge/generator text desync now inflates the rationale-hallucination rate.**
   `webapp._rationale` shows the model title + abstract + *the single best-matching passage* — which,
   post-backfill, is frequently a newly-added deep paragraph. `audit.ref_text` shows the judge
   title + abstract + claim 1, and only falls back to body chunks when abstract **and** claim 1 are
   both missing — a fallback the backfill just made much rarer by populating `claims`. So the model
   can now ground a statement in text the judge is never shown. Measured: of the 10 flagged cards,
   **2 are clear artifacts** (evidence ≥0.8 present in the full document, <0.6 in the judge's
   snippet). Correcting only those gives 8/38 = **21.1%** — still a regression versus 10%, so the
   headline finding stands either way. The desync is a real harness bug and should be fixed by
   showing the judge the same passage the generator saw.

2. **`nl_robot_eoat_vacuum`'s gold may be mis-specified.** Its 3 gold families come from an asserted
   `extra_gold_families = SCHMALZ_FAMS` list, not from examiner citations. One of them, `70050062`,
   is *"Console, clamping means and vacuum chuck device"* — a workholding vacuum chuck for fixturing
   parts on a machine table, not a robotic end-of-arm tool. Scoring a robotic-EOAT query against it
   is arguably measuring the wrong thing. **I did not change it** — the gold set is frozen by design
   and the query still scores 0.0 on the two defensible families.

3. **`nongold_rate@20` remains uninformative** (≈0.96–1.0 throughout) because the gold set is
   deliberately incomplete — it is a citation-derived answer key, not an exhaustive relevance
   labelling. It should not be read as a precision measure; §6's judged p@10 is the precision number.

---

## 8. Assessment

The pilot is **not** closer to production-usable than it was at the committed baseline. Retrieval
quality is unchanged where it matters (`recall@100`), slightly worse at depth for lexical channels,
and the one metric that clearly moved — rationale faithfulness — moved the **wrong way**.

What this run did buy is a much sharper understanding of where the ceiling actually is: it is **not**
text depth. The highest-leverage remaining levers, in order, are (a) fixing the query↔document
semantic gap that leaves gold families out of the dense candidate pool entirely, (b) restoring the
rationale grounding guarantee, and (c) the corpus-width gap for the 11 absent families. See
`REACHABILITY.md` for the revised analysis.
