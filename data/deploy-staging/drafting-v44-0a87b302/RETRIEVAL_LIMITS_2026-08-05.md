# Where the cited art actually is, and what that rules out

The previous round fixed the scoring stage and took the benchmark from 0/22 to 5/23. This round
went after the biggest remaining bucket: the cited documents this corpus HOLDS, has EMBEDDED, and
never retrieves. Most of what follows is negative results, which is the point: three plausible
fixes are now measured and dead, and one large piece of work is ruled out entirely.

## The baseline, on six subjects

    subject           displayed   cited families in corpus
    ep3707092            2/16
    schmalz               2/7
    suction_chuck        3/13
    suction_unit         2/12
    suction_display      1/11
    robot_gripper        0/10
    TOTAL               10/69      (14%)

This is the first number worth trusting, and it is lower than the previous round's 5/23 suggested.
The per-subject spread is 0% to 29% on the same pipeline, which is exactly why one subject was
never enough to tune on: any single-subject reading is a sample from that spread, and a change
worth +1 on one subject is indistinguishable from picking a different subject.

## First, the benchmark had to be trustworthy

**It was measuring its own answer key.** A link or upload search names no subject, so `subject` was
None, so `retrieval._date_clause` excluded nothing. Measured on EP 3 707 092:

* the subject's OWN family came back at **rank 1 of its own results**, wasting a display slot on
  the invention being searched;
* `retrieval.channel_citation_family` expands the backward citations of whatever it retrieves, so
  the subject's own citation list, the examiner's answer key, was expanded into the candidate pool.
  All six of its in-corpus backward citations were in the ranked list;
* with no effective filing date, art published AFTER the invention was eligible to be returned as
  prior art against it.

`webapp._generate` now recovers a `Subject` from the ingested document (local row first, then App
A's merged record). One change closes all three, because `_date_clause` consumes both the date and
the number. This is a correctness fix for every link and upload search, not just the benchmark.

**Two subjects could not tell a gain from noise.** Now six, 83 citations, via
`eval/collect_subject.py`. The gold definition matters and was nearly wrong: `citations.category`
records WHO supplied each citation, and `origin` its search-report relevance code.

    APP   the applicant's disclosure statement. One US patent here carries 5,771 of these
          against 11 from the search report. An IDS is a dump of everything the applicant knew;
          scoring a search engine against it measures the wrong thing and is unreachable anyway.
    SEA / EXA / ISR   the search report, examiner, international search report.
    origin X / Y      particularly relevant alone / relevant in combination. This is the gold.
    origin A          background.

## Then: why are 12 held, embedded citations never retrieved?

**They are not near the query, and nothing else can see them either.** Measured on EP 3 707 092:

    US-10625955-B2   best chunk at rank  1,929   cos 0.762   retrieved, displayed at 5
    US-5344202-A     best chunk at rank 19,893   cos 0.732   reachable only by a much wider fetch
    the other ten    beyond the 50,000 nearest chunks, cos 0.568-0.714

The seed pass fetches 9,000 chunks. Ten of twelve sit beyond 50,000, so **widening the dense fetch
is not the fix**: going to 30,000 chunks costs 44 seconds and still reaches only 6,809
publications. On a single whole-brief pass, ten of the twelve appear in **no channel at all**.

### Two CPC fixes tried, both worse. Recorded so they are not retried.

The CPC channel looked like the obvious lever, and it is genuinely broken: nothing in the pipeline
ever passes `cpc_hints`, so it always falls back to all eight seed branches (~82,000 publications)
and ranks by `count(*)`, the number of matching symbols a publication carries. That is not
relevance, it is how heavily classified a document is, so it returns much the same documents for
every query in the corpus. It surfaced 2 of 12, at ranks 1,251 and 2,139 out of 2,500.

    1. Narrow the pool to the SUBJECT'S OWN symbols, rank by specificity      0 of 12
    2. Keep the broad pool, order by deepest shared prefix with the subject   0 of 12

Both fail for the same measured reason: **examiner citations do not sit in the subject's own
subgroups.** The cited documents share only 3 to 10 characters of CPC prefix with EP 3 707 092,
most of them 3 (the subclass), and live in neighbouring groups: B25J robot grippers, B65G
conveyors, B23Q machine tools, not its own B66C1/023. Narrowing excludes them by construction, and
proximity-ranking a capped pool spends the whole budget on the neighbourhood where they are not.
Reverted to the original; the docstring now carries both dead ends.

The honest reading: these documents are beyond reach of both available signals at this pool size.
Improving this needs a different signal or a much bigger pool, not a better ordering of 2,500.

## And a large piece of work ruled out

The obvious response to "14 of 37 citations were not in the corpus" is to index more, and the
obvious way to get that wrong is to guess which classifications to add. `eval/cpc_gap.py` measures
it: take every publication in the indexed field with search-report citations and look at where the
CITED documents are classified.

    examiner-cited documents reachable from the indexed field   59,988
    of those, held in this corpus                               59,567  (99.3%)

**The corpus is not the constraint for in-field art.** It already spans 529 subclasses beyond
SEED_CPC through family and citation expansion, including B65G (9,278 cited documents held) and
B25J (4,817). The 14 misses were specific to those subjects, which cite a lot of old foreign art
and utility models, not a classification gap. A CPC-driven corpus expansion is not warranted, and
that is a large ETL saved.

## Smaller things, closed

* **The learning loop is closed and verified.** 1,854 externally-discovered rows had 0 embeddings,
  so art the fan-out found could not be retrieved next time. `ops/embed_external.py` chunks and
  embeds them: 4,462 chunks, 0 left unchunked, embedding norms matching the corpus baseline
  (0.5846 against 0.5897). The weekly refresh drains the same queue with no tier filter, so this
  now maintains itself; the script exists so it can be done on demand and verified rather than
  assumed.
* **The live path now leaves a SerpApi reserve.** `ops/enrich_field.py` refuses to start without
  one; the live search path had none, so the batch job stopped politely while searches spent the
  allowance to zero mid-run and on-demand full-text enrichment silently went dark. `enrich.
  may_spend()` is a separate function precisely because the test suite stubs `fetch_details` out
  entirely, so a test driven through it would pass whether the guard existed or not. Writing that
  test also found the guard re-reading the account on every reference when the endpoint was down.
* **Lens has been returning 401 on every search** while reporting healthy in `/api/health`. One
  whole source contributing zero. Reported to the advisor; needs a renewed token.
