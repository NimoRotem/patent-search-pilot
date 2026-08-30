# Prior art search pilot: brief for an outside advisor

2026-08-06. Written for someone who knows patents and search but not this codebase.

## 1. What we are trying to build

Not "find similar patents". Every search here exists to support a legal argument about a subject
patent: which of its disclosures were already known, and where. The deliverable is a **portfolio**
of references that *collectively* address the subject's disclosures, not a ranked list of the 50
documents most similar to it.

That distinction drives everything. If a subject discloses ten things and forty nine documents all
answer the same nine, the fiftieth document is worth more if it answers the tenth alone. Ranking by
similarity, or by how many disclosures a document matches, gets this exactly backwards.

**Primary metric:** weighted disclosure coverage at 50, against a disclosure list and weights fixed
*before* the run. Scope is vacuum handling and adjacent mechanical fields: vacuum cleaning, power
tools, machinery, material handling. No software, biology or chemistry.

## 2. How the system works

**Corpus.** PostgreSQL 17 with pgvector on a single VM. 4.98M publications, 26.7M embedded chunks,
8.5M claims, 291 GB. Seeded from eight CPC branches of vacuum handling, then expanded: 2.43M "core"
publications, 2.53M "expanded", and 13,689 "external" rows acquired from API results during
searches. Embeddings are Vertex AI `gemini-embedding-001` at 768 dimensions, HNSW indexed.

**Pipeline**, per subject:

1. **Decompose.** An LLM reads the subject and produces aspects, title-shaped keywords and per
   aspect CPC codes. Product neutral on purpose, so it does not just search for the product name.
2. **Retrieve, many channels in parallel.** Semantic over chunks, query by example, CPC, the
   citation graph, and lexical. Alongside these, an external fan out to PQAI, SerpApi Google
   Patents, a BigQuery mirror of Google Patents (32.4M publications, 1976 to 2026, US/EP/WO), EPO
   OPS, USPTO ODP, OpenAlex and IP Australia. Roughly 15,000 candidates per subject.
3. **Fuse.** Weighted reciprocal rank fusion with a per source best rank, a breadth bonus for
   documents several channels agree on, and family deduplication on the DOCDB `simple_family_id`.
   Date filtering follows EPC Article 54(2) and 54(3).
4. **Screen.** An LLM scores roughly 2,500 candidates cheaply.
5. **Read and chart.** The survivors are read in full and each disclosure is grounded against
   specific passages, with a verdict of disclosed, partial or absent.
6. **Construct the portfolio.** Greedy maximum coverage over the disclosure set, so a document is
   selected for what it *adds* rather than what it matches.

A run takes about 21 minutes.

**Disclosures.** For each subject we extract four kinds and weight them by legal load:
independent claim limitations (1.0), a whole independent claim taken as a combination (0.9),
what each dependent claim adds (0.7), and "potential claims", meaning teachings the description
supports that the claims do not cover (0.55). The last category matters because claims get amended,
and a search that only checks the granted claims goes blind the moment they are.

## 3. How we measure, and why the benchmark was rebuilt

**Gold standard:** the citations an examiner actually raised, from `patents-public-data`, keeping
the X, Y and A relevance codes, deduplicated to families and filtered to art that was citable at
the priority date.

The first benchmark was built from subjects already in our corpus, and it was structurally
incapable of testing the thing we most needed to test: 297 of 306 of its citations were already
held, so reach was 100% by construction. It was rebuilt from BigQuery instead, and **stratified by
how much of each subject's cited art the corpus holds**: `mostly_in`, `mixed`, `mostly_out`.

Current benchmark: 50 subjects, 30 development and 20 held out. 502 eligible citations, of which
142 (28%) are not in the corpus. Disclosure lists are frozen to disk with a content hash before any
run, because generating them during a run means two runs of one subject are scored against two
different denominators, and a retrieval change then moves the denominator underneath the numerator.

## 4. Being able to believe our own numbers

More of the work went here than into retrieval, and in hindsight that was correct. Every item below
was found producing a confident wrong number rather than an error.

- **The funnel scored a run that was still in flight.** A subject read mid retrieval turned 3
  delivered references into 11 missing and looked exactly like a regression. Runs now carry an
  immutable manifest with a completion status, and the analysis refuses anything unfinished.
- **Two tests passed for the wrong reason**, one stubbing an LLM call it had captured at import
  time, the other replacing the very function it claimed to exercise.
- **The benchmark builder was not idempotent** and the benchmark silently grew from 50 to 70 to 90
  subjects across reruns.
- **A disclosure extractor ran out of output tokens mid JSON**, so the whole response was discarded
  and claim rich subjects were recorded as disclosing *nothing*. Two of the four unusable
  development subjects failed this way and both were among the richest in the set.
- **My own probes were the defect three separate times**, including a chunk ranking probe that
  reported 780 chunks where the true figure was about 9,000 because an index scan setting was off.
- **A missing index** made a normalised publication lookup a 6.1 second sequential scan over 4.98M
  rows, on every search.
- **A script with its work at module level, plus a multiprocessing pool using the "spawn" start
  method, re-ran its entire job inside every child it spawned.** Four levels deep, about 1.3 GB per
  interpreter, and it exhausted 16 GB of RAM and 16 GB of swap and froze the host. Several
  recursive copies were writing the same output file, so that run would have produced meaningless
  numbers even had it survived.

Two pieces of standing machinery came out of this. **Fail closed execution**: in benchmark mode a
degraded fallback raises rather than silently substituting an identity ranking, because identity
order is not a ranking, it is "we did not rank", and downstream it is indistinguishable from "the
reranker agreed with the incoming order". And **oracle injection**: a diagnostic that hands a stage
the gold it never received, stamps every report it touches, and is refused by the metric code, so
an upper bound can never be mistaken for a measurement.

## 5. Experiments

### Worked

**Depth confidence in scoring.** Hypothesis: cards were being ranked and displayed on thin text.
Result: delivered references went from 0 of 22 to 5 of 23, and the median text behind a displayed
card went from 15,432 to 55,130 characters. The single largest win so far.

**External API fan out.** Hypothesis: our corpus is a small part of the picture, and searching
wider through the APIs would reach art we do not hold. Result: candidates went from about 40 to
about 15,000, and reach on one real world citation set went from 0 of 10 to 2 of 10. Genuinely
better, but a control run showed external was not the binding constraint: 1 of 22 with it off
against 0 of 22 with it on.

**Redesigning ranking around contribution.** Hypothesis: ranking on the number of disclosures a
document matches is wrong, and greedy maximum coverage over the disclosure set is right. This
worked mainly as a diagnostic: it exposed that the disclosure list was far too coarse. With 12
disclosures the top 50 covered 87% of the weighted mass and 42 of the 50 slots added nothing at
all, because after nine documents there was nothing left to add. With a realistic list of about 80,
the top 50 covers 16% and half the disclosures are untouched. A marginal ranker needs something
left to be marginal about.

### Refuted

Each of these was built, measured and rejected. Listing them because the pattern matters more than
any one of them.

- Conceptual query diversity, locally generated.
- Claim level lexical matching. Zero improvement across three configurations.
- A dedicated CPC retrieval channel. Two variants, no improvement in either.
- Agentic tournament ranking, including comparing several patents inside one query and iterating in
  small groups. **Eighteen variants, all refuted.** Part of that null result was itself a bug: a
  helper returned its own input when the LLM call failed, which scrambled the ordering, and an
  unbounded free text field truncated the JSON at the token limit.
- CPC driven corpus expansion. 99.3% of the target art was already held.

### The two experiments that decided the direction

**Funnel attribution.** 26 development subjects, 266 eligible gold families, every one assigned a
single terminal stage. 100% attributed, against an exit criterion of 95%.

```
                delivered  lost to RANKING  lost to REACH      n
in corpus              24              124             61    209
NOT in corpus           0                0             57     57
```

By stratum, 66% of losses in `mostly_in` are ranking and 76% in `mostly_out` are reach. The single
sharpest line: **none of the 57 out of corpus gold references was delivered, and every one died at
`NOT_RETRIEVED`.**

**Oracle bounds.** Hand each stage the gold it never received and measure what survives. Each arm
is a ceiling on what fixing everything above it could be worth.

| arm | delivered | charted |
|---|---|---|
| control | 3/23 | 8 |
| retrieval perfect | 5/23 | 8 |
| retrieval and screening perfect | 5/23 | 12 |
| everything perfect except selection | 6/23 | 11 |

The ladder is flat, and on one subject the highest arm scored *below* the arm beneath it, which an
upper bound cannot legitimately do. That violation is what produced the actual finding.

## 6. The finding

**The "retrieval and screening perfect" arm never put the gold into the read set.** It read 8 of 12
gold families on one subject and 4 of 11 on the other, the same four as the control. The injection
was working correctly and is stamped and verified. The reason is simpler: **7 of that subject's 11
cited references are not in the database at all.** A family identifier can be spliced into a
ranking. A document that does not exist cannot be screened, read, charted or delivered.

Measured across the whole development split, of 326 eligible gold references:

```
  readable, 3k+ characters   101   31.0%
  stub, under 3k              147   45.1%
  in the DB with no text        5    1.5%
  absent entirely              73   22.4%

                   readable      stub       absent
  mostly_in       45   32%   86   62%    5    4%
  mixed           21   36%   18   31%   18   31%
  mostly_out       3    6%    8   17%   36   77%
```

**We hold enough text to read 31% of the references we are graded on.** Nearly half are a title and
an abstract. For the `mostly_out` stratum it is 6% readable and 77% absent, and even in `mostly_in`
62% is a stub.

This reframes the 44% of gold families that die at `NOT_RETRIEVED` in the funnel. A stub embeds and
ranks poorly because there is almost nothing to embed, so an **acquisition** failure presents as a
**search** failure. It also explains the run of refutations in section 5: every one of those
experiments was re-ordering a pool in which most of the right answers are two paragraphs long.

One correction worth flagging, because we made it against ourselves during this run: an earlier
reading of the trace suggested that a third of genuinely cited art grounds no evidence when read in
full. That was wrong. **Every gold reference that was actually read grounded evidence**, between 2
and 7 disclosures each. They were not read.

## 7. What this changes

Neither of the two candidate workstreams goes first. Disclosure level retrieval and portfolio
construction are both ranking work, and ranking work on documents we do not hold cannot pay off.
The oracle demonstrates that directly rather than by argument.

The next workstream is **acquisition**: fetching and chunking full description and claims for the
candidates the fan out already surfaces. The honest version is a general acquisition fix measured
on the benchmark, not fetching the gold references, which would be teaching to the test.

## 8. Open questions we would put to an advisor

1. **Run to run variance.** One subject's control arm delivered 3 of 11 where the same subject
   delivered 0 of 11 in the batch hours earlier. That is comparable to the gaps between our
   experimental arms, which means most of our A/B results to date are underpowered. We intend to
   calibrate this with repeats before the next comparison. Is there a cheaper standard approach?
2. **Where full text legitimately comes from at scale.** We can reach bibliographic data and
   abstracts widely, but full description text for arbitrary DE, GB and JP publications is the
   bottleneck. What do practitioners actually use?
3. **Whether examiner citations are the right gold standard.** They are what we have and they are
   real, but an examiner cites what is sufficient to reject, not everything relevant. Our metric
   may be scoring against a set that is systematically narrower than the product's actual goal.
4. **Whether "potential claims" belong in the denominator at all**, or whether they should be a
   separate report that is not scored.
