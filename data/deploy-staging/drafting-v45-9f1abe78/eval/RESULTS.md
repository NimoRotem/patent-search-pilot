# Results log

One entry per change. **Hypothesis → change → measurement → verdict.** A change with no
measurement beside it has not earned its place, and a measurement that says "worse" is kept here
rather than deleted, because the point of the log is to stop the next session repeating it.

Target: the two frozen expert gold sets. `eval/attorney_gold.json` (Schmalz, 10 references) and
`eval/nguyen_gold.json` (Nguyen 18/915,337, 5 searchable references + 1 unreachable NPL).

```
PYTHONPATH=src .venv/bin/python eval/attorney_recall.py --gold eval/nguyen_gold.json <slug>
```

---

## R1 — Where does a search actually spend its time? (2026-08-16)

**Hypothesis.** The 2.5-hour wall clock is the model being slow.

**Measurement.** Profiled `analyse_reference` on six real documents from `adhoc-a2fec8ee8ba2`, and
separately measured each provider at the same 60k-character prompt size and 24-way concurrency.

```
pipeline, per reference       11 LLM calls, issued SEQUENTIALLY
pipeline @ CHART_WORKERS=24   21.8s per reference, 2.70 calls/s

vertex gemini-2.5-flash       median 2.8s   5.97 calls/s     no throttling at 24
anthropic claude-haiku-4.5    median 2.6s   7.96 calls/s     no throttling at 24
meta muse-spark-1.2           median 14.3s  0.99 calls/s     1 error in 24
```

**Verdict. REJECTED.** One provider alone sustains more than twice what the whole pipeline
achieved. The ceiling was ours: `analyse_reference` issued its eleven batches one after another,
with a comment saying this was deliberate to avoid a nested pool multiplying concurrent calls —
never measured, and wrong.

---

## R2 — Parallelise the batches inside a reference

**Hypothesis.** Issuing a reference's batches concurrently, bounded by per-provider semaphores
instead of by refusing to ask two questions at once, roughly doubles throughput.

**Change.** `deep_analysis.analyse_reference` fans its feature and claim batches out over
`DEEP_BATCH_WORKERS` (6). `src/model_pool.py` owns the real ceiling.

**Measurement.** Same six documents, same 24 workers:

| | before | after |
|---|---|---|
| 6 references, wall clock | 130.7s | **61.8s** |
| throughput | 2.70 calls/s | **6.04 calls/s** |
| per reference @ 1 worker | 96.8s | **47.4s** |

**Verdict. ACCEPTED — 2.1× on the stage that dominates a search.** Note the second provider is not
what did it: this number is with the reader on flash alone (see R4).

---

## R3 — A fenced reply is not a truncated one

**Hypothesis.** The flood of `SALVAGED a truncated response` warnings after adding Anthropic means
the fast pool is truncating and losing evidence.

**Measurement.** Reader prompt, seven real documents, 12 limitations each, max_tokens 12,000:

```
vertex-flash   rows 84/84   salvaged 0/7   quoted 60
haiku          rows 84/84   salvaged 7/7   quoted 50
sonnet         rows 84/84   salvaged 7/7   quoted 59
```

**Verdict. REJECTED, but it exposed a real bug.** Nothing was truncated — all 84 rows came back
every time. Vertex has a JSON response mode; Anthropic returns the object inside a ```json fence
or after a preamble, so every reply failed `json.loads`, fell into the truncation salvage, and was
flagged `_truncated`. `disclosures.extract` reads that flag as "the checklist is incomplete", so a
healthy provider's complete answer was being treated as degraded. Fixed with `_extract_json`.

---

## R4 — Is the fast pool good enough to READ with?

**Hypothesis.** A cheap model that screens well also reads well, so the whole reader can go on the
fast pool.

**Measurement.** US-11999030-B2 — the reference an examiner applied under 35 U.S.C. 102(a)(2) to
thirteen claims of the parent — asked the same 68 limitations with the same prompt:

| model | disclosed | partial | absent | claims with a DISCLOSED | quoted rows |
|---|---|---|---|---|---|
| vertex-flash | 20 | 8 | 40 | **7** | 28 |
| haiku | 9 | 14 | 45 | **3** | 23 |
| sonnet | 21 | 8 | 39 | **8** | 29 |

**Verdict. REJECTED, and this is the one that would have done damage.** Haiku finds less than half
the teachings in 90,000 characters. Putting the reader on the fast pool would have bought 2.1× wall
clock with the evidence the report is made of, and nothing downstream would have shown it — fewer
cells looks exactly like a document that discloses less.

Sonnet buys one claim over flash for twice the latency across seven hundred references. Not worth
it on the first pass; the second look already runs on the strong tier.

**Resulting design.** Three tiers, not two:
`fast` = screening and query facets (flash + haiku) · `read` = the full-text chart (flash) ·
`strong` = the refuter, the claim split, the second look (sonnet).

---

## R5 — Order the page by claims killed, and guarantee every claim an answer

**Hypothesis.** An attorney reads down a list looking for the document that takes out the most
claims. Ordering by that, and guaranteeing every claim at least one plausible match, puts more of
the references an attorney actually filed on the page.

**Measurement.** Offline against `adhoc-a2fec8ee8ba2` (713 references, 20 claims, 60-card window):

| arm | claims answered | attorney refs on page | head, claims per card |
|---|---|---|---|
| A. as shipped | 20/20 | 1/5 (Preta @22) | 5,6,7,8,7,6,5,7,5,6 |
| B. + limitation guarantee (today) | 20/20 | 1/5 (Preta @22) | unchanged |
| C. claim-first, plausible count | 20/20 | 1/5 (Preta @40) | 8,8,8,7,7,7,7,7,7,7 |
| C'. claim-first, DISCLOSED first | 20/20 | 1/5 (Preta @50) | sorted on strong count |

**Verdict. ACCEPTED for the ordering it was asked to deliver; REJECTED as a fix for the attorney
metric.**

- Every claim already had a plausible answer on the page — **0 promotions were needed**. The
  guarantee is a safety net that has not yet had to fire on this subject, which is worth knowing.
- The ordering does exactly what it says: the head becomes monotonically strongest-first.
- It does **not** move the attorney metric, and costs Preta 18-28 places. The other four references
  sit at ranks 81, 132, 288 and unread — **outside the 60-card window entirely** — and reordering
  the window cannot reach what is not in it.

**What this localises.** The binding constraint is no longer retrieval (5/5 found), ranking
(reordering cannot help), or the page (every claim answered). It is that our reader credits
GRABO's patent with 4-7 claims where the examiner credits 13. R4 shows a stronger model buys one
of those, not six. So the gap is the QUESTION, not the model — see R6.

---

## R6 — Ask fewer requirements per call

**Hypothesis.** The reader economises when asked many things at once. This repo already recorded it
on the feature side — "the same reference grounded 10 of 12 asked alone and 2 of 12 asked together"
— and kept `CLAIM_BATCH=12` anyway, because eleven sequential calls per reference was already the
wall-clock ceiling. R2 removed that ceiling, so the trade can be re-opened.

**Measurement.** Swept over the four references the attorney filed that this run charted, same
model (flash), same prompt, same document text. Ground truth for GRABO: the examiner applied it to
thirteen claims.

| batch | claim calls | disclosed cells | quoted cells | GRABO claims disclosed |
|---|---|---|---|---|
| 12 (was) | 6 | 58 | 94 | 8 |
| 8 | 9 | 67 | 101 | 7 |
| **4 (now)** | 17 | 66 | **117** | 7 |
| 2 | 34 | **88** | **125** | **9** |

Per-reference wall clock barely moves (8.5s → 7.9s at four) because the batches are concurrent.
What grows is the global call budget: 11 calls per reference becomes 21 at four and 38 at two,
which over ~660 references is roughly 20, 38 and 68 minutes at the measured 6.04 calls/s.

**Verdict. ACCEPTED at 4.** +52% disclosed and +33% quoted are available at 2, and cost the whole
speed win. Four takes 94% of the quote gain for half the price and still leaves the search
substantially faster than the 77-minute baseline. `DEEP_CLAIM_BATCH=2` when evidence matters more
than the hour.

**Not changed, deliberately:** `FEATURE_BATCH` is still 24 and the same argument probably applies.
One change at a time, or the next measurement cannot be attributed.

---

## R7 — Screen with several models at once and take the best score

**Hypothesis.** The screen decides what is read, and what is not read cannot become evidence. On
this docket Meyer was retrieved and never scored at all; on the Schmalz docket Quackenbush — the
attorney's most comprehensive match — was screened 70 and not read. Different model families
disagree, so scoring with three and taking the max should get more of the right documents read.

**Measurement.** The five attorney references plus twenty real neighbours from the same run, in one
batch (the screener calibrates within a batch, so scoring five alone would flatter them all).

```
attorney references clearing the read threshold (70):
   3/5 as the run scored them        4/5 on the max across three models
```

Which looks like a win. **The control says otherwise.** On each model's own scale:

```
model A   attorney median 60.0   padding median 77.5   separation -17.5   12/20 padding over 70
model B   attorney median 60.0   padding median 77.5   separation -17.5   13/20 padding over 70
model C   attorney median 75.0   padding median 83.5   separation  -8.5   17/20 padding over 70
```

**Verdict. REJECTED.** Every model ranks the attorney's references BELOW the documents our own
search surfaced. Taking a max over three samples raises everything — 17 of 20 padding candidates
clear the threshold too — so the 3/5 → 4/5 is the arithmetic of a maximum, not better
discrimination. Shipping it would have read more documents, not better ones.

**Caveat on this experiment.** A latched-off provider falls back to a healthy one, so the per-model
columns are not reliably attributable when that happens. The finding does not depend on which
column is which: all three separations are negative.

**What it does establish.** Screening is not the bottleneck on this subject either — 4 of 5 were
read. Retrieval found 5/5, the screen read 4/5, the page shows 1/5. Every stage before the page is
doing its job.

---

## R8 — End to end, everything on, against the attorney's own subject

`adhoc-c0182f3d1d57` vs the `adhoc-a2fec8ee8ba2` baseline. Same subject (US 2025/0033224 A1), same
gold set, everything from R2-R6 live.

| | baseline | now |
|---|---|---|
| wall clock | 2h32m | 2h35m |
| read stage | 700 references in 7,805s | 685 references in 8,153s |
| claim calls per reference | 6 | **15** (57 limitations at batch 4) |
| limitations covered | 42 of 68 — **62%** | 45 of 57 — **79%** |
| limitations with only partial evidence | 26 | **12** |
| claims ANTICIPATED (one document teaches all of it) | 4 | **6** |
| rescue re-read of already-read references | 13 cells across 11 refs | **76 cells across 59 refs** |
| claims with an answer on the page | not measured | **20/20**, 11 of them a full disclosure |
| attorney references screened | 4/5 | **5/5** |
| attorney references read in full | 4/5 | 4/5 |
| attorney references on the page | 1/5 — Preta @32 | 1/5 — **GRABO @33** |

**Verdict. The wall clock did not improve, and that is the honest result: the 2.1× from R2 was
SPENT on R6 rather than banked.** Throughput rose about 2.5× (2.5× the calls in the same time) and
every one of those extra calls went into asking fewer requirements per question. `DEEP_CLAIM_BATCH=12`
takes the speed instead and roughly halves the read stage; the two are a dial, not a pair of wins.

**The page metric is still 1/5 but it is now a different 1.** It is US 11,413,727 — the reference
the examiner used to reject thirteen claims under 102(a)(2), and the one the attorney led his
filing with — where before it was Preta, cited for a single claim. Getting the most important
reference visible is worth more than the ratio says.

**What is still not fixed.** Ristau at 103, Schmierer at 247, Meyer screened 70 and not read. Four
of the five are found, read and correctly charted, and the 60-card page cannot hold them because
~60 documents from our own search genuinely score higher on our own objective. Either the objective
is wrong for a submission, or the page is the wrong deliverable for a Type B search and the ledger
is — which is now at 79% covered with six anticipated claims, and is the thing an attorney would
actually file from.

---

## R9: The exact-phrase channel's cost is the PHRASE, not the number of phrases (2026-08-22)

**Hypothesis.** `channel_exact` is a cheap precision channel, so it can go into the deep presets.
The agent already generates the phrases (`plan()` asks for "exact multiword phrases to match" and
`search()` is given them) and no preset ran the channel, so they were computed and discarded.

**Measurement.** EP 3 707 092, novelty mode, four phrases a model produced for that subject, each
run alone against the live corpus on a cold cache:

```
'air extraction means'      0.33 s     12 families
'vacuum seal element'       2.80 s      4 families
'rigid base element'        3.27 s      5 families
'contact surface'          97.26 s    300 families
```

One generic two-word phrase is 94% of the channel's 103 s. The whole four-phrase channel measured
270 s on the first pass of a fresh process. A deep run makes about 39 passes.

**Verdict. The hypothesis is REJECTED as stated and the channel is fixed instead.** The cost is
the aggregation over the entire match set, which the 1,200-row limit then truncates, so a phrase
matching tens of thousands of chunks pays for a ranking that is thrown away.

`retrieval.exact.PHRASE_MAX_CHUNKS` (`EXACT_PHRASE_MAX_CHUNKS`, default 20,000, about 17x the
1,200 rows the channel can return) declines such a phrase after a bounded probe:

```
probe 'contact surface'      LIMIT  5,000    1.16 s   (fills it)
probe 'contact surface'      LIMIT 40,000    3.70 s   (fills it)
probe 'vacuum seal element'  LIMIT  5,000    0.30 s   -> 1,111 chunks, affordable
```

Same four phrases, same subject, two runs each:

| | wall clock | families |
|---|---|---|
| guard off | 16.08 s / 9.17 s | 321 |
| **guard on** | **3.17 s / 2.96 s** | 21 |

DECLINED, NOT TRUNCATED. Reading the first 20,000 matches and ranking those would look like a
result and be an arbitrary subset of one, which for a precision channel is the one failure mode it
must not have.

**The guard also turned out not to cost recall, and on one subject to buy it.** Frozen phrases,
same seeds, arm A without `exact` and arm B with it:

| ep3707092 | families | gold@100 | gold@500 | gold@2500 |
|---|---|---|---|---|
| A, no exact | 2,129 | 0.0 | 0.0 | 0.3077 |
| B, exact + guard | 2,154 | 0.0 | **0.0769** | 0.3077 |
| B, exact unguarded | 2,443 | 0.0 | 0.0 | 0.3077 |

The unguarded generic phrase contributes 314 families and displaces a gold family out of the top
500, and costs 10 s doing it. That is one family on one subject and inside the recorded +/-2
variance, so it is not the reason for the guard; it is a reason to stop worrying that the guard
throws recall away.

---

## R10: `exact` into the deep presets

**Change.** `agentic` and `claim_agentic` now name `exact`. With R9's guard the channel is bounded.

**Measurement.** Five benchmark subjects, phrases generated per subject, arm A without `exact` and
arm B with it, same query text and same seeds in one process:

| subject | gold in corpus | A@100 | B@100 | A@2500 | B@2500 |
|---|---|---|---|---|---|
| ep3707092 | 13/27 | 0.0 | 0.0 | 0.3077 | 0.3077 |
| b66c_ep1889537a3 | 10/10 | 0.4 | 0.4 | 0.5 | 0.5 |
| b25j_ep3546144a1 | 11/11 | 0.0 | 0.0 | 0.0 | 0.0 |
| b65g_ep3345847a1 | 11/11 | 0.5455 | **0.6364** | 0.7273 | 0.7273 |
| f16b_ep1989975a1 | 15/15 | 0.0 | 0.0 | 0.4 | 0.4 |

**Verdict. ACCEPTED, and stated honestly: this is not a proven recall win.** Nothing moved at
depth on any subject. One subject gained one gold family at rank 100, reproduced across two runs
with frozen phrases, and that is inside the +/-2 family variance this log already records. What is
established is the cost: about 3 s per pass warm on a four-phrase set, against a channel weighted
0.60 whose input the pipeline was already paying a model to produce and then throwing away.

`b23q_ep1651392a4` is not in this corpus at all (the A4 kind code), so it produced no arm. That is
a gap in the benchmark subject list, not a harness failure.

**Not changed, deliberately.** `bm25` stays out of the deep presets. `docs/lexical_interface.md`
records it returning ZERO gold families at any depth on both standing subjects while costing
20.45 s and 175.55 s a pass, and nothing here contradicts that. It becomes a question again when
workstream C's Tantivy index lands, not before.

---

## R11: The shard router's family dedup was inverted

**Found while writing the first test of `shard_router.route`.** `_rank_weighted` suppresses every
member of a family after the first, and `domains_of_publications` then defaulted a pid missing from
the weights map to a FULL vote of 1.0. So the five suppressed members of a six-member family each
voted 1.0 while the one that survived voted 1/41: the dedup did not fail, it inverted.

**Measurement.** Seven candidates, six of them one family classified in `B25J` and one a distinct
family in `B66C`:

```
before   B25J 0.986   B66C 0.004
after    B25J 0.534   B66C 0.466
```

**Verdict. FIXED.** A supplied weights map is now the electoral roll: a pid absent from it does not
vote. `tests/test_shard_router.py::test_one_family_votes_once` and
`::test_a_pid_absent_from_a_supplied_weights_map_does_not_vote`. Nothing in production consumed
`route()` yet, so this cost nobody a search; it would have cost workstream E a shard fleet woken by
whichever family happened to publish most.

---

## R12: The cold and global tiers cost nothing when their backends are absent

**Hypothesis.** Naming `cold` and `global` in a preset before workstream E lands is free, so they
can be wired into the default presets now rather than in a later change nobody measures.

**Measurement.** `b65g_ep3345847a1`, three alternating runs of each arm in one process, arm A
`dense,brief_dense,cpc,citation,qbe` and arm B the same plus `cold,global`:

```
A   7.21 s   6.62 s   7.44 s      mean 7.09 s
B   6.60 s   6.68 s   6.58 s      mean 6.62 s
```

Identical per-channel hit counts (`dense` 1000, `brief_dense` 1000, `cpc` 1000, `citation` 546,
`qbe` 139), identical 2,994 families, identical recall at 100, 500 and 2,500.

**Verdict. ACCEPTED.** The tiers short-circuit on `shard_manager.available()` and
`global_search.available()` before any routing query is issued, which matters because
`shard_router.historical_prior` is a 7.9 s `GROUP BY` over 53,473,700 classification rows. The
guarantee is also a test rather than a hope:
`tests/test_retrieval_cold.py::test_no_shard_backend_issues_no_query_and_creates_no_channel`
fails if the tier so much as opens a connection.
