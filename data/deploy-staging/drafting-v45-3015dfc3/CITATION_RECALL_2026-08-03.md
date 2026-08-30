# Measuring the pipeline against a real examiner citation list

The test: take the 27 publications cited against **EP3707092B1** and ask where each one lands in a
search started from that patent. Blind procedure, four runs: measure, form a theory, change the
code, re-run, measure again. Nothing in the code knows this list.

**Harness:** `eval/citation_recall.py` (where each cited document reached: not in corpus, never
retrieved, retrieved, screened, read, displayed) and `eval/gold_probe.py` (corpus presence and
text depth). Both take the citation list as an argument.

## Result

Counting FAMILIES, because the report shows one card per family, and excluding EP3707092's own
family (see defect 1 below):

| | in the ranked top 50 | surfaced on the page |
|---|---|---|
| **v1** baseline | **4 of 22** | 4 |
| v2 after the retrieval fixes | 3 | 3 |
| v3 after depth-weighted scoring | 5 | 5 |
| **v4** after two-tier ordering | **7 of 22** | **9 of 22** |

The seven land at ranked positions 4, 15, 16, 17, 18, 21 and 23. Two more are named in the
"identified but not readable here" section.

## What the corpus actually holds, which bounds everything

26 of the 27 cited publications are in the corpus (23 distinct families); only JPH0611987U, a
Japanese utility model, is absent. That is not the problem.

**15 of the 26 have no claim text at all.** They are abstract-only records: a title, one short
abstract, a classification. That is the shape of the whole corpus, 84% of which is abstract-only,
and it is exactly the shape of the documents an examiner cites: old, foreign, utility models.
Every failure below is downstream of that fact.

## Defect 1: the search returned the searcher's own patent as prior art against itself

The #1 result was US-11413727-B2, which is the US member of EP3707092's own 29-member DOCDB
family. It had been the #1 result on every previous run of this search, under three different
publication numbers, because the existing guard excluded only the exact publication number the
document identified itself by.

Fixed in `webapp._drop_self_family`: when the uploaded document identifies itself, its whole
simple family is removed. This also means the v1 baseline of "5 displayed" was really 4.

## Defect 2: the funnel is biased against short documents, by construction

The dense channel takes the best chunk per publication out of a global top-K. A long modern patent
contributes a hundred paragraph chunks and gets a hundred chances to be in that top-K; a document
whose entire indexed text is a forty-word abstract gets one.

Measured, same query, same K = 9,000 chunks:

| scope | distinct publications returned |
|---|---|
| all chunk kinds (what the pipeline did) | 2,330 |
| abstract and whole-document chunks only | **6,109** |

**Nine cited documents that the all-kinds channel never returns at all appear in the
abstract/whole pool**, several inside its first 3,000. Fixed with a `brief_dense` channel, weighted
below `dense` because a summary match is weaker evidence than a passage match.

Worth recording: the essence sentence, which the previous study showed was dramatically better for
one reference, is dramatically *worse* for these. WO1997044592A1 sits at 1,491 on the brief and
5,737 on the essence. Neither formulation dominates, which is the argument for the query set.

## Defect 3: the screen read 600 of 7,328 ranked families

Eleven of the 23 cited families were retrieved and then never looked at, sitting at fusion
positions 625 to 5,731. The screen is the cheapest stage in the pipeline: 600 candidates cost 11
seconds. Raised to 2,500 (~30 s), and the read depth from 150 to 300.

The screener was also given the **CPC symbol and the publication year**, which it had never seen.
For an abstract-only 1923 filing the classification is often the strongest evidence available, and
withholding it forced the model to score those documents on forty words. Its instruction changed
from "how many core elements does this disclose" to "how likely is it that reading this in full
would be worth your time", because a forty-word abstract cannot demonstrate several elements and
scoring it low for that is scoring it for being old.

## Defect 4: the ranking punished a document for being short

Grounded coverage is a proportion whose denominator assumes the document had a chance to disclose
every feature. It cannot. Measured: the candidate with the **highest screen score of all 2,500**
came 67th, behind long documents its own reader had scored lower, because 9,160 characters cannot
carry twelve grounded quotes.

The coverage term is now weighted by how much text there was to ground in, and what it gives up
goes to the two judgements that do not depend on length: the reader's holistic verdict and the
screen. All three remain gated on the reference having been read at all.

## Defect 5, which only a live run could have found: a score cap is a magnet

Holding unreadable references below readable ones with a score **cap** looked right and is wrong.
Every abstract-only record whose screen cleared the cap scored *exactly* the cap, so they formed a
plateau, and that plateau sat above the natural range of genuinely-read documents.

Measured on the v3 run: **44 of the 50 displayed references were abstract-only records sitting at
exactly 70**, and four cited references that had been read in full with grounded quotes were
pushed off the page by documents nobody could read.

Two incomparable things were being sorted into one list. They are now two: the ranked list is
references whose full text was read and quoted, and everything else is named in an **"identified
but not readable here"** section with the screen's judgement and a link to the office copy. The
cap survives only as a ceiling on what a card may display, so a screen score is never presented as
if it were evidence.

## What still misses, honestly

Of the 22 in-corpus families that are not the subject's own, 13 did not reach the ranked list:

- **3 never retrieved at all**: DE29916647U1, JPH06335877A, FR2561564A1. All abstract-only.
- **5 retrieved but beyond the 2,500 screened**: at fusion 2,664 to 5,313. All but one
  abstract-only.
- **4 screened and not read**: two scored 60 and two scored 10. The two that scored 10 (US2920916A,
  US3506297A) are genuinely relevant vacuum lifters whose abstracts the screener dismissed.
- **1 read and ranked 71st**: GB207177A, a 1923 filing.

**The single change that would move most of these is not a ranking change: it is text.** Twelve of
those thirteen are abstract-only in this corpus. `ops.py` (EPO OPS) and the SerpApi path can fetch
claims and description for DE, EP, WO and JP documents on demand. Enriching the top few hundred
screened-but-thin candidates before the reading stage would move them from the second tier into
the first, where they can be ranked on evidence like everything else. That is the next experiment,
and it is a corpus change rather than a scoring change.

**Run-to-run variance is real and should temper any single number.** The agent's element queries
are model-generated, so retrieval order moves substantially between runs: FR2714037A1 was card 25
in v3 and unscreened at 3,472 in v4; CN2806399Y was card 22 and then 2,664. A recall figure from
one run of this pipeline carries roughly ±2 families of noise. The v1-to-v4 movement is larger than
that; a v3-to-v4 comparison of any single document is not.

## Cost

The run is ~18 minutes: retrieval ~11, screening 2,500 ~30 s, reading ~330 references in full
~5 minutes (12.7M characters).

---

# Round two: on-demand text, and four things that did not work

The first round left 12 of 13 remaining families abstract-only, and named text as the next lever.
Five more runs, same blind procedure.

## Final result

| | ranked top 50 | surfaced on the page |
|---|---|---|
| **v1** baseline | **4 of 22** | 4 |
| v4 two-tier ordering | 7 | 9 |
| v5 + on-demand text | 7 | 9 |
| v6 + wider funnel | 4 | 7 |
| v7 + interleaved screen batches | 5 | 7 |
| v8 + narrower read set | 5 | 7 |
| **v9** funnel reverted | **7 of 22** | **9 of 22** |

v9 displays ten cited publications at ranked positions 6, 6, 14, 17, 18, 18, 22, 23, 23 and 35.

## What worked: fetch the text, before reading

Measured per source on the 15 cited documents this corpus held no claims for:

| source | supplied full text |
|---|---|
| SerpApi Google Patents | **10 of 15**, about a second each, including every DE, FR, CN and JP one |
| lemad Mongo | 1 of 15 |
| EPO OPS | 0 of 15, because its full text is EP and WO only and none of these were |

So the pipeline now fetches the missing text for the references it has just decided to read, and
persists it. `enrich.enrich_publication` already did exactly this and stores the claims **without
embedding them**, which is the right shape: the reading stage needs text, not vectors, and the
vectors follow on the next ordinary embed pass. Measured live: 49 of 80 references gained full
text in 19 seconds.

Two more that cost nothing:

- **The screener was reading nothing at all for old US patents.** They have no abstract row and no
  claims table here; their whole disclosure is in paragraph chunks, which the screen never looked
  at. It scored two genuinely relevant vacuum lifters 0 and 10 on an empty string. With a
  description fallback, one of them came back at rank 49.
- **A low score from a screener shown nothing is not evidence of irrelevance.** Candidates the
  screen could not see text for are now read anyway if the retrieval ranked them well.

## What did not work, and is recorded so it is not retried

**Widening the funnel lowered recall, three runs running.** Screen 2,500 to 5,000 and retrieval
publication cap 2,500 to 6,000: 7 families became 4, then 5, then 5, while the two narrower runs
had scored 7 twice and a third narrow run scored 7 again. The reasoning for widening was sound and
the effect on retrieval was real, cited references moved from fusion rank 3,000-4,000 to 141-191.
It still lost, because every stage below is a fixed size and a wider pool is more competition at
each of them. **Widening a funnel only helps if the stage below it widens too, and the page does
not.** The same shape killed a larger read set: charting 504 references instead of 344 pushed
cited art from ranks 30-47 down to 54-184.

**Cutting the screen's text budget to afford a deeper screen.** Tried, and the A/B that settled it
is worth keeping: re-screening identical batches at 950 and 1,600 characters moved the cited
references by +1.9 and the other 237 candidates in those batches by -0.1. The budget explains
almost nothing. That measurement cost three minutes and saved a thirty-minute run on a wrong fix.

**But it exposed a real defect.** The same publication scored 85 in one run, 60 in the next and 75
on an isolated re-screen. The screener judges 25 candidates in one call and calibrates within it,
and the batches were contiguous slices of a rank-ordered list: the first batch was all excellent
documents spread over 60-95, the two-hundredth all mediocre ones spread over 20-60. Those numbers
were never comparable, and they are used both as the threshold for what gets read and as a term in
the final score. Batches are now interleaved round-robin so every batch spans the whole ranking.

## The remaining ceiling is the corpus, and it is not a per-search problem

Of the 13 families still outside the ranked list: 3 are never retrieved, 6 sit beyond the screened
2,500, and 5 are screened at 50 to 70 and not read. Almost all are abstract-only.

Per-search enrichment is bounded by quota and by time: 80 documents per search is already 80
SerpApi calls. The fix that would actually move this is a **batch enrichment of the field**: about
50,000 abstract-only publications sit in the 8 seed CPC branches, and at roughly a second each
that is a background job measured in days and a SerpApi tier, not a search-time cost. It would
also feed retrieval, because the new text gets embedded on the next pass, which per-search
enrichment deliberately does not.

---

# Round three: the field text backfill

## What was done

9,000 SerpApi calls against the field, ordered by in-corpus citation in-degree, then chunked and
embedded.

| | before | after |
|---|---|---|
| field publications with claims | 16,614 of 81,890 (20%) | **23,629 of 81,890 (29%)** |
| new claim chunks, embedded | | **145,102** |
| SerpApi hit rate | | 77.5% (6,972 of 9,000) |
| cost | | 9,000 of a 15,000/month allowance already paid for |

Two real bugs surfaced doing it, both invisible at single-document scale:

**`enrich.gp_id` never resolved a US pre-grant publication.** It stripped hyphens, so
`US-2015032252-A1` became `US2015032252A1`, and Google Patents only answers to the zero-padded
`US20150032252A1`. Every pre-grant lookup asked for a document that does not exist, got nothing,
and still spent a call. Measured on five: "no claims" before, 22 to 39 claims after. **The backfill
hit rate went from 27% to 77.5%**, so roughly two thirds of the allowance was being burned on
nothing, and the on-demand path inside every search had the same defect.

**Chunking a publication scanned the whole chunks table.** `id NOT IN (SELECT ref_id FROM chunks
...)` is an uncorrelated subquery over 16 million claim chunks, run once per publication. Invisible
when a search chunks one document; in a backfill it managed fewer than a thousand publications in
twenty minutes. Publication-scoped, it does ten a second.

## What it bought, and what it did not

It did **not** move the headline: 6 families ranked and 7 surfaced, against 7 and 9 the run before,
which is inside the ±2 run-to-run variance. Saying it improved recall on this search would not be
supportable.

What it did buy is real and permanent:

- **Four cited documents changed state from abstract-only to having claims**, so they can be read
  and ranked on evidence instead of only listed. Two of them now display at ranks 13 and 25.
- The whole field is 42% better covered, for every future search, not just this one.
- On-demand fetching during a search dropped from 80 references to 54, because the corpus already
  holds what it used to go and get.

**Why the metric did not move.** Only 7 of the ~20 cited documents were inside the 9,000-target
window; six had already been enriched by the per-search path in earlier runs; and the remaining
misses do not fail for want of text. Four are never retrieved at all and six sit beyond the 2,500
screened, at fusion ranks 2,500 to 5,400. Text does not fix reach.

And reach is the lever that was already measured to backfire: widening the screen and the
retrieval cap lowered top-50 recall three runs running, because every stage below is fixed-size.

## Where this actually stands

Top-50 recall on this citation list has gone from 4 of 22 to 6-7 of 22 and plateaued. Six separate
levers have been tried: the query set, funnel width, screen depth, read depth, the scoring
function, and now corpus text. The first and the last are permanent improvements; funnel width was
measured to hurt.

The honest next levers, in the order I would try them:

1. **Three of the cited documents have no CPC rows at all** in this corpus, so every
   classification-scoped operation is blind to them, including the field definition this backfill
   used. That is a data gap in the BigQuery ingest, not a ranking problem.
2. **Three others sit in `B25B11/007`**, a sibling of the seed `B25B11/005`. The field is defined
   by 8 CPC subgroups and the art an examiner cites does not respect that boundary. Widening
   `SEED_CPC` is a one-line change with a real consequence: it recalibrates `domain_detect`, so it
   needs its own measurement rather than being slipped in.
3. **Finish the backfill.** 41,000 field publications still have no claims, and the remaining
   allowance is 1,587 this month. At 15,000 a month that is roughly three more months, or one
   month on a larger tier.
