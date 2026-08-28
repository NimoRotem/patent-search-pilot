# Is this a ranking problem or a query-generation problem? Measured: both, and ranking is bigger

## The theory

Coverage of the answer space is a function of the number of GENUINELY DISTINCT query
formulations, not of the depth of any one of them. A document at cosine 0.62 from the brief's
vector is unreachable at any practical depth from THAT vector, but may sit at rank 20 from a
differently conceived one.

Grounds for stating it: 99.3% of the art examiners cite in this field is already in this corpus,
and ten of twelve cited documents on EP 3 707 092 sit beyond the 50,000 nearest chunks to the
brief. Meanwhile the ~13 formulations the pipeline does run are all paraphrases of the same
document, written by the same model from the same source, and the genuinely different
problem-shaped queries `external.plan()` produces are sent ONLY to the external APIs. The local
corpus, which holds 99.3% of the answer, never sees them.

## The experiment

`eval/reach_curve.py`. Cumulative REACH of the cited families -- does the document appear in the
channel's output at all -- as formulations are added arm by arm. Reach, not final rank, because
reach is the necessary condition.

    arm                                          queries   pool      reached
    A  brief alone                                     1    2,500      1/16
    B  + query set: essence, alts, claims              7    9,940      4/16
    C  + element queries                               7    9,940      4/16
    D  + problem-shaped aspects, run LOCALLY          39   48,600      4/16
    E  + claim-level lexical                          40   48,600      4/16

## The result: half confirmed, half refuted

**Confirmed.** Paraphrase diversity quadruples reach for free: one query reaches 1 of 16, seven
reach 4. That is the single cheapest recall gain measured anywhere in this system.

**Refuted.** Conceptual diversity does not compound. Thirty-two further queries, deliberately
product-neutral and written in other fields' vocabulary, grew the pool five-fold to 48,600
publications and reached ZERO additional cited documents. The reason is visible in what they
retrieve: a query about "attenuating noise in an air stream" returns this corpus's nearest match
to that idea, which is not the vacuum-lifter art the examiner cited. Product-neutral queries move
AWAY from the answer in a single-field corpus. They earn their keep against the external APIs,
which index every field; against a corpus of one field they are noise.

**Claim-level lexical reaches nothing here.** The existing `channel_claim_bm25` takes the four
LONGEST stemmed terms and ANDs them, which on a 1,633-character brief matches almost nothing --
that is why arm E was a no-op. A proper version was then measured: terms weighted by rarity in the
claim corpus, OR-ed with `ts_rank_cd`, restricted to claim chunks, at 8, 16 and 24 terms. All
three reached 0 of 16, each over a 2,500-publication pool. Shipping this channel would add cost
for no measured recall, so it is not being shipped. The reason it fails is worth keeping: the
"distinctive" terms a brief yields are *means, extent, rigid, central, suction, vacuum* -- the
vocabulary of the field, shared by every document in it, discriminating nothing.

## The correction that matters

On EP 3 707 092 alone the picture was reach-limited: 12 of 16 never retrieved. Across all six
benchmark subjects it is not. Of 69 cited families this corpus holds:

    displayed                                 10
    lost to RANKING (retrieved, not shown)    33     <- of which 15 were READ IN FULL
    lost to REACH   (never retrieved)         26
                                              --
    (a further 14 citations are not in the corpus at all)

**More citations are lost to ranking than to reach**, and I generalised wrongly from one subject
before the benchmark was wide enough to show it. Fifteen were read in full, cover-to-cover, and
still did not make the top 50. Ten more were retrieved and never even screened, because they sat
beyond `SCREEN_TOP`.

## What this says to do next, in order of measured value

1. **Rank the pool we already have.** 33 of 69 are retrieved and not shown; 15 of those were read
   in full. This is the largest bucket and the cheapest to attack, and it is where a reranker over
   a much larger pool belongs. Note the pipeline ALREADY reranks a large pool -- deep_rank's LLM
   screen runs over 2,500 candidates -- so the work is in the ORDERING, not in enlarging the pool.
   The 10 "retrieved, not screened" are cheaper still: they are simply beyond SCREEN_TOP.
2. **Reach the other 26 through the external APIs, not through more local queries.** Arm D settles
   that local conceptual diversity is exhausted. The external fan-out already demonstrably reaches
   documents this corpus cannot hold, and it has begun writing them back into it.
3. **Do not add local claim-level lexical.** Measured at 0 of 16 across three configurations.
