# Corpus reachability — where the recall ceiling actually is

Decision-support for whether to fund a bigger ingest. Measured on the frozen 11-query gold set
(79 distinct gold families).

> **REVISED 2026-07-19 after the EPO OPS backfill ran.** The previous version of this document
> predicted that filling text depth was "the #1 lever" and would move `reachable@100` from ~0.18
> toward 0.4–0.5. **That prediction was tested and it was wrong.** The backfill was executed, it
> worked at the data layer, and recall did not move at all. The analysis below is rewritten around
> the measurement instead of the hypothesis. Full numbers: `data/eval/eval_report.md`.

**TL;DR: text depth was not the ceiling. The ceiling is query↔document semantic distance in the
dense channel — most gold families never enter the candidate pool at all, at any text depth.**

## Finding 1 — corpus coverage is high (86%) and unchanged

| | families | share |
|---|--:|--:|
| distinct gold families | 79 | 100% |
| in the 107,795-pub corpus (reachable) | 68 | 86% |
| missing from corpus | 11 | 14% |

The OPS backfill added **no publications** (107,795 before and after) — it only adds text to
documents already present. The 14% width gap is untouched and remains unreachable at any k.

## Finding 2 — text depth was filled, and it changed nothing (the disproved hypothesis)

Text depth genuinely improved. Corpus chunks 1,838,952 → 2,311,784; ~52k claims and ~360k
description paragraphs added. Gold families carrying claims went from 22 (at the time of the original
analysis) to 43. Of the 79 gold families, **12 were given genuinely new text on 07-19**, and 11 of
those 12 now carry fully-embedded claims.

The new text is verifiably live in the index: self-retrieval of OPS-added claim chunks under the
app's exact settings returns the source publication top-1, **8/8**, cosine 0.917–0.948. Gold families
have **zero** unembedded chunks — they were fully vectorised before the eval ran, while ~450k
non-gold chunks still await embedding. The gold set was, if anything, privileged.

Result:

| Config | recall@100 before | after |
|---|--:|--:|
| keyword | 0.0152 | 0.0152 |
| vector / hybrid / hybrid_rerank | 0.1697 | **0.1697** |
| agentic | 0.1856 | 0.1874 (LLM noise) |

Candidate-level, the retrieved gold sets are **literally identical** — deepening 12 gold families
moved **0** of them into or out of the pool for vector and hybrid. Deeper cut-offs slightly
regressed (keyword `r@1000` 0.1356 → 0.1078).

**Conclusion: "thin text" was a correlate of low recall, not its cause.** Thin families ranked badly
because they are semantically distant from the queries (old, non-English, differently-worded art) —
the same property that made them thin in BigQuery in the first place.

## Finding 3 — the actual binding constraint

Measured per query, the dense channel returns ~1,120 distinct publications from a 4,000-chunk budget,
so `PUB_CAP = 1000` binds before the chunk budget does. Within that pool, gold families are almost
absent:

| query | gold families anywhere in the dense candidate pool |
|---|--:|
| grabo_gripper_novelty | 11 of 44 |
| grabo_gripper_inventive | 13 of 44 |
| grabo_extended_frame | 14 of 49 |
| grabo_de_utility_xling | 2 of 7 |
| schmalz_sauggreifsystem | 1 of 4 |
| schmalz_vacuum_clamp | 5 of 7 |
| probst_stone_lifter_xling | 2 of 6 |
| probst_kerb_lifter | 1 of 9 |
| nl_handheld_vacuum_seal_sensor | 2 of 5 |
| nl_porous_surface_gripper | 2 of 6 |
| **nl_robot_eoat_vacuum** | **0 of 3** |

A gold family that never enters the candidate pool cannot be rescued by giving its documents more
text, by reranking, or by raising k. `nl_robot_eoat_vacuum` scoring 0.0 in every configuration
before *and* after the backfill is fully explained by this row.

## Finding 4 — deepening non-uniformly *hurts* the lexical channel

`chunks.tsv` is a generated column, so BM25 sees backfilled text immediately, with no embedding step.
The backfill added large amounts of text to *existing, overwhelmingly non-gold* publications, giving
them far more surface to match query terms. Keyword `r@1000` fell 0.1356 → 0.1078 and hybrid `r@500`
fell 0.2747 → 0.2467. Any future backfill should be treated as a **ranking-affecting change**, not a
free data improvement, and re-evaluated on the gold set before being called an upgrade.

## Finding 5 — the 11 missing gold families (unchanged)

Resolved via BigQuery; they sit outside the US/EP/WO/DE seed jurisdictions: FR 4 (1953–1965, old
French vacuum-lifting art), CN 1 (2017), 6 unresolved (very old / non-CPC-classified). Corpus
expansion can recover at most 14% of gold and the recoverable part is dominated by old French art.

## Revised recommendation

1. **Do not fund more text depth as a recall lever.** It is measured at ~zero for recall@100. Finish
   the running backfill for its *display* value — deeper documents demonstrably give the LLM judge
   and a human reader real passages to check (lenient p@10 rose 0.54 → 0.594, and whole-doc-only
   claim-chart cells fell 8 → 6) — but do not book it as retrieval improvement.
2. **Attack the semantic gap first — this is where the ceiling now is.** The concrete failure is
   gold families absent from the dense pool entirely. Candidates, cheapest first:
   - **Multi-vector / query expansion**: embed the subject's individual claim elements as separate
     queries and union the pools, rather than one whole-query vector. The agentic config already does
     a weak version of this and is the only config that beat the baseline at all.
   - **Cross-lingual retrieval**: the two permanently-0.0 queries are both cross-lingual or
     neighbouring-CPC. Embedding DE/FR text and English queries into one space is being asked to do
     a lot of work; translating queries into DE and searching both is cheap to test.
   - **Raise `PUB_CAP`** — it currently binds at 1000 while the chunk budget would supply ~1,120.
     Nearly free; likely small but positive.
3. **Re-test `hybrid_rerank`'s value.** The cross-encoder has now changed recall by exactly 0 across
   two full evaluations. It costs CPU and latency on every query for zero measured recall benefit;
   its only defensible role is top-25 presentation order.
4. **A small FR + GB expansion (~$15–25 BigQuery)** remains the only lever that can touch the 14%
   width gap. Still bounded, still honest, still optional — but note it is now the *second* lever,
   not the first, and it cannot help until the semantic-gap problem is fixed, or the new documents
   will simply fail to enter the candidate pool too.
5. **Skip the full worldwide ingest (CN/KR/JP, +42k pubs, ~$40–60).** Unchanged: it recovers one
   missing gold family.

## Honest expansion vs teaching-to-the-test (unchanged)

- **Honest field-coverage expansion** = ingest more vacuum-gripping + neighbouring-CPC art in
  under-covered jurisdictions (FR/GB). Helps future, unseen searches.
- **Teaching-to-the-test** = ingesting the 11 missing gold publications by number. **Do NOT do this.**
  The gold set exists to *measure* coverage, not to be back-filled into the corpus.

Net: the pilot's recall ceiling is a **retrieval-semantics** problem, not a text-depth problem and
not primarily a corpus-size problem. The free text-depth fix has now been spent, and measured at zero.
