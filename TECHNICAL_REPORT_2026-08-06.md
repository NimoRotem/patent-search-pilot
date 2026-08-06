# Prior art search pilot: technical report

2026-08-06. Written for a technical reviewer who will audit the system design and the experimental
record. Assumes familiarity with dense retrieval, rank fusion and patent search. Every number here
is measured; where a number is an estimate or comes from a single run, it says so.

Repository: `~/patent-search-pilot`, branch `pilot-build`. 93 modules and 34,070 lines under
`src/`, 18 evaluation harnesses under `eval/`, 55 test files, 822 tests passing.

---

## 1. Product objective, and why it constrains the design

The system does not rank documents by similarity to a subject patent. It assembles a **portfolio**
of references that collectively address the subject's disclosures, because the artefact being
produced is an argument about novelty: which disclosures were already known, and where.

The consequence is that relevance is **not independent across results**. If a subject has ten
disclosures and forty nine selected references all address the same nine, the fiftieth reference is
more valuable if it addresses the tenth alone, even if it is less similar to the subject than any
of the other forty nine. This makes the selection problem submodular rather than pointwise, and it
is the reason the final stage is a maximum coverage construction and not a sort.

Domain scope: vacuum handling and adjacent mechanical fields, including vacuum cleaning, power
tools, machinery and material handling. Explicitly excluded: software, biology, chemistry.

**Primary metric.** Weighted disclosure coverage at 50, computed against a disclosure list and a
weight vector fixed before the run.

---

## 2. System architecture

### 2.1 Data layer

PostgreSQL 17 with pgvector on a single 4 vCPU / 16 GB VM, shared with roughly a dozen other
services.

| table | rows |
|---|---|
| `publications` | 4,975,809 |
| `chunks` (embedded) | 26,674,518 |
| `claims` | 8,538,133 |

Database size 291 GB. 32 tables; the retrieval-relevant ones are `publications`, `chunks`,
`claims`, `classifications`, `citations`, `family_of`, `paragraphs`, `figures`, `figure_images`.

**Tiering** on `publications.tier`:

| tier | rows | meaning |
|---|---|---|
| `core` | 2,426,311 | the seed CPC branches of vacuum handling |
| `expanded` | 2,528,051 | breadth added by classification and citation expansion |
| `external` | 13,689 | acquired from API results during searches |

The `external` tier is the mechanism by which the corpus grows from its own searching: candidates
returned by the external fan-out are materialised as rows, chunked and embedded, so a later search
can retrieve them locally. **13,689 rows against 4.98M is the number to hold on to for section 6.**

**Vector index.** HNSW over `chunks.embedding`, 768 dimensions, Vertex AI `gemini-embedding-001`.
Query-time parameters: `hnsw.ef_search` 200 for ordinary channels and 400 for the seed channel,
`hnsw.iterative_scan = relaxed_order`, `max_scan_tuples` 12,000 ordinary and 60,000 for the seed.
The iterative scan setting matters more than it looks: with it off, a diagnostic probe reported 780
retrievable chunks where the true figure was about 9,000, and that probe was believed for a while.

**Indexes of note.** `ix_chunks_hnsw` (vector), `ix_chunks_tsv` (lexical), `ix_pub_simple_family`,
`ix_pub_ext_family`, `ix_pub_date`, `ix_pub_prio`, `ix_class_symbol`, `ix_cit_src` / `ix_cit_dst`,
and `ix_pub_number_norm`, a functional index on
`upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g'))`. That last one was missing until
recently and the normalised publication lookup, which runs on every search, was a 6.1 second
sequential scan over 4.98M rows. With the index it is 0.002 seconds.

**Identifier normalisation** (`src/pubnorm.py`) handles the usual patent identifier problems:
country prefixes, kind codes, and the leading zero ambiguity in US pre-grant numbers. It emits a
ladder of every zero-strip level (`0008929` produces `008929`, `08929`, `8929`) because different
sources pad differently and a single canonical form loses matches.

### 2.2 Pipeline

One subject takes about 21 minutes on an unloaded box.

#### Stage 1: decomposition (`src/external.py:plan`)

An LLM (`gemini-2.5-flash`) reads the subject and emits up to `MAX_ASPECTS = 9` aspects. For each
aspect it produces title-shaped keyword queries and up to `CPC_PER_ASPECT = 4` CPC codes. The
prompt is deliberately **product neutral**: it is instructed to describe mechanisms rather than the
product name, because searching for "vacuum lifter" retrieves the same commercial cluster
repeatedly and misses the mechanism expressed in other words.

#### Stage 2a: local retrieval (`src/retrieval.py`)

Multiple channels run in parallel, each producing a ranked publication list capped at
`PUB_CAP = 1000` (`SEED_PUB_CAP = 2500` for the seed channel), aggregated from `CHUNK_FETCH = 4000`
chunk hits (`SEED_CHUNK_FETCH = 9000`).

Channels and their fusion weights:

| channel | weight | what it is |
|---|---|---|
| `dense` | 1.00 | semantic over all chunk kinds |
| `claim_dense` | 1.00 | semantic restricted to claim chunks |
| `federated` | 0.90 | results streamed from the sibling engine, already fused and scored upstream |
| `brief_dense` | 0.80 | semantic over abstract and whole-document chunks only |
| `crosslingual` | 0.70 | dense over a translated query |
| `exact` | 0.60 | ordered phrase match |
| `citation` | 0.55 | family and citation graph neighbours of a strong hit |
| `qbe` | 0.50 | query by example, dense from a strong hit |
| `biblio` | 0.30 | assignee and inventor prior |

The weights exist because unweighted RRF let broad noisy channels out-vote a strong rank-1 dense
hit: CPC ranks a thousand documents by classification match count and BM25 by lexeme count, so a
mediocre document appearing in several weak channels beat an excellent document appearing once in
the strongest. A `DENSE_FLOOR = 30` guarantees the top 30 dense hits a floor contribution so weak
channels cannot bury them entirely.

`brief_dense` deserves a note: it is the pool in which a document whose entire indexed text is an
abstract can compete at all. Its existence is an early, partial acknowledgement of the problem
section 6 identifies.

#### Stage 2b: external fan-out (`src/external.py`)

Runs concurrently with local retrieval. Up to `MAX_QUERIES = 78` queries across seven sources:

| source | fusion weight | notes |
|---|---|---|
| PQAI | 1.0 | semantic over the whole disclosure |
| SerpApi Google Patents | 1.0 | Google's own relevance ranking, `SERPAPI_ASPECTS = 4` |
| USPTO ODP | 0.7 | US titles only, ordered by ODP relevance |
| BigQuery Google Patents mirror | 0.5 | `nimo-gpt.patents_cache.pubs`, 32.4M publications, 1976 to 2026, US/EP/WO. Title substring match with word boundaries, ranked by weighted term hits |
| EPO OPS | | bibliographic and family |
| OpenAlex | | |
| IP Australia | | |

Lens.org is configured but its token is expired, so it returns 401 and is recorded as a source
failure rather than silently contributing nothing.

The fan-out yields roughly 15,000 candidates per subject against about 40 before it existed.

**Channel-per-query, not channel-per-source.** `channels()` creates one RRF channel for each query
rather than pooling a source's hits. RRF consumes rank order, so pooling buries each aspect's best
find under the whole fan-out. A channel per query is what keeps a document that only one aspect
found competitive, which is the point of asking nine separate questions. `CHANNEL_DEPTH = 100`.

**External fusion** (`fuse_families`): per-source best rank, `RRF_K = 40`, multiplied by a breadth
bonus `1 + BREADTH_BONUS * (aspects_hitting / n_queries)` with `BREADTH_BONUS = 0.30`. Then
`rescore()` re-embeds the top `RESCORE_TOP = 1500` and blends semantic similarity at
`SEMANTIC_SHARE = 0.6`. Survivors are materialised into the `external` tier with a SAVEPOINT per
row so one bad record cannot abort the batch.

#### Stage 3: fusion and deduplication

Weighted RRF with `RRF_K = 40` (chosen small: it sharpens the rank-1 advantage of a strong
channel). Family deduplication on the DOCDB `simple_family_id`, falling back to the publication
number when a family id is absent. Date filtering implements EPC Article 54(2) and 54(3), so art
published after the priority date is marked ineligible rather than dropped silently.

Typical output: 8,000 to 9,500 ranked families.

#### Stage 4: screening (`src/deep_rank.py`)

`SCREEN_TOP = 2500` candidates, `SCREEN_CHARS = 1600` of text each, `SCREEN_BATCH = 25` per LLM
call, `SCREEN_WORKERS = 6`. Produces a 0 to 100 score per publication.

#### Stage 5: reading and charting

`CHART_TOP = 300` (max `CHART_TOP_MAX = 350`), gated by `CHART_MIN_SCREEN = 75`, with
`CHART_WORKERS = 18`. Two escape hatches exist so screening cannot be a single point of failure:
`ALWAYS_CHART_RETRIEVAL_HEAD = 60` charts the retrieval head regardless of screen score, and
`BLIND_RESCUE` charts up to 60 documents from the top 400 that the screen never saw.

Charting grounds each disclosure against specific passages, returning `disclosed`, `partial` or
`absent` with quoted evidence. `DISCLOSURE_CAP = 40` disclosures are charted per document.

A cross-encoder reranker (`bge-reranker-v2-m3`, CPU, `RERANK_TOP = 50`) runs in a dedicated child
process, and a listwise LLM reranker (`src/rerank_listwise.py`) reorders the head.

#### Stage 6: scoring and portfolio construction

Per-document evidence score (`src/deep_rank.py`, around line 595):

```
rest   = 1 - COVERAGE_WEIGHT                       # COVERAGE_WEIGHT = 0.55
w_ovr  = rest * OVERALL_SHARE                      # OVERALL_SHARE   = 0.60
w_scr  = rest * (1 - OVERALL_SHARE)
raw    = COVERAGE_WEIGHT*coverage + w_ovr*overall + w_scr*screen
depth  = clamp(chars_read / DEPTH_FULL_CHARS, 0, 1)   # DEPTH_FULL_CHARS = 60000
score  = raw * (DEPTH_CONFIDENCE_FLOOR + (1 - DEPTH_CONFIDENCE_FLOOR) * depth)
```

`DEPTH_CONFIDENCE_FLOOR = 0.75` is a **confidence multiplier, not a penalty**. It expresses that a
score computed from 2,000 characters is less trustworthy than the same score from 60,000, without
asserting that the short document is irrelevant. Swept at 0.30 / 0.45 / 0.60 / 0.75 against the sum
of cited references' ranks: 0.75 scored 576 against 836 for the previous depth-reweighting scheme.
It is also the gentlest of the four, which matters precisely because so much of the art examiners
cite is abstract-only here.

A document that could not be read in full is ranked **in its own tier**, below every fully read
reference, and displayed in a labelled section. This started life as a score cap, and the cap was a
bug that only a live run exposed: **a ceiling is a magnet.** Every abstract-only record whose screen
score cleared the cap scored exactly the cap, forming a plateau that sat above the natural range of
genuinely read documents and evicted them. On one real run, 44 of 50 displayed cards were
abstract-only records sitting on that plateau.

Final ordering is greedy maximum coverage (`src/coverage_rank.py`):

```
gain(doc | chosen) = SUM over disclosures d of  idf(d) * max(0, q(doc,d) - best(d))
val                = gain / total_mass + SCORE_WEIGHT * (score(doc) / hi)
```

with `CORROBORATION = 0.10` (a second reference proving an already-covered disclosure is worth
something, but only a tenth) and `SCORE_WEIGHT = 0.15`. Both defaults were originally 0.25 and
0.35, and at those values twenty duplicate references outbid one unique one, which reintroduced
exactly the defect the module exists to prevent. A unit test now pins this.

### 2.3 Serving

Flask (`src/webapp.py`, 4,434 lines) under supervisor. The report page renders from a `.view.json`
cache built lazily on first render, not written by generation. That distinction caused a real
measurement bug, see section 8.

---

## 3. Evaluation apparatus

More engineering went into this than into retrieval, and on the evidence that was the right call.

### 3.1 Benchmark construction

**The first benchmark was invalid and had to be discarded.** It was built from subjects already in
the corpus, so 297 of its 306 citations were already held: reach was 100% by construction and the
benchmark was structurally incapable of measuring the thing that turned out to matter most. This is
worth dwelling on, because the benchmark looked reasonable and produced plausible numbers.

The current benchmark is sourced from BigQuery instead, and stratified by how much of each
subject's cited art the corpus holds.

| | count |
|---|---|
| subjects | 50 (30 dev, 20 holdout) |
| strata | `mostly_in` 20, `mixed` 13, `mostly_out` 11, pinned 6 |
| gold citation rows | 513 |
| eligible after date and family filtering | 502 |
| eligible and **not** in corpus | 142 (28%) |

The six "pinned" subjects are the original hand-chosen ones, retained for continuity.

### 3.2 Gold standard

Examiner citations from `patents-public-data.patents.publications`. `citation.type` carries the
X/Y/A/I relevance code and `citation.category` the supplier (SEA, APP, EXA). We keep X, Y and A,
deduplicate to DOCDB families, and mark ineligible anything not citable at the priority date. Every
exclusion is recorded with its reason so the denominator is inspectable rather than asserted.

### 3.3 Frozen disclosure denominator

Disclosures are extracted once per subject and written to `eval/disclosures_frozen/` with a content
hash, a list version, and now the extraction budget used. Four kinds with fixed evaluation weights:

| kind | weight |
|---|---|
| `independent_limitation` | 1.00 |
| `combination` (a whole independent claim) | 0.90 |
| `dependent_limitation` (only what it adds) | 0.70 |
| `potential_claim` (supported by the description, not covered by the claims) | 0.55 |

Two separate reasons this had to be frozen. First, the weights were previously derived from the
candidate set via `deep_rank.rarity()`, which computes `log(N/df)` over the references the search
happened to chart. **Change retrieval and the weights change**, so a retrieval improvement that
finds more art covering a disclosure makes that disclosure cheaper and coverage can fall while the
product improves. Second, the list itself was generated during the run, so two runs of one subject
were scored against two different denominators.

Ranking may still use candidate-derived rarity, which is a genuine signal. What it may not do is
decide the score card. The two are now separate quantities.

A structural validator rejects a list that is empty, has no independent-claim limitations when
claims were supplied, has no combination, or hits the cap (which would make it a prefix). In
benchmark mode extraction is `strict` and raises rather than returning a defective list.

### 3.4 Trustworthiness machinery

- **Manifests** (`src/manifest.py`): an immutable record per run, including corpus snapshot
  counts and a completion status. Snapshot counts use `reltuples` estimates cached for 900 seconds,
  because the first implementation ran `count(*)` over 26.7M rows on every search request. A test
  now asserts the request-path cost.
- **Trace and funnel** (`src/trace.py`, `eval/funnel.py`): one row per gold family with exactly one
  terminal stage from a fixed enum. `UNKNOWN` is defined as a defect rather than a category.
- **Fail-closed execution** (`src/failclosed.py`): in benchmark mode a degraded fallback raises. The
  motivating case is the reranker: returning identity order on failure is not a ranking, it is "we
  did not rank", and downstream it is indistinguishable from "the cross-encoder agreed with the
  incoming order".
- **Oracle injection** (`src/oracle.py`): hands a stage the gold it never received, to measure the
  stages below it. It requires three independent conditions to arm (an environment flag, a valid
  stage, a non-empty gold list), stamps every report it touches, and both the funnel and the
  coverage metric refuse to score a stamped report.

---

## 4. Experiments

Each entry gives the hypothesis, the method, and the result as measured.

### 4.1 Depth confidence in scoring. **Confirmed.**

*Hypothesis:* the ranker was rewarding documents it could barely read, and displayed cards were
backed by too little text to support the claims made about them.

*Method:* replace depth reweighting with a confidence multiplier bounded below by a floor, swept at
four values against the sum of cited references' ranks over both subjects available at the time.

*Result:* delivered gold references went from 0 of 22 to 5 of 23. Median text behind a displayed
card went from 15,432 to 55,130 characters. Sweep: 576 at floor 0.75 against 836 for the previous
scheme.

*Caveat:* two subjects, and the sweep and the evaluation share those subjects.

### 4.2 External API fan-out. **Confirmed but not the constraint.**

*Hypothesis:* the corpus is a small part of the picture, and searching wider through the APIs would
reach art we do not hold.

*Method:* add the seven-source fan-out, per-query channels, source-weighted RRF, and materialise
survivors into the corpus.

*Result:* candidates went from about 40 to about 15,000. Reach on one real citation set went from 0
of 10 to 2 of 10. However, a **control run with the fan-out disabled delivered 1 of 22 against 0 of
22 with it enabled.** External art is not the binding constraint on delivery.

That control is the most useful thing in this experiment and it was nearly not run.

### 4.3 Contribution-based ranking. **Confirmed as a diagnostic; changed what we measure.**

*Hypothesis:* ranking on how many disclosures a document matches is wrong; greedy maximum coverage
over the disclosure set is right.

*Result:* the immediate finding was about the denominator, not the ranker. With the 12-disclosure
lists then in use, the top 50 covered 87% of the weighted mass and **42 of 50 slots added nothing
at all**, because after nine documents there was nothing left to add. With a realistic list of
about 80, the top 50 covers 16% and half the disclosures are untouched.

A marginal ranker needs something left to be marginal about. The coarse list had made the entire
question unmeasurable.

### 4.4 Refuted experiments

| experiment | scope | result |
|---|---|---|
| conceptual query diversity, locally generated | | no improvement |
| claim-level lexical matching | 3 configurations | 0 of 16 in every configuration |
| dedicated CPC retrieval channel | 2 variants | 0 of 12 in both |
| agentic tournament ranking, including multi-patent group comparison and iterated small-group Swiss/Borda passes | **18 variants** | refuted |
| CPC-driven corpus expansion | | 99.3% of the target art was already held |

The tournament null result is partly contaminated: a helper returned its own input when the LLM
call failed, which scrambled the ordering, and an unbounded free-text field truncated the JSON at
the token limit so whole comparison groups returned empty. Both were fixed and the experiment
re-run, still refuted, but the first pass reported a 40% ranking regression that was entirely a
broken call.

### 4.5 Funnel attribution

*Method:* 26 development subjects run at tag `v15`, 266 eligible gold families, each assigned
exactly one terminal stage reconstructed from the finished reports.

*Result:* 266 of 266 attributed, against an exit criterion of 95%.

```
stage                       n     share
NOT_RETRIEVED             118     44.4%
PORTFOLIO_EXCLUDED         51     19.2%
FUSION_TRUNCATED           43     16.2%
TOP_50                     24      9.0%
NOT_SELECTED_FOR_READING   21      7.9%
READ_NO_EVIDENCE            5      1.9%
CHANNEL_TRUNCATED           4      1.5%
```

Split by whether the corpus holds the reference:

```
                delivered  lost to RANKING  lost to REACH      n
in corpus              24              124             61    209
NOT in corpus           0                0             57     57
```

By stratum, 66% of losses in `mostly_in` are ranking; 76% in `mostly_out` are reach. **None of the
57 out-of-corpus gold references was delivered, and every one died at `NOT_RETRIEVED`.**

An earlier 10-subject version of this table contained no `mostly_out` subject and therefore
implied, incorrectly, that ranking dominated overall.

### 4.6 Oracle bounds

*Method:* four arms per subject in separate processes, on one `mostly_in` and one `mostly_out`
subject, 23 eligible gold families total. Each arm is a ceiling on what fixing everything above it
could be worth.

*Result:*

| arm | delivered | charted |
|---|---|---|
| control | 3/23 | 8 |
| `before_screen` (retrieval perfect) | 5/23 | 8 |
| `before_read` (retrieval and screening perfect) | 5/23 | 12 |
| `before_portfolio` (only selection can fail) | 6/23 | 11 |

Per subject:

```
b25f_ep3517252a1 (mostly_in, 12 gold)     control 0/12  screen 1/12  read 1/12  portfolio 3/12
b23q_ep2324952a1 (mostly_out, 11 gold)    control 3/11  screen 4/11  read 4/11  portfolio 3/11
```

Two anomalies drove the follow-up. `before_portfolio` scored **below** `before_read` on the second
subject, which an upper bound cannot legitimately do. And the second subject's control delivered
3 of 11 where the same subject delivered 0 of 11 in the batch a few hours earlier.

### 4.7 Text coverage of the gold set

*Method:* for each eligible development-split gold reference, measure the characters of chunk text
actually held.

*Result*, 326 references across 30 development subjects:

```
  readable, 3k+ characters   101   31.0%
  stub, under 3k             147   45.1%
  in the DB with no text       5    1.5%
  absent entirely             73   22.4%

                   readable      stub       absent
  mostly_in       45   32%   86   62%    5    4%
  mixed           21   36%   18   31%   18   31%
  mostly_out       3    6%    8   17%   36   77%
```

By publication authority, which matters because it decides how hard the fix is:

```
absent entirely (73)   WO 24   US 15   DE 8   EP 8   GB 4   other 14
stub, under 3k (147)   DE 38   EP 37   JP 31   US 21   WO 7   other 13
```

The two buckets have different causes and different remedies. The art we hold **nothing** for is
dominated by WO and US, which are among the easiest full texts to obtain in bulk. The art we hold a
**stub** of is dominated by DE, EP and JP, where the obstacle is full-text description in the
original language rather than availability of a record.

---

## 5. The finding

The `before_read` arm is defined as retrieval and screening being perfect, so every gold reference
should enter the read set. It read 8 of 12 on the first subject and **4 of 11 on the second, the
same four as the control**.

The injection was not broken. It is stamped, and its record reads
`n_gold=12, n_injected=3, already_present=9`, matching the funnel's `NOT_RETRIEVED = 3` for that
subject exactly. The explanation is simpler: **7 of the second subject's 11 cited references are not
in the database at all**, with zero characters of text. A family identifier can be spliced into a
ranked list. A document that does not exist cannot be screened, read, charted or delivered.

So all four arms were only ever measuring the in-corpus subset, and the ladder's flatness is an
artefact of that.

**We hold enough text to read 31% of the references we are graded on.** Nearly half are a title and
an abstract. In the `mostly_out` stratum it is 6% readable and 77% absent, and even in `mostly_in`,
62% is a stub.

This reframes the funnel's 44% `NOT_RETRIEVED`. A stub embeds and ranks poorly because there is
almost nothing to embed, so an **acquisition** failure presents as a **search** failure. It also
supplies a single explanation for the run of refutations in 4.4: every one of those experiments was
re-ordering a candidate pool in which most of the correct answers are two paragraphs long. Query
diversity, claim-level lexical matching, a CPC channel and eighteen tournament variants would all
be expected to produce null results under that condition, and they did.

**A correction we made against ourselves.** An earlier reading of the trace suggested a third of
genuinely cited art grounds no evidence when read in full. That was wrong, and the error was in
inferring "read" from the trace's charted count rather than from `by_pub`. Every gold reference
that was actually read grounded evidence, between 2 and 7 disclosures each, at read depths from
1,862 to 46,571 characters. They were not read.

### Implication for sequencing

Neither disclosure-level retrieval nor portfolio construction goes next. Both are ranking work, and
ranking work on documents we do not hold cannot pay off. The oracle demonstrates this directly
rather than by argument: perfect ranking of a family with zero characters of text delivers nothing.

The next workstream is acquisition: fetching and chunking full description and claims for
candidates the fan-out already surfaces. The `external` tier holding 13,689 rows after dozens of
runs, each of which generates about 15,000 candidates, is the quantitative statement of the gap.

The honest form of this work is a general acquisition improvement measured on the benchmark, not
fetching the gold references, which would be teaching to the test. Because the gold set is known,
this is a live risk and the holdout split exists partly to detect it.

---

## 6. Threats to validity

Listed because a reviewer should not have to find them.

1. **Run-to-run variance is comparable to our effect sizes.** One subject's control arm delivered
   3 of 11 where it delivered 0 of 11 hours earlier. Sources: LLM nondeterminism at temperature
   0.2, external API variability, and a corpus that grows during every run. **Most A/B results in
   section 4 are underpowered and we have not quantified the variance.** This is the single largest
   methodological weakness.
2. **The corpus is not fixed across runs.** Materialisation into the `external` tier means run N+1
   searches a different corpus than run N. Manifests record snapshot counts and a `comparable()`
   check exists, but the effect is not controlled.
3. **Small oracle sample.** Two subjects, 23 gold families. The per-stage ceilings should be read as
   existence proofs, not estimates.
4. **The parameter sweeps in 4.1 used the same subjects as the evaluation.** The holdout split has
   never been run.
5. **Examiner citations may be the wrong gold standard.** An examiner cites what is sufficient to
   reject, not everything relevant. Our metric may be scoring against a set systematically narrower
   than the product's actual goal, and a system that found genuinely better art would be penalised
   for it.
6. **Disclosure lists are LLM-generated.** They are frozen and hashed, so they are stable, but they
   are not human-verified. The plan calls for human verification of the primary metric and that has
   not been done.
7. **Extraction budget inconsistency.** 43 frozen lists were built with a 6,000-token output budget
   and 2 with 24,000. The coverage metric now refuses to average across differing budgets, but a
   uniform re-freeze is owed.
8. **`potential_claim` disclosures may not belong in a scored denominator** at all. They are
   inherently speculative and they are 55% weighted rather than excluded.

---

## 7. Defects found in the measurement instruments

Every one of these produced a confident wrong number rather than an error. Included because the
ratio of instrument defects to genuine findings is itself a finding.

| defect | consequence |
|---|---|
| funnel scored a run that was still in flight | 3 delivered references became 11 missing; looked exactly like a regression that did not exist |
| two tests passed for the wrong reason | one stubbed an LLM call captured at import time; the other replaced the function it claimed to exercise |
| benchmark builder not idempotent | benchmark silently grew 50 to 70 to 90 subjects across reruns |
| disclosure extractor hit `max_tokens` mid-JSON | whole response discarded; claim-rich subjects recorded as disclosing **nothing**. The 6,000-token cap could not have fitted a 160-item list even in principle |
| `"never ran"` placeholder survived | `len(items) > len(best)` is `0 > 0` when every attempt returns nothing, so two subjects reported a placeholder instead of a cause |
| `coverage_rank` defaults | corroboration 0.25 and score weight 0.35 let 20 duplicates outbid 1 unique reference |
| missing functional index | 6.1 second sequential scan over 4.98M rows on every search |
| manifest ran `count(*)` over 26.7M rows | on every search request; caught only by two concurrency tests timing out |
| `oracle_bounds` read a `.view.json` headless runs never write | every arm would have reported `delivered 0`, which reads as "injection buys nothing", the exact conclusion under test |
| four `UNKNOWN` funnel attributions | all four were retrieved by the citation graph, CPC or QBE and then dropped by fusion before ranking; now `CHANNEL_TRUNCATED` |
| module-level work plus multiprocessing `spawn` | each spawned child re-ran the entire job, four levels deep, ~1.3 GB per interpreter. Exhausted 16 GB RAM and 16 GB swap and froze the host. Several recursive copies wrote the same report file |
| my own probes were the defect three times | notably a chunk-rank probe with `iterative_scan` off reporting 780 chunks against a true ~9,000 |

Guards added for these are defect-injected: the test is verified to fail when the fix is reverted,
not merely to pass with it.

---

## 8. Questions for the reviewer

1. **Acquisition at scale.** Two distinct problems. 73 of 326 development gold references are
   absent entirely, and that set is dominated by WO (24) and US (15), both of which should be
   straightforwardly obtainable in bulk, which suggests our pipeline is at fault rather than the
   sources. Separately, 147 are stubs and that set is dominated by DE (38), EP (37) and JP (31),
   where the obstacle is full description text in the original language. What do practitioners
   actually use for the second case, and is there a legitimate bulk route we are missing?
2. **Variance control.** Is there a cheaper standard than N-fold repetition for calibrating
   run-to-run variance in an LLM-in-the-loop retrieval pipeline?
3. **Is the gold standard right?** See threat 5. If examiner citations are systematically narrow,
   what is the accepted alternative, and how do practitioners evaluate recall against art an
   examiner did not happen to cite?
4. **Fusion.** Channel weights and `RRF_K = 40` were tuned by hand on a small set. Given the
   finding in section 5, is fusion tuning worth revisiting at all before acquisition is fixed, or
   is the current fusion adequate for a pool that will look very different afterwards?
5. **Stub handling.** `DEPTH_CONFIDENCE_FLOOR` and the separate unread tier are palliative: they
   stop stubs evicting real documents but do not make stubs useful. Is there a defensible way to
   rank a title-and-abstract record against a fully read one, or is the only correct answer to
   fetch the text?
6. **Submodular selection.** Greedy maximum coverage with a corroboration term is a first
   approximation. Is there prior art in the IR literature on portfolio construction for
   invalidity search specifically, as opposed to general diversity-aware ranking?
