# Why the best references did not come back: a measured study

Case: `rotem.ai/patents/report/adhoc-584455f78ae2`, an upload of **EP3707092B1** (GRABO's own
"Vacuum gripper"), novelty mode, wide (federated), search focus **Claims**.

Two references Nimo named:

| reference | what the report did | what is actually true |
|---|---|---|
| **US-10625955-B2** "Electric vacuum suction lifter" | card 11, relevancy **45**, "not among the ones read in full" | in the corpus, fully embedded (21 claims, 46 paragraphs). Read in full it discloses **10 of the 12** claimed features with grounded verbatim quotes |
| **DE-3724659-A1** "Sucker for vacuum lifting devices" (1989) | never shown. It was retrieved, at **rank 244 of 2402** | in the corpus with claims. It discloses a *Distanzstück* that "begrenzt die Zusammenpressung der Dichtlippen", i.e. **the bracing structure that limits over-compression of the seal**, which is the characterising feature of EP3707092 claim 1 |

Neither is a corpus problem. Both documents were in the index and embedded. Every number below
was measured against the live corpus (4.95M publications, 26.5M chunks) on 2026-08-02.

---

## 1. The query is the single biggest cause

The search did not run on the patent. It ran on a 4,737 character LLM brief, of which
**54% is figure-description prose** ("FIGURE 1 depicts ... an outer wall 210 and an inner wall
220 ... thickness T and height D1"). Every patent has that prose, so it is pure dilution.

Rank of **US-10625955-B2** in the dense channel, same corpus, same day, only the query text changed:

| query | dense (all chunks) | claim_dense |
|---|---|---|
| the live query (brief + figure prose), 4,737 chars | **#528** | not in top 1,000 |
| the same brief with the figure prose deleted | **#35** | #273 |
| claim 1 verbatim | not in top 1,000 | not in top 1,000 |
| a 30 word plain-language essence sentence | **#2** | **#1** |
| RRF over 14 short queries (essence + brief + 12 elements) | **#37** (found by 9 of the 14) | |

Deleting the figure paragraphs alone is a 15x rank improvement. A short essence sentence is a
260x improvement. Claim 1 verbatim is *worse* than the brief: it is legalese about one narrow
feature, not a description of the device.

**Change:** stop searching with one long brief. Build a query *set*, embed each separately, fuse.
Keep figure descriptions for the image channel and the reader, never in a retrieval vector.

## 2. "Claims" focus silently removes description text, and that is where the matches are

With `search_focus=claims` the pipeline drops the general `dense` channel entirely
(`presets["claim_agentic"]` has no `dense`). US-10625955-B2's best passage is description
paragraph p0006 at cosine 0.795; its best claim chunk is 0.771 and outside the funnel. Across
the 25 cards the report displayed, the winning passage was a claim in only 10 cases:
paragraph 6, claim_own 7, claim_resolved 3, abstract 2, whole 2, figure_caption 1, none 4.

The UI presents this as an expansion ("Claim focus expands claim-level hits through patent
families and citations"), not as a restriction.

**Change:** make claim focus a *boost* on claim chunks, not a filter that removes description
chunks. Or relabel it honestly.

## 3. The top of the funnel is ~900 publications out of 4.95M

`CHUNK_FETCH = 4000` chunks aggregated to publications yields **893 distinct publications**
(0.018% of the corpus). `PUB_CAP = 1000` never binds. `hnsw.ef_search = 200` with
`max_scan_tuples = 12000` also caps the ANN sweep well below a `LIMIT 4000`.

The note in the codebase that "CHUNK_FETCH 4000 -> 12000 moves recall@100 by exactly 0.0000" was
measured at 107k publications and 2.7M chunks. The corpus is now 46x larger. That conclusion has
expired.

Depth alone is not the fix, though. Measured with the live query, going from 4,000 to 60,000
chunks inside the 8 seed CPC branches (123 seconds) moved US-10625955-B2 not at all (#524 either
way) and only brought DE-3724659-A1 in at #7,189. **Depth without a better query buys nothing.**

Worth knowing: the whole on-field universe (the 8 seed CPC branches) is **81,890 publications**.
Restricting the dense search to it changes nothing at today's depth (892 vs 893 publications
returned), because the head is already on-field. It becomes a lever only in combination with a
deep sweep.

## 4. Two new channels are throttled to 8 results each

`_merge_channel(rep, name, scored, head_keep=12, take=8)`. The document-chunk channel found
200 families and the image channel 14; at most **8 of each** are spliced into the ranking.

US-10625955-B2 entered the report **only through the image channel**. No text channel surfaced
it. It reached the page because its drawings look like EP3707092's drawings.

(Measured aside: the doc-chunk pooling multiplies cosine by a per-kind weight. Re-pooling the same
38 document vectors by max cosine or by RRF changes US-10625955-B2 from #343 to #281 / #390, so the
weighting is not the problem here. The `take=8` cap is.)

## 5. The final ranking is decided from a 900 character snippet

`relevancy.py` scores each card 0-100 with an LLM that sees only `_matched_text(c, 900)`:
title, assignee, date and the matched passage. That score is the displayed relevancy and the
sort key. The same model, given the reference's real text:

| reference | score from the 900 char snippet | score after reading the full text |
|---|---|---|
| US-10625955-B2 | **45** | **85** (repeated 3x, stable) |
| US-12115659-B1 | 60 | 85 |
| DE-3724659-A1 | never scored | 65 to 75 |
| US-11999030-B2 | 95 | 95 |

Text budget matters monotonically. US-10625955-B2: 900 chars -> 0, all claims (18.5k) -> 75/80/75,
full text (144k) -> 85/85/85.

## 6. The app already reads 50 references in full, and throws that reading away

`deep_analysis` read 50 references, 3.84M characters, in 45 seconds. It found 5 or more
disclosed/partial features in 18 of them. **Twelve of those 18 are not on the page at all.**
The reading feeds a tab, never the ranking.

## 7. The reading list and the page are two different lists, and the reading is never refreshed

`deep.json` was written 08:36. The final report was written 08:40 and the final view 08:41.
`ensure_report` returns "ready" as soon as a **partial** report is on disk, so opening the page
mid-run fires `deep_analysis.ensure` against the partial ordering. The result is cached, and
`deep_analysis.invalidate` is defined but **never called from `webapp.py`**. So the reading is
frozen against an ordering that no longer exists.

That is literally the message Nimo saw: US-10625955-B2 is card 11 and says "not among the ones
read in full", because the 50 that were read came from a different list.

Second defect in the same function: `_extend_to` slices `ranked_families[len(cards):]` on the
assumption that the 25 cards are `ranked_families[0:25]`. After the listwise rerank and the
federated merge they are not, so ranks 26 to 50 of the reading list are an arbitrary offset.

## 8. The cross-encoder depth is 25, not the 50 the code says

`retrieval.RERANK_TOP = 50` carries a comment explaining that it was raised from 25 "because the
deep full-text analysis reads the top 50". On the live path it is dead: `agent._final_rank`
takes `top=25` and calls `rerank_families(..., top=min(25, len(fam)))`. The report records
`cross_encoder_rerank: {scored: 25, requested: 25}`, while the progress heartbeat is handed
`retrieval.RERANK_TOP` and tells the user 50.

## 9. RRF's agreement bias buries thin old documents

DE-3724659-A1 is 9,160 characters total. In a 14 query fusion it appeared in **1** of the 14
and landed at #12,207, even though its best single-query rank was #1,070. RRF rewards agreement
across queries, which a one paragraph 1989 utility filing can never produce.

## 10. Only 25 of 2,402 families are analysed or shown

The ranked tail exists (`/api/more-references`, capped at 300, 25 per page) but returns
bibliographic rows only, with no score and no analysis. DE-3724659-A1 at rank 244 was reachable
in principle by paging ten times and reading titles.

## 11. One upstream data hazard worth defending against

`publications.abstract` for US-10625955-B2 is **a different patent's abstract** (a touch display
device with optical adhesive). This is upstream: patents.google.com/patent/US10625955B2 shows the
same wrong abstract, and both members of the family carry it. Prevalence is low: 0 of 1,500
sampled US publications had an abstract sharing no content word with their own title or claim 1.

It matters because a screener that reads title + abstract scores this patent **0, three times out
of three**. Any abstract-based scoring must cross-check the abstract against the title and claim 1
and drop it when they disagree.

---

## What to change, in order of measured value

### A. Judge on the full text, and rank on grounded evidence (biggest win, ~1 minute of model time)

Measured end to end on the **live** candidate list, retrieval untouched:

1. Screen `ranked_families[:300]` with a batched LLM (25 per call, title + claims + abstract, with
   the abstract dropped when it disagrees with the title/claims): **12 calls, 25 seconds**.
2. Read the top 60 in full and chart each of the 12 features: verdict plus a verbatim quote,
   gated by the existing `grounding.grounded` (span 0.70 / bigram 0.30): **60 calls, 24 seconds,
   3.1M characters**.
3. Rank by grounded features weighted by rarity across the charted set, `sum(log(N/df))`, so
   "portable vacuum gripper" (34 of 60) counts far less than "bracing structure protrudes less
   than the seal" (9 of 60).

Result:

| reference | live report | after |
|---|---|---|
| **US-10625955-B2** | card 11, score 45 | **rank 3** (10 of 12 features grounded) |
| **DE-3724659-A1** | rank 244, never shown | **rank 18** (6 of 12 grounded, including both bracing features) |
| US-11999030-B2 | card 1 | rank 1 (12 of 12) |
| US-11731291-B2 | card 3 | rank 2 (12 of 12) |

**21 of the new top 25 never appeared on the live page**, all of them from within the top 300 the
pipeline had already ranked: WO-9301026-A1 (fusion 74, 10 features), US-3240525-A (179, 9),
EP-2489615-A1 (111, 8), DE-19646890-A1 (96, 8), US-9221623-B2 (220, 7).

Why rarity weighting matters: a free-form 0-100 full-text score is not safe on its own. In an
earlier run it gave **85** to nine Soviet-era records that have **zero characters of text** in the
corpus, scoring them off the title alone, and the model invented 8 to 12 quotes each for them.
Counting only grounded quotes gives them 0 and is not foolable that way.

### B. Search with a query set, not a brief

Per search: one essence sentence (<= 35 words: device, power source, characterising feature),
5 alternative-vocabulary phrasings, the 8 to 12 elements already extracted, and each independent
claim. Embed each, run each as its own ANN pass, fuse. Never put figure prose in a query vector.
Cost measured: 21 passes in 101 seconds, and they parallelise.

Fuse with `0.5 * normalised RRF + 0.5 * normalised best cosine` so a document found strongly by
one query is not buried by the agreement bias (item 9).

Caveat, measured: replacing the current channel mix with a dense-only query set **loses**
DE-3724659-A1, which reaches the list through citation/family expansion. This change is
**additive**, not a replacement. Keep every existing channel.

### C. Do not let claim focus delete description retrieval

Always run `dense`; add `claim_dense` on top with its own weight. One line in
`retrieval.presets`.

### D. Fix the reading list (small, pure defect)

1. Call `deep_analysis.invalidate(slug, REPORTS)` when the final report supersedes a partial, or
   stamp the cache with the report/view hash it was built from and rebuild on mismatch.
2. Do not start `deep_analysis` from a partial view at all: gate `ensure` on
   `not rep.get("partial")`.
3. Make `_extend_to` top up from the *displayed* order, not from `ranked_families[len(cards):]`.
4. Show 50 analysed cards, not 25 (`_DISPLAY_TOP`), since 50 are already read.

### E. Make `RERANK_TOP` real

`agent._final_rank(top=...)` and its inner `min(25, ...)` should both read
`retrieval.RERANK_TOP`. Measured cost at 25 passages is roughly 40 seconds, so 50 is roughly 80
seconds, and it runs once per search in the background.

### F. Raise the channel merge caps

`_merge_channel(..., head_keep=12, take=8)` throws away 192 of 200 document-chunk families. With
a wide screener in front (item A), there is no reason to cap at 8. Take 50 to 100 from each and
let the screen sort it out.

### G. Widen the first stage, now that a screener can absorb it

Fetch to a publication target rather than a chunk count (fetch until N distinct publications, not
`LIMIT 4000` chunks), and raise `hnsw.ef_search` (the build caps at 1000). Target roughly 3,000
to 5,000 families into the screener rather than 300. Screening 3,000 is about 120 LLM calls and
under 4 minutes at the measured rate.

### H. Two lists, not one

DE-3724659-A1 is not a novelty reference: it discloses 2 to 6 of 12 features. It is the best
single disclosure of the **characterising** feature, which is what an inventive-step attack needs.
The report already computes a `combination_view` and never ranks by it. Alongside the main list,
show "best reference per feature", ranked by rarity. DE-3724659-A1 tops
"bracing structure prevents over-compression" (13 of 60 charted references cover it).

### I. Put this case in the gold set

The frozen 11 query gold set cannot see any of this. Add EP3707092B1 as an anchor with
US-10625955-B2 and DE-3724659-A1 as known-relevant, so a future change to any of the above can be
measured rather than argued. Per the M9 lesson in this repo, a retrieval-layer change that looks
like an improvement can regress recall, so measure before shipping.

---

## Reproduction

Every script used is committed under `eval/recall_study_2026-08-02/`, with a README saying what
each one measured. They are probes, not tooling: paths are pinned to the instance-3 checkout and
they read the live corpus directly. `exp11.py` is the end-to-end demonstration of change A. Report
artefacts: `data/reports/adhoc-584455f78ae2.{json,view.json,deep.json,meta.json}`.

---

# BUILT, 2026-08-02. What actually changed, and what it measures now

The whole of A to H above is implemented and deployed on instance-3 (`patent-results`, :8631).
Everything below was measured by re-running the ORIGINAL search end to end on the rebuilt
pipeline (same upload, same mode, same claims focus).

## Result on the case that prompted it

| | before | after |
|---|---|---|
| **US-10625955-B2** | card 11, score **45** from a 900-character snippet, "not among the ones read in full" | **card 10**, score **45** from a **142,823 character** reading with grounded, located quotes |
| **DE-3724659-A1** | rank **244 of 2,402**, never shown | **card 37 of 50**, read in full, and the leading disclosure of the characterising feature |
| references read in full | 50, chosen from a stale ordering | **184** (8.67M characters, 172 s), chosen by a wide screen |
| candidate families ranked | 2,402 | **7,062** |
| candidates LLM-screened | 0 | **600** (11.5 s) |
| cards on the page | 25, ordered by a snippet score | **50, every one read in full**, ordered by evidence |
| cross-encoder depth | 25 (the documented 50 was dead code) | **50, applied** |
| wall clock | ~6 min | **~15 min** |

Top of the new list: US-11999030-B2 (96), US-11731291-B2 (83), US-5681022-A (56),
EP-0648187-A1 (54), US-9919432-B1 (51).

## New modules

**`src/query_set.py`**: search with a query SET, not one long brief. `retrieval_text()` strips the
folded-in figure description from anything that reaches an embedding (the figures still go to the
image channel and to the reader). `build()` returns an essence sentence, five
alternative-vocabulary phrasings, the de-figured brief, every element and every independent claim.
One LLM call, cached; a failure degrades to brief + elements.

**`src/deep_rank.py`**: the stage that now decides the order.
1. Screen the head of the ranked list in batches of 25 on title + claims + abstract, with the
   abstract dropped when it shares no content word with the title or claims (the upstream
   wrong-abstract case scored 0 three times out of three).
2. Read the top 150 by screen IN FULL, plus the top 60 of the retrieval order no matter what the
   screen thought, so a screen miss can never cost a reference the old pipeline would have shown.
3. Score each on **grounded, rarity-weighted evidence**: every cell must carry a verbatim quote
   that passes `grounding.grounded` and is located by code, weighted by `log(N/df)` over the
   references actually read, blended 55/45 with the reader's holistic 0-100 verdict, and that
   holistic number is **gated** on the reference having been read and having grounded at least one
   quote. A reference that leads a rare feature gets explicit credit and the card says so.
4. Publishes the readings straight into `deep_analysis`'s cache, so nothing is read twice.

## Changed

- `retrieval.py`: `claim_agentic` now includes `dense` (claim focus BOOSTS claims, it no longer
  deletes description retrieval). Seed passes run a wide funnel (`SEED_CHUNK_FETCH` 9,000,
  `SEED_PUB_CAP` 2,500, `ef_search` 400, `max_scan_tuples` 60,000) while the ~20 element passes
  keep the cheap profile, because measured width only pays on the whole-invention passes.
- `agent.py`: runs the query set as seed-bucket passes; `_final_rank` honours
  `retrieval.RERANK_TOP` (it hard-coded 25 in two places); the cross-encoder and the element
  decomposition now see the de-figured text; `SEED_TOPK` 200 -> 800.
- `webapp.py`: `_DISPLAY_TOP` 25 -> 50, `MERGE_TAKE` 8 -> 60, `order_cards_by_evidence()` (read in
  full outranks screened-only, ties by score then incoming order), `deep_analysis.ensure` gated on
  `not partial`.
- `deep_analysis.py`: `VERSION` 2 -> 3 (invalidates every cache written against a partial,
  pre-listwise ordering); features and claims are now TWO focused reads, because asking for 12
  feature rows and 13 claim rows in one answer made the model economise (the same reference
  grounded 10 of 12 features asked alone and 2 asked together); `_extend_to` tops up from the
  families the cards do not cover instead of `ranked_families[len(cards):]`; the reader also
  returns a holistic `overall` score.
- `webview.py`: cards carry the evidence score, what it grounded, whether it was read and what it
  leads on; a read reference is protected from the text-shape demotion; federated hits dedup
  against the corpus across the dropped-leading-zero US pre-grant spelling (the same invention was
  being rendered twice, once as a corpus card and once as a federated card five places higher).
- `rerank_pool.py`: the cross-encoder timeout scales with the passage count (a flat 240 s timed
  out at the real depth of 50 and silently fell back to identity order).
- `templates/report.html` + `static/style.css`: a **Best reference for each feature** panel (one
  row per feature, rarest first, with the verbatim quote and its location) and a per-card
  "N/12 read in full" / "not read in full" line under the score.

## Tests

`tests/test_deep_rank.py`, 32 tests, every one anchored on a measured failure from this study.
Full suite: **685 passing, 0 failing** (16 min). Three real defects were found by these tests while writing them: the unread
score cap was not applied at the point of use, `leaders()` dropped the rarest feature to a rounding
comparison, and the two-call split had to be proven rather than assumed.

## Deliberately not done

- The `0.5 * RRF + 0.5 * best cosine` fusion blend (item B). Measured, a dense-only query set
  LOSES DE-3724659-A1, which reaches the list through citation expansion, and the wide screen
  already recovers everything the blend was meant to. Changing the fusion carries regression risk
  the frozen gold set cannot currently measure.
- The gold-set entry (item I). It needs a re-baseline run and belongs in its own change.
