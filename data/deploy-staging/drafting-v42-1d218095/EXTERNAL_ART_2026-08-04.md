# Reaching art the corpus does not hold

Two blind runs against two real citation lists said the same thing from opposite directions.

    EP 3 707 092 B1    22 cited families, most of them IN this corpus. The job is RANKING.
    US 2026/0109053    10 cited documents. Four are classified in acoustics (G10K11/161),
                       exhaust silencers (F01N1/00), vacuum cleaners (A47L7, A47L9) and power
                       tools (B25F5/026). This corpus indexes eight CPC branches of vacuum
                       handling. The job is REACH: no ranking change can produce a document
                       that was never retrieved from anywhere.

A federation already existed for the second job and returned 40 hits, none of the four. This
records why, what was changed, and what each change was measured to be worth. Nothing here is
tuned to either subject: every fix is a defect that was visible without knowing the answers.

## Four structural defects, each fatal on its own

**1. The federation returned 45 families, total.** App A's `/api/search` is an agentic loop that
ends in an LLM-picked shortlist sized for its own results page. The pilot ranks several thousand
local families, so 45 external ones are a rounding error however good they are, and the shortlist
has already discarded the recall the call was made to buy.

Added `POST /api/bulk_search`: hand it a list of native sub-queries, it fans them across the
adapters and returns the RAW candidates. No planner, no LLM, no shortlist, no detail fetch.

    two queries    172 candidates    2.9 s     (against 40 families in 242 s)
    57 queries   15,265 candidates  108.7 s

**2. It asked one question, in the invention's own words.** A brief-shaped query describing the
whole invention retrieves art that looks like the whole invention, which is exactly the art this
corpus already holds. Remote art is reached by asking about the PROBLEM in the other field's
vocabulary: an examiner cites a lawn-mower muffler against a vacuum gripper because both attenuate
noise in a moving air stream, and no query containing the words "vacuum gripper" will ever return
it. `external.plan()` decomposes the invention into 6-9 product-neutral aspects and asks each one
separately as both a keyword query and a semantic blurb.

**3. The biggest source was blind by construction.** The BigQuery adapter SKIPS any sub-query
carrying no CPC hint, and the planner fell back to the plan-level CPC, i.e. the invention's own
classes. So the source with 32 million publications was only ever asked about the field we
already index. Each aspect now carries its own candidate subclasses, proposed for the problem
rather than for the product, which is what lets a query reach G10K or F01N.

**4. External art could not be judged even when it was found.** A federated hit lived in its own
block on the report, outside `ranked_families` — the list the screen, the reader, the claim
charter and the renderer all consume. It was never screened, never read, never scored on
evidence. It could only ever be listed, so it could never win. `external.materialise()` now
writes each genuinely new publication into the corpus as `tier='external'`, after which it is an
ordinary row and every stage treats it like one.

## Then the queries had to actually work

Reach on the ten Schmalz citations, measured at the top of the funnel only (does the fan-out
return the document at all — no ranking, no screening):

    one brief-shaped federated query                       0 / 10      40 hits
    problem-shaped aspects, 4 CPC per query             0 / 10   2,729 candidates
    + title-shaped keywords                             0 / 10   5,390 candidates
    + one query per CPC subclass, weighted matching     2 / 10  16,579 candidates

The two newly reached are US 11,206,961 and US 5,269,665 — both documents this corpus does not
hold and never will.

Three defects found on the way, all of them things that were simply wrong rather than untuned:

* **`ORDER BY RAND() LIMIT 100`** returned a 0.1% random sample of the match set. The same query
  twice gave different documents. Now ranked by how many query terms the title matched, with
  RAND() only as the tie-break inside a tier, and the limit raised to 400 (measured at 10 MB
  billed per query, so this was never the constraint anyone thought it was).
* **`LOWER(title) LIKE '%air%'`** also matches chair, repair, hair, airbag and staircase. Word
  boundaries now, which is the difference between a selective term and one matching a large
  fraction of every subclass.
* **Four CPC subclasses in one query share one row budget**, so the largest crowds out the rest
  and a document sitting in exactly the subclass we asked about never came back. One query per
  subclass.

And two data facts that were wrong in the code's own comments:

* The BigQuery cache docstring said "US/EP/WO since 2000". It is **1976-2026, 32.4 M
  publications** (US 17.2M, EP 9.1M, WO 6.1M). Believing the docstring would have ruled the
  source out for exactly the old art examiners cite most. Nine of the ten Schmalz citations are
  in it; only the 1960 one is not.
* The USPTO adapter falls back to `US<applicationNumber>` when a record has no publication or
  patent number, yielding ids like `US35530491`. Those match nothing anywhere and would have been
  INSERTED into the corpus as permanent junk rows. `external.plausible()` rejects them.

## Fusion had to change too

Plain RRF assumes channels are independent evidence. Thirty-odd BigQuery title queries with
overlapping keyword sets are one source asked thirty ways, and summing them counts the same weak
signal thirty times — a generic document matching a common word in many aspects then outscores
the remote-field document that exactly one aspect was written to find. Which is the document the
whole fan-out exists to retrieve.

    channel depth capped at 100     past there a title-keyword channel is ordered by RAND(),
                                    so the tail is not a weak signal, it is no signal
    scored per SOURCE, not per query  each source contributes once, at its best rank
    weighted by source              semantic hit 1.0, Google 1.0, US titles 0.7, title match 0.5
    breadth kept as a bounded bonus  it can break ties, never overturn a strong hit

## Two smaller fixes with the same root

**`pubnorm` dropped the wrong zeros.** US 2014/0008929 A1 has serial `0008929`. Google keys it
`US20140008929A1`; this corpus stores `US-2014008929-A1` — ONE zero gone, not three. The old code
emitted the padded form and the fully-stripped `US20148929A1` and never the form actually on disk,
so a document we hold looked absent: it would be re-inserted from an external source and then
ranked and displayed as a second copy of itself. Now emits the whole ladder, padded first (which
is what every outbound Google and Espacenet link is built from).

**External art skipped the date filter.** The local channels are date-filtered inside their own
SQL; families spliced in after retrieval bypassed it, and a link/upload search names no subject so
it runs with no cutoff at all. External sources skew recent, so an unfiltered fan-out injects art
that POSTDATES the invention. `external.citable()` applies `search_modes.citable_where` — the same
function, so there is one definition of citability — and `subject_from_doc()` recovers a cutoff
for uploaded documents from the local row or App A's merged record.

## What it was worth end to end, and what actually turned out to be the constraint

The fan-out was measured against the two-subject benchmark, with the external channel switched
off as the control (`EXTERNAL_ENABLED=0`, everything else identical):

    tag        external  deep_rank scoring   ep3707092   schmalz   TOTAL
    v11        on        depth-reweighted       0/16       0/6      0/22
    v11noext   OFF       depth-reweighted       0/16       1/6      1/22
    v12        on        depth-CONFIDENCE       2/16       3/7      5/23

**The external channel was not the constraint, and the control proves it.** Turning it off scored
1/22 against 0/22 with it on: indistinguishable at a run-to-run variance of +/-2. Fifteen thousand
extra candidates bought nothing, because whatever reached the screen was then ranked by a scorer
that was throwing the answer away. Only 2 of 50 displayed cards ever came from the external
channel, so it was not crowding the page either.

The constraint was `deep_rank`'s score, and the defect was visible in the saved reports:

    WO-2017215163   coverage  7, read   8k chars -> 77   displayed at rank 10
    US-10625955     coverage 52, read 142k chars -> 63   NOT displayed, rank 53

`w_cov` scaled with how much text was read, and the weight it gave up went to `overall` and
`screen` — judgements made from a snippet. So the LESS of a document we read, the MORE its score
came from a guess, and the guess is optimistic. A reference measured to disclose half the
invention from its full text lost to one measured to disclose almost nothing, because the second
had been read too thinly to be measured and inherited its screener's optimism. The median text
behind a displayed card was 15,432 characters.

Fixed by holding the weights fixed and letting a shallow reading DISCOUNT the score instead of
redistributing it (`DEPTH_CONFIDENCE_FLOOR`). Not reading a document can no longer raise its rank.
Swept over both subjects at floors 0.30 / 0.45 / 0.60 / 0.75 on the sum of the cited references'
ranks: **836 before, 576 at 0.75**, which is also the gentlest setting — and gentleness matters
because a large share of the art examiners cite is abstract-only here. Displayed median text read
went 15,432 -> 55,130 characters.

Every cited reference that had been read but buried moved up sharply:

    US-10625955   57 -> 5      DE-3724659    61 -> 40
    US-11413727  237 -> 7      US-4453755   113 -> 42
    US-2014008929 81 -> 20     US-10794526  281 -> 102 (still not displayed)

This supersedes part of an earlier finding, recorded in
`test_a_short_document_is_not_driven_to_the_floor_for_being_short`. That fix was right that short
documents were being punished; it over-corrected into rewarding them.

**The corpus grew from searching.** `US 5,269,665` was NOT IN CORPUS on every previous run and is
now held, materialised by the fan-out that found it. Its own retrieval will follow on the next
chunking pass.

## Consequences worth knowing

* The corpus now GROWS from searching: `tier='external'` rows are inserted by live searches, and
  they enter the ordinary chunking queue, so art found once becomes retrievable by vector for
  every later search. `unchunked_publication_ids(tiers=("core","expanded"))` scopes the ETL
  resumability invariant, which is no longer a property of a corpus that has been searched.
  Everything inserted this way is removable with one `DELETE ... WHERE tier='external'`.
* Lens has been returning **401** on every search since before this work. It is enabled in the
  health output and contributes nothing.
* `US 2,966,138` (1960) is outside the BigQuery cache's 1976 floor. Pre-1976 art is reachable
  only through SerpApi and PQAI.
