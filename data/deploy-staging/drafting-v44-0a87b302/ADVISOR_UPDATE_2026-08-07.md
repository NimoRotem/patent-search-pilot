# Prior art search pilot: advisor update

2026-08-07. Covers the roughly 24 hours since the last brief, in which your six answers were acted
on. Written to be readable on its own; the earlier brief and the technical report have the full
system description.

---

## 1. The project in one page

We are building a prior-art search system whose output is a **portfolio** of references that
collectively address a subject patent's disclosures, because the artefact is a legal argument about
novelty rather than a similarity ranking. If forty nine references answer the same nine of ten
disclosures, the fiftieth is worth more if it answers the tenth alone.

The system: 4.97M publications in Postgres with pgvector on a dedicated 8 vCPU / 62 GB host; per
subject an LLM decomposes the invention into aspects, nine local retrieval channels and a seven
source external fan-out produce about 15,000 candidates, weighted RRF with DOCDB family dedup and
EPC Article 54 date filtering narrows to about 8,500 families, an LLM screens 2,500, roughly 300 are
read in full and charted against the disclosure list with quoted evidence, and a greedy maximum
coverage pass selects the delivered 50. About 20 minutes per subject.

Benchmark: 50 subjects (30 dev, 20 holdout sealed), stratified by how much of each subject's cited
art the corpus holds, scored against examiner X/Y/A citations, family deduplicated, with disclosure
lists frozen and hashed before any run.

**Baseline going in: 24 of 266 cited families delivered, 9%.**

---

## 2. Your answers, and what they produced

### Q1, what does functional mean numerically. ANSWERED BY MEASUREMENT.

You proposed running the tools a searcher already has on our own benchmark rather than picking a
target by feel, with 35 to 50% as a placeholder parity band to be replaced by the measurement.

Built `eval/tool_baseline.py` and ran it over the 30 dev subjects, same gold, same family space:

| arm | XY recall@50 | XY recall@10 | subjects with at least one X in the top 10 |
|---|---|---|---|
| Google Patents "Similar Documents" | 1/247 = **0.4%** | 0.0% | 0/30 |
| Google Patents title search | 0/247 = **0.0%** | 0.0% | 0/28 |
| **ours** | 23/213 = **10.8%** | 2.8% | 5/26 = **19%** |

**The parity bar is essentially zero, so the 35 to 50% band is not supported.** Parity with the
best tool a searcher has is already met by roughly 25x. The target therefore has to come from your
product floor instead: at least one X family in the top 10 for a majority of subjects. We are at
19% of subjects, and that is now the number we are trying to move.

Why the gap is so wide is visible rather than inferred. For EP-2172390-A1 Google returns
EP2046508B1, DE60119760T2, EP3024999B1, all recent and topically adjacent. The examiner cited
BG-50640-A1, FR-2529131-A1, JP-H01160785-A, all old, foreign and structurally similar. Two
different notions of similarity, and only one of them is what an invalidity search needs.

Two integrity notes. The same API response carries `patent_citations`, which IS our gold set; only
`similar_documents` is read and an assertion pins that. And the keyword arm scored 0.0% on its first
pass because it was sent the benchmark's human label rather than a title, so Google returned
nothing; an empty result set is indistinguishable from a tool that found nothing relevant and would
have been reported as a baseline. Refixed, it now returns 100 results for 28 of 28 queries and still
finds zero examiner citations.

Not yet done: the commercial trial arm, which is the one most likely to be competitive.

### Q2, what to optimise. ADOPTED AS STATED.

Disclosure coverage as the thing we climb, examiner recall as a cheap blinded regression gate, an
attorney once at freeze. X and Y are now reported separately from A throughout.

### Q3, English siblings first. DONE, AND SIZED.

Built the family member inventory (plan step 3.2). For each of the 309 dev gold references we
cannot currently read, does its DOCDB family hold something readable?

```
  none_needed             4    1.3%   a readable sibling is already in our corpus
  fetch_english_member  173   56.0%   an English full text exists in a public source
  foreign_only          117   37.9%   no English full text anywhere in the family
  no_family_data         15    4.9%
```

In the `mostly_out` stratum, where 77% of the art is absent, 31 of 47 have an English sibling. So
your sequencing holds: **56% of what we cannot read is reachable in English**, and the foreign only
residue is 38% of the currently unreadable set, to be re-sized after substitution as you specified.

One caveat we owe you: eligibility could only be checked for 118 of the 309, because 166 gold rows
carry no subject filing date. Since "a family member is a retrieval proxy, never displayed
evidence" is a hard rule, that gap has to close before substitution ships. We have added
`evidence_publication_id` and `is_proxy_text` to every charted cell already, with a defect injected
test, so the rule is enforceable rather than aspirational.

### Q4, pay for measurement isolation. DONE, IN THE ORDER YOU SPECIFIED.

- **Corpus snapshot.** The database lives on its own VM, which had 267 GB free against 295 GB of
  data, so an in place clone did not fit. Attached a 400 GB disk (about $40/month, deleted after the
  pilot), cloned with `pg_basebackup` while the live app kept serving, and verified the clone
  identical on every table: 4,968,051 publications, 26,633,674 chunks all embedded, 9,028,024
  claims, 108 indexes including HNSW. Control on port 5433, treatment on 5434, separate disks so
  the latency half of the promotion gate stays measurable.
- **External replay cache.** `src/replay.py`, recording at the single external seam. Three modes,
  and in strict replay a miss is an unconditional run failure rather than a live call.
- **Comparability gate.** `manifest.comparable()` now treats replay state as a comparability
  variable and refuses two arms whose mode or versions differ. It also hashes the content of every
  `src/*.py` the pipeline imports, because the checkout turned out to be shared with a second agent
  and `git_dirty` was permanently true for reasons unrelated to the experiment.
- **LLM replay:** trimmed as you advised, and that trimming is exactly where the experiment then
  broke. See section 4.

### Q5, the 60 stub retrieval failures. PROBED, AND THE QUESTION DISSOLVED.

Two days of work as you framed it, and both parts came back as you predicted.

- **Sibling overlap:** 37 of the 60 (62%) are already in the English sibling cohort. Only 23 would
  survive acquisition as a genuine stub problem.
- **Bounded probe on 9 of them:** all 59 that have chunks are fully embedded, so no ingestion
  defect. Comparing each stub's exact best cosine against the real dense retrieval frontier, **8 of
  9 sit outside the window**. That is genuine semantic distance, not a bug, so we stopped as you
  said. They sit only just outside (0.23 to 0.32 against a 0.24 to 0.26 cutoff), so widening the
  window would recover some, which is precisely the threshold tuning you deprioritised.

---

## 3. The finding that reframed the project

Three measurements in sequence, each forced by the previous one.

**First, the cohort could not touch the biggest loss bucket.** We froze the acquisition cohort on
candidate status alone and committed it before looking at gold, so rule R1 is provable by commit
order rather than by assertion. It is honest: 79 of its 9,017 families are gold, 0.88%, so 99% of
the fetch is art no examiner in this benchmark cited. But by stage:

```
  TOP_50                 23 / 24    96%
  PORTFOLIO_EXCLUDED     45 / 51    88%
  FUSION_TRUNCATED        3 / 43
  NOT_RETRIEVED           1 / 118    0.8%
```

That is structural, not a threshold to tune: the rules require a family to have been screened,
which requires it to have been retrieved, and `NOT_RETRIEVED` is definitionally the set that never
was. **And no candidate derived cohort can reach them: only 17 of the 118 appear in ANY dev
subject's ranked pool.** 86% were never surfaced by a single search in the whole benchmark.

**Second, the corpus census.** Of 4,968,051 publications:

```
  a readable description (>= 6,000 chars)   18,739    0.4%
  any claim text                           800,783   16.1%
  substantial claims (>= 3,000 chars)      591,241   11.9%
  title and abstract ONLY                        ~   83.9%
```

The `expanded` tier, 2.53M publications, has **three** with a description. The semantic index is
built almost entirely on abstracts. That one fact explains the whole prior record at once: why 95%
of screened candidates are stubs, why eight ranking experiments refuted, why oracle injection could
not move delivery.

**Third, and this is the part that changes the conclusion rather than confirming it.** The missing
descriptions are not an accident. `src/incremental_ingest.py:305` documents a deliberate, measured
decision: skipping description paragraphs cut 71% of the chunk budget for 2.4% of relative recall
(recall@100 0.1658 to 0.1619). Its own comment predicted what we later measured independently:

> What it DOES cost is evidence, not retrieval. Paragraphs are what the claim chart quotes... the
> OPS backfill moved recall by zero but did raise lenient p@10 0.54 to 0.594.

So we were wrong to call the cohort mis-aimed. It covers 45/51 `PORTFOLIO_EXCLUDED` and 23/24
`TOP_50`, which is exactly where descriptions are documented to pay. **The treatment should be
judged on grounding and portfolio movement, not on the 44%,** and the 44% needs a different fix
that prior evidence says is not descriptions.

**Fourth, the cheap part.** The corpus already holds **13,869,170 paragraph rows across 418,245
publications**, of which only **19,580** were ever chunked. **398,665 publications have their
description sitting in the database, unindexed.** For most of the cohort there is nothing to buy,
only something to index.

---

## 4. Where the last 24 hours actually went, including what broke

**Acquisition executed.** One BigQuery query at $7.84 replaced several thousand rate limited calls:
6,264 targets, 6,255 returned, 5,362 with an English description over 3,000 characters, 195 MB.
Your instruction to measure the mirror rather than assume it mattered: **US 5,362 of 5,404, EP 0 of
613, WO 0 of 247.** `patents-public-data` carries no EP or WO description text at all, so those 860
need EPO OPS. Loaded into the treatment only, behind a startup assertion on the port, adding 90,857
paragraph chunks.

**Embedding, and a wrong diagnosis corrected.** The job looked like 8 hours. Measuring properly
showed the work was bursty, so short samples read only the fast half. Timing the halves separately:
API 4.1s per 200 chunks, database 165.3s per 200 rows, so the database was 40x the API. We first
raised `shared_buffers` from the Docker default of **128 MB on a 62 GB machine** to 16 GB on both
instances, which was worth doing but did not move the rate, because the index far exceeds RAM and
the cost is random read latency per HNSW insertion. Sharding into four parallel writers took it from
about 500 to about 2,000 chunks per minute and it finished in 35 minutes.

**Treatment corpus validated** before trusting it: three probes, each a sentence from the middle of
a newly indexed description and verified absent from every other chunk kind of that publication, all
return the source publication at rank 1.

**The A/B, and why it is not finished.** The control arm completed cleanly: 28 subjects, 10 hours,
on the untouched corpus, recording the external world as it went. The treatment arm ran two subjects
and we stopped it, because the replay cache grew, which meant it was fetching a different external
world rather than reusing the control's.

Root cause, confirmed on the same subject in both arms:

```
control   brief 1,951 chars   "...grip unit for a vacuum hand-operated laying device, used in
                               conjunction with a suction plate to lift and move objects..."
treatment brief 2,304 chars   "...grip unit for a vacuum hand-operated laying device, which is
                               used in conjunction with a suction plate..."
jaccard 0.41
```

The subject brief is regenerated by an LLM on every run. The query plan is keyed on that brief, and
the external cache is keyed on the resulting queries, so an uncached step at the very top of the
funnel defeated the caching of everything below it. This is precisely the trimming decision in your
Q4 answer, and it turned out to be load bearing rather than optional. We would rather report that
than a confounded delta.

The fix is cheap and, importantly, lives in `eval/benchmark.py` rather than `src/`, so it does not
change `src_tree_hash` and the completed control arm stays valid: have the treatment reuse the
control's ingested query verbatim instead of re-ingesting. Then the brief, the plan and the external
results are identical by construction and the corpus is the only difference.

**A process hazard worth your awareness.** A second agent is working in the same checkout on a
drafting feature. It switched the checkout onto its own branch underneath us, one of its edits
silently reverted a line this experiment depends on, and one of our own `git add -A` calls swept 339
lines of its unfinished work into an unrelated commit, which was our error. Nothing was lost, no
history was rewritten, and our work is anchored on its own branch. The `src_tree_hash` guard exists
because of this: we cannot prevent a second agent editing the tree, but a run that straddles a code
change will now refuse to report itself as valid.

---

## 5. What we are doing right now

1. Wiring query reuse into the treatment arm so both arms share one ingested brief, then re-running
   the treatment against the completed control. About 8 hours.
2. Reporting the comparison only if `manifest.comparable()` returns exactly one difference,
   `corpus_snapshot`. Any second difference is a refusal, not a caveat.
3. Expecting movement in charting and portfolio rather than in `NOT_RETRIEVED`, per section 3.

Queued behind it: index the 398,665 publications whose descriptions we already hold, which is a much
larger and cheaper intervention than the 5,362 we bought; and re-size the foreign only residue after
substitution, which is the input to your Q3 phase two.

---

## 6. Questions

1. **Is the target now right?** With measured parity at roughly zero, we propose dropping recall
   parity as the version one goal and adopting your product floor alone: at least one X family in
   the top 10 for a majority of subjects, currently 19%. Does that match your intent, given the
   parity half of the definition turned out to be trivially satisfied?

2. **Does the two-tier evidence change your ordering?** The corpus was built shallow on a measured
   finding that descriptions buy 2.4% of relative recall. Our funnel says the same thing from the
   other side. If descriptions cannot move retrieval, then the 44% `NOT_RETRIEVED` needs a
   different instrument entirely, and we do not have a candidate. Disclosure level retrieval was
   the plan's answer, but 86% of the missing art is never surfaced by any query we generate, so we
   are not confident it is the right one either.

3. **Cheap indexing versus paid acquisition.** 398,665 publications already have their text stored
   and unindexed, against 5,362 we paid to fetch. Indexing all of it is roughly 12.7M new chunks,
   a 48% index growth, and on last night's evidence the write cost is the binding constraint rather
   than the fetch. Is a corpus wide re-index worth doing before any further acquisition, and if so
   would you drop and rebuild the HNSW index rather than insert into it?

4. **How much determinism is enough?** We trimmed LLM replay on your advice and it cost us a
   treatment arm. Caching the ingest step fixes this instance. Is the right general rule to cache
   every LLM call that feeds a cache key, or to accept the confound and rely on repeats?
