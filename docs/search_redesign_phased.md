# The phased search redesign

2026-08-23. Written against measured numbers from this repo and from the live boxes, not from
intent. Read `condensed.md` sections 4, 8, 13 and 15 and `TECHNICAL_REPORT_2026-08-06.md` first;
this document changes the shape of the pipeline they describe, and every constant it moves was
moved for a reason recorded there.

---

## 1. The question this answers

A semantic engine like PQAI returns in milliseconds. We take between 50 minutes and two and a half
hours for a claim attack. The instinct that says "it is one embedding and one ANN query, where is
the time going" is correct about the retrieval and wrong about what the product currently does.

Two separate facts, both measured:

**The vector search is not the two hours.** On a claims run the full-document reading stage measured
1,375 to 5,067 seconds and the claim rescue another 495 to 4,217 seconds. Reading is 97% of the
model bill: 72.8M of 112M prompt tokens on the run we costed. Local retrieval on a concept run was
678 seconds, the external fan-out 65 to 101 seconds concurrent with it, and screening 2,480
candidates took 33 seconds. So retrieval and screening together are minutes inside a two hour run.

**The vector search is nevertheless far slower than it should be, for one fixable reason.** The
production HNSW index was 74 GB against 62 GB of RAM on `patents-pilot-db`. Every probe is a random
disk read, which is why embedding throughput collapsed from 199 to 49 chunks per second when the
index outgrew memory, and why `hnsw.iterative_scan` with a 12,000 tuple scan budget is doing work
that a resident index would not need. An index that fits in RAM answers in single-digit
milliseconds. Ours does not fit.

So the redesign has two independent jobs: make retrieval resident and parallel so it is measured in
seconds, and stop using an LLM as the primary ranking engine.

---

## 2. The principle

> The vector database does the relevance and the element mapping. The language model verifies a
> small set of already-grounded passages. It is never the search engine.

Everything below follows from that sentence, plus one product rule:

> Nothing expensive runs unless a human asked for it by name.

---

## 3. The phase model

Today one `/run` buys everything, and a user who wanted a list of references pays for a claim
ledger, a grid, a rescue loop and a portfolio construction. That is now five separately purchased
phases. Each is a durable run row with its own lease, checkpoint, cost record and artifacts, and
each is resumable and cancellable on its own.

| phase | what it is | who starts it | target wall clock |
|---|---|---|---|
| 0 intake | parse the input patent or description, split claims into limitations, build the query set | automatic | 5 to 20 s |
| 1 find | retrieval plus the candidate passage matrix. No legal disclosure assertions | automatic | p50 20 s, p95 45 s |
| 2 ledger | passage verification into a real disclosure ledger | button: "Build claim ledger" | 1 to 3 min |
| 3 grid | deep full-document analysis for the strongest references | button: "Build claim x prior-art grid" | 2 to 5 min |
| 4 rescue | targeted recall rescue for limitations nothing covered | button, or automatic tail of phase 3 | 30 to 120 s |
| 5 submission | the legal and export package: 37 CFR 1.290 papers, IDS listing, exports | button: "Prepare third-party preissuance" | under 60 s, no new retrieval |

The line the phase names exist to hold: **semantic similarity is candidate evidence, a verified disclosure requires reading.** Phase 1 may never call anything disclosed.

A user who wanted "what is out there" stops after phase 1 and has paid seconds of compute. A user
attacking a patent pays for phases 2 and 3 deliberately, sees the estimate before clicking, and gets
a total claim attack in 4 to 10 minutes instead of 50 to 150.

Phase artifacts are the next phase's input, so nothing is recomputed:
`phase1.json` (hits, families, element matrix) to `phase2.json` (verified passage cells) to
`phase3.json` (full-read charts) to `phase5` (papers).

---

## 4. Phase 0: intake and the query set

Unchanged in substance, tightened in shape. `src/patent_pdf.py` and `src/patent_doc.py` already
produce claims, abstract and description; `limitations.split_claims` already turns 20 claims into
about 68 limitations; `query_set.py` already produces an essence sentence and alternative phrasings.

What changes:

* The query set is built **once**, as a flat list of typed queries, and is the only thing phases 1
  and 4 search with. Types: `claim_whole` (independent claims), `claim_resolved` (dependent claims
  with their ancestry inlined), `limitation` (one requirement), `paraphrase` (a register variant of a
  rare limitation), `concept` (for a description-only input).
* Figure narration never reaches an embedding. That was measured to be 1,700 of one query's 4,488
  characters and it is already stripped by `query_set.retrieval_text`; the rule is now enforced at
  construction.
* Typical volume for a 20 claim patent: 3 to 5 `claim_whole`, 15 to 20 `claim_resolved`, 60 to 70
  `limitation`, 10 to 20 `paraphrase`, so **90 to 120 queries**.

---

## 5. Phase 1: find, in seconds

### 5.1 Embed everything in one call

All 90 to 120 query texts go to Vertex in a single batched embedding request, not one call per
query. Measured embedding throughput is 199 to 376 chunks per second in the factory, so this is
sub-second work, and the in-process `_QCACHE` becomes a persistent cache keyed on
`sha1(text | model | task_type | dimension)` so a re-run or a sibling phase pays nothing.

### 5.2 Make the index resident, then parallel

This is the whole latency fix and it is an infrastructure change, not an algorithm change.

* **Split by chunk kind.** Claims are the highest value and the smallest set: in `niche_full_v1`
  today, `claim_own` plus `claim_resolved` is 4.17M of 13.0M chunks, against 7.22M description
  paragraphs. A claims plus abstract index is small enough to be resident on one machine. Build it
  as its own index and query it first.
* **Keep fp32 unless residency actually requires fp16.** The 768 / 1024 / 3072 result (all scored
  0.8251) says DIMENSION is saturated for us. It says nothing about quantization, and our own
  halfvec probe measured top-100 overlap as low as 83% on one query, so fp16 is a recall change
  until proven otherwise. At 4.17M claim and abstract chunks, fp32 at 768 dimensions is a few GB
  of vectors plus its graph, which fits a dedicated box with room to spare. Reach for `halfvec`
  only when a partition genuinely will not stay resident, and re-measure top-100 overlap on the
  gold queries before it becomes the default.
* **Shard description paragraphs across the shard VMs.** `src/retrieval/shard_router.py`,
  `shard_manager.py`, `cold.py` and `global_search.py` are already deployed and inert because no
  backend is registered. Register the shard fleet as that backend. The eight `c4-highmem-16` shard
  VMs exist and are TERMINATED; each holds one partition sized to its RAM.
* **Bound the probe instead of scanning.** Phase 1 runs with `hnsw.ef_search` 100 to 200 and
  iterative scan off, so latency is bounded and predictable. Phase 4 rescue may raise it, because
  there the query count is tiny.
* **Fan out concurrently, bounded by what the box actually has.** One connection per query through
  pgbouncer, with the pool sized from the resident node's connection and CPU budget rather than
  from a number in a design document. 120 queries times 4 channels is about 480 probes, and the
  point is that a resident index turns the whole retrieval stage into tens of seconds instead of
  ten minutes; the exact concurrency is a tuning result, measured per node, not a constant. Start
  conservative, raise it while p95 stays flat, stop when it does not.

Channels in phase 1, all peers, all cheap: `claim_dense`, `paragraph_dense`, `bm25` (Tantivy, being
built now on `niche_full_v1`), `exact` phrase for terms of art.

**CPC and citation expansion stay in phase 1, and they do not block it.** They have demonstrated
independent recall value, and a phase that drops them is a phase that loses art dense and lexical
both miss. Run them off the first dense results, concurrently, and fold whatever has landed when
the page is assembled: the phase 1 deadline belongs to dense plus BM25, and a slow graph expansion
degrades to fewer candidates rather than to a slower page. QBE and biblio move to phase 4, where
recovering what the head missed is the actual job.

### 5.3 Aggregate without a model

Every hit carries `(query_id, chunk_id, publication_id, family_id, chunk_kind, score, coordinate)`.
From that, with no model call:

* **element by family matrix**: for each limitation, the best passage per family, with its score and
  its coordinate.
* **family score**: rarity-weighted coverage of the limitation set, where rarity is idf over the
  candidate pool, plus a small prior for text depth and for the family holding the matched
  publication. This is `deep_rank.rarity` and `coverage_rank` logic applied to retrieval scores
  rather than to LLM verdicts.
* **the funnel**: keep the top ~150 families for phase 2 and the top ~40 for phase 3, and log every
  truncation. A cap that binds silently has cost this project three separate measurement errors.

The page renders straight off this: 60 cards, each with a per-claim coverage bar, the matched
passage for each element, and the coordinate.

### 5.4 The honesty rule for phase 1

A cosine is not a disclosure. Phase 1 output is labelled **candidate passage**, never "disclosed",
and the verdict colours stay grey until phase 2 verifies them. `src/disclosure.py` already carries
this vocabulary and the existing lesson is explicit: the element grid rendered retrieval cosines
next to a reading that disagreed with it, and the reading was right.

### 5.5 Optional cheap rerank, still inside phase 1

Rerank the top 300 `(limitation, passage)` pairs only. Either the CPU cross-encoder in its existing
dedicated child process, or a Flash-Lite listwise pass over passages. Budget 10 to 30 seconds, and
it is a toggle, because phase 1 must stay interactive.

---

## 6. Phase 2: the claim ledger, from passages not documents

Input: the top ~150 families and, per family, the best 1 to 3 passages per limitation that phase 1
already retrieved. Not the 50 page document.

One model call per family, on the `read` tier (flash), asking a single bounded question: which of
these limitations are actually supported by these passages, quote the words that support each, and
say absent where nothing here teaches it. Grounding, location and refutation gates unchanged
(`grounding.grounded`, `claim_chart._locate`, `deep_analysis._refute`), because they are what keeps
the output filable.

150 calls at 6 to 8 calls per second per provider is about 20 to 30 seconds of model time. Budget 1
to 3 minutes with retries.

Output: `report["ledger"]`, the claim and limitation coverage table, every cell carrying a verdict,
a verbatim quote and a coordinate, plus the list of limitations nothing covers. That list is what
phase 4 searches for.

**The cap lesson applies here.** The ledger previously held 10,880 verified cells and kept 544, five
per cent, cutting by verdict strength, so a `partial` from the reference an attorney would file lost
its slot to eight confident rows from documents nobody would file. Keep 40 per limitation, record
`n_evidence`, and never let the display cut decide the ranking.

---

## 7. Phase 3: the grid, on 20 to 40 references

Only now does anything read a whole patent, and only the references phase 2 says matter.

* **Small limitation groups per reference, run concurrently. NOT one giant call.** Our own sweep
  says a bigger batch loses disclosures: over the four references an attorney filed, batch 12
  produced 58 disclosed cells, batch 4 produced 66, batch 2 produced 88. Ask each reference about
  2 to 4 related limitations at a time, in parallel, and cache the document context so the full
  text is not retransmitted per group. The order of magnitude comes from the READ SET (20 to 40
  references instead of 150 to 250) and from concurrency, which bought 2.1x on its own, never from
  cramming a reference's whole checklist into one request.
* **Full text comes from the niche corpus, never from a paid provider at query time.** Today the
  pipeline buys full text for up to 400 references mid-search because the corpus is thin. That is
  what the factory exists to end. A missing document becomes a factory job and the search proceeds
  without it, naming the gap.
* Refutation, rarity, `coverage_rank.rank`, `guarantee` and `claim_first` are unchanged. They were
  each measured into their current shape and none of them is slow.

Target 30 to 120 seconds of model time, 2 to 5 minutes wall clock with the refutation pass.

---

## 8. Phase 4: rescue only what is missing

Do not re-run the claim search. For each uncovered limitation, and for the pairs of limitations that
matter to a section 103 argument, issue a handful of targeted queries with the recovery channels
switched on (citation, CPC, QBE, cross-lingual, and the external fan-out if the user enabled it).
Retrieve ~100 families, verify ~10 to 20 at passage level, read ~5 in full.

The measured shape says this is where the reach problem lives, not the reading: 44.4% of lost gold
never came back from retrieval at all, and per-limitation querying moved one reference from rank
8,810 under an invention-wide query to 135 under the query for the requirement it was actually cited
for.

---

## 9. Phase 5: the deliverable

`concise_description.py`, `concise_render.py`, `export_ids.py`, `deliverables.py` and the exports are
unchanged and already cheap, because the substance is already in the phase 3 artefacts. The only
change is that they are a phase the user starts, with its own row and its own record of what it was
built from.

---

## 10. What each change is worth

| stage | today, measured | after | why |
|---|---|---|---|
| local retrieval | 678 to 932 s | 5 to 30 s | resident indexes, sharded paragraphs, one batched embed, bounded concurrency |
| external fan-out | 65 to 101 s, concurrent | unchanged in phase 1, off by default | it is free wall clock but it is not the constraint: control run was 1/22 with it off against 0/22 with it on |
| pre-search paid text | up to 400 fetches | 0 | acquisition is the factory's job and runs offline |
| screen | 2,500 candidates, 33 s | deleted | the screen exists because retrieval order was untrustworthy; vector aggregation replaces it |
| reading | 1,375 to 5,067 s, 150 to 250 refs | 30 to 120 s, 20 to 40 refs | passages first, one call per reference, verified set only |
| rescue | 495 to 4,217 s | 30 to 120 s | only uncovered limitations |
| **total claim attack** | **3,097 to 7,243 s** | **4 to 10 min** | |

Cost follows the same curve: the 114.6M token run we costed at about $78 becomes a few dollars,
because 97% of that bill was reading documents that phase 2 would have eliminated on their
passages.

---

## 11. Machine layout

Data plane, running today, unchanged:

| VM | role |
|---|---|
| `patents-niche-discovery` | manifest discovery, queue seeding, completeness report |
| `patents-niche-fetch-1`, `-2` | full-text acquisition, provider waterfall, shard 0 and 1 of 2 |
| `patents-niche-embed` | parse and chunk pool, Gemini Batch embedding controller |
| `patents-niche-build` | `niche_full_v1` Postgres, vector publisher, Tantivy builder |

Serving plane:

| VM | role |
|---|---|
| app | `nimo.iptorch.com`, gunicorn, routes and rendering only, no search work in process |
| claims index | the resident claims plus abstract HNSW for the whole niche, fp32 by default, plus Tantivy |
| shard 1..8 | description paragraph partitions, `c4-highmem-16`, registered through `shard_router` |
| phase-1 workers | many, short, cheap, one lane |
| phase-2/3 workers | few, long, LLM heavy, their own lane and their own concurrency cap |

Queue: the existing `search_runs` durable queue with `FOR UPDATE SKIP LOCKED`, lease, heartbeat,
per-phase checkpoint and exactly-once settle, which `src/runner/worker.py` already implements and
which was proven when a live run survived a restart of `patent-results` with `attempts=1`.

---

## 12. Migration, in order, each step shipping something

1. **Split the phases behind a flag on the current retrieval.** No new infrastructure. The user gets
   results and a set of buttons; the heavy stages stop running unasked. This alone converts most
   searches from two hours to minutes because most searches do not need phase 3.
2. **Build the claims plus abstract index on `niche_full_v1` and point phase 1 at it**,
   keeping the current path as a fallback. Measure on the two attorney gold sets and the two-subject
   benchmark before switching the default.
3. **Delete the 2,500 candidate screen for claim attacks**, replacing it with the phase 1
   aggregation. Re-measure; the screen was worth its cost only while retrieval order was untrusted.
4. **Cut the read set to 20 to 40 with one call per reference.** Re-measure.
5. **Wake the shard fleet** and register it, moving description paragraph retrieval off the single
   box.
6. **Turn off live paid text acquisition** in the search path and route misses to the factory queue.

---

## 13. Guardrails, all of them paid for once already

* Widening a funnel while the page stays a fixed size lowers visible recall. Measured three times.
  Move the read set and the page together or not at all.
* A cap that binds is a silent truncation, and two caps on one list means the tighter one wins
  silently. Log what was dropped, always.
* Never judge a change on one query. Run `eval/benchmark.py` over both subjects and both attorney
  gold sets, and stamp every result with the git sha and the corpus size. Run to run variance is
  plus or minus two families.
* Do not put the reader on the fast tier. Asked the same 68 limitations, flash grounded 20
  disclosures, sonnet 21, haiku 9. Fewer cells looks exactly like a document that discloses less.
* A ceiling is a magnet: any score cap creates a plateau that evicts genuinely read documents.
* Retrieval-layer precision fixes regress recall. Filter at the display layer.

---

## 14. Targets to hold ourselves to

| | target |
|---|---|
| phase 1 p50 | 20 s |
| phase 1 p95 | 45 s |
| phase 2 | 1 to 3 min |
| phase 3 | 2 to 5 min |
| full claim attack, all phases | 4 to 10 min |
| model spend per full attack | single dollars |
| attorney gold, references on the page | not worse than today at every step, measured before each default flips |
