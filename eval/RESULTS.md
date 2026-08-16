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
