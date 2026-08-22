# Where the niche corpus ends, and how that was decided

Owner: workstream B. The executable copies are `config/niche_boundary.json` (the definition),
`src/corpus_niche.py` (the predicate) and `ops/niche_boundary.py` (the derivation). If this file
and the code disagree, the code is right.

Everything below is measured on the live corpus on **2026-08-22** and on
`patents-public-data.patents.publications` on the same day. Method is stated next to every number.
No number here was taken from an earlier document without being re-measured, because the corpus has
grown from 107,795 publications to 4,984,254 since `REACHABILITY.md` was written and every ratio in
it is now stale.

---

## 1. The question, and the thing that had to be measured first

The brief says the corpus is "one field wide" and that against a real attorney's filed prior art,
6 of 10 references sat outside the eight seeded CPC branches. The obvious response is to add
branches. The first thing to establish is what the corpus actually contains today, because the
eight `config.SEED_CPC` subgroups match 80,308 publications and the corpus holds 4,984,254.

**Method.** One sequential `COPY` of `classifications` (51.5M rows, 29 s), sorted by publication id,
counted per CPC subclass; compared against a BigQuery `COUNT(DISTINCT publication_number)` per
subclass over the whole of `patents-public-data` (17.3 GB, $0.11).

| CPC subclass | corpus | world | held |
|---|--:|--:|--:|
| B65G transport and conveyors | 855,116 | 855,116 | **100.0%** |
| F16B fastenings, and F16B47 suction cups | 546,508 | 546,508 | **100.0%** |
| B25J manipulators | 445,410 | 445,410 | **100.0%** |
| B25B work holders | 300,939 | 300,939 | **100.0%** |
| B66C cranes and load engaging | 222,051 | 222,051 | **100.0%** |
| B66F hoisting and lifting | 180,174 | 180,174 | **100.0%** |
| next best, G05B control | 124,065 | 848,792 | 14.6% |
| B65B packaging | 78,628 | 520,361 | 15.1% |
| F04F jet pumps and ejectors | 2,007 | 34,514 | 5.8% |
| A47L suction cleaners | 24,020 | 458,000 | 5.2% |
| F01N exhaust silencers | 3,817 | 373,000 | 1.0% |

**The corpus is not eight subgroups wide. It is six subclasses wide, and inside those six it is a
complete copy of patents-public-data.** Everything else in it is a co-classified or citation-reached
sliver. That changes the question from "which branch do we add" to "is there a branch worth adding
at all".

A trap worth recording, because it produced a wrong answer for twenty minutes: a subclass total is
NOT the sum of its main groups. A publication carrying both `B65G47` and `B65G49` is one B65G
publication and two rows in a main-group roll-up. Summing them reported B65G as 55% held when the
true figure is 100%. `ops/niche_world.py` therefore runs a separate `COUNT(DISTINCT ...)` per level.

## 2. The two evidence signals, and why neither of them is the gold set

`eval/attorney_gold.json`, `eval/nguyen_gold.json` and `eval/benchmark_gold.csv` are the answer key.
A boundary chosen from them would score well on the benchmark and nowhere else, and the failure
would be invisible until a holdout ran. `eval/acquisition_cohort.py` already learned this the hard
way and stated the rule: every criterion must be expressible without the word gold. So:

* **E1, co-classification.** Of the 80,308 publications carrying a `SEED_CPC` symbol, how many also
  carry candidate group *g*. What this field's own documents are about.
* **E2, examiner reach.** Take the search-report citations of those 80,308 publications, `category`
  in SEA / EXA / ISR, never the applicant's IDS. That reaches **271,507 citation edges to 150,028
  distinct documents**. Of those, how many carry *g*. What an examiner working in this field
  actually reaches for.

`ops/niche_boundary.py` computes both and imports no gold set. The gold overlap is measured
afterwards by `ops/niche_gold_check.py` and reported in section 7 as an outcome.

### The measurement that decided the whole design

Of the 150,028 examiner-cited documents, 148,942 have a row in this corpus. Splitting them by where
they are classified:

| | documents | share | method |
|---|--:|--:|---|
| inside the six subclasses held complete | 42,491 | **28.5%** | any CPC symbol with that subclass |
| classified, but outside them | 52,911 | **35.5%** | classified, no symbol in the six |
| **no CPC classification at all** | 53,540 | **35.9%** | no row in `classifications` |

The corpus-wide unclassified rate is 20.4% (1,015,875 of 4,975,809 publications have no symbol).
The examiner-cited population is **1.76x more likely to be unclassified than the corpus average**.
Unclassified art is not a rounding error in this field, it is the single largest bucket of what
examiners cite.

And the 35.5% that IS classified elsewhere is spread almost flat. The largest subclass outside the
six is `H10P` at 12.35% of that bucket, and it is a CPC restructuring class; after it come Y tagging
codes, then `B65B` at 5.1%, `H05K` 4.2%, `B29C` 3.8%, `B65H` 3.7%. Covering 80% of that bucket by
classification means most of section B plus much of G and H.

**Conclusion the evidence forces: a CPC-only niche cannot be completed by widening it.** That is why
the boundary below has closures, and why the closures are not an afterthought.

## 3. Granularity: subclass for the core, main group for the adjacency

`docs/shard_and_global_seams.md` settles that a *shard domain* is a four-character CPC subclass:
finer and the router wakes a shard per query with no reuse, coarser and B65G's 6.8M classification
rows carry no routing information. That argument is about a partition that must be reused across
queries. The niche boundary answers a different question, "which documents must exist", so the two
do not have to agree, and measurement says they should not agree everywhere.

**The core is a subclass, and it matches the shard domain exactly.** Six subclasses, already held at
100%, containing all eight seed subgroups. Nothing is gained by cutting them finer: the corpus
already holds them whole, so a finer core would only mean declaring documents we hold to be outside
the niche.

**The adjacency is a main group, and it deliberately does not match.** A subclass costs 10x to 40x
more publications for the same evidence:

| what the evidence points at | as a main group | as its subclass | ratio |
|---|--:|--:|--:|
| `F04F5` jet pumps, i.e. ejectors and venturi | 22,344 | `F04F` 34,514 | 1.5x |
| `B65B23` packaging fragile articles | 8,042 | `B65B` 520,361 | 65x |
| `C03B35` transporting glass during manufacture | 17,138 | `C03B` 448,986 | 26x |
| `H05K13` assembling electric components | 87,571 | `H05K` 1,328,198 | 15x |
| `H10P72` handling wafers and substrates | 632,815 | `H10P` 1,605,944 | 2.5x |
| all 22 admitted groups | **1,226,600** | their 12 subclasses, **6,183,000** | **5.0x** |

Taking the adjacency at subclass granularity would add roughly 6.2M world publications, more than
doubling the corpus, for the same evidence. Every admitted main group still rolls up to exactly one
subclass, so **`Boundary.shard_domains()` hands workstream E the shard list without anybody
re-deriving it**: the niche is a subset of 18 shard domains, 6 held whole and 12 entered only where
the evidence points.

## 4. The rule

A CPC main group is in the niche when

```
support(g)  = E1(g) + E2(g)              >= min_support   (100)
density(g)  = support(g) / world_pubs(g) >= min_density   (0.03)
```

with CPC section Y and the 2000-series orthogonal indexing subgroups excluded from the boundary and
kept in the record. Both are tagging schemes rather than fields: `Y02E` alone carries 5.2M
publications and `B65G2201` is an index attached alongside a real classification, so counting either
as a field puts most of the patent system in the niche.

**Why density and not count.** Ranking by raw evidence count admits `G06F3` and `B01D46`, because a
big branch is cited often for the same reason it is big. Dividing by the branch's world size asks
the acquisition question: of the documents this branch holds, what share does this field touch.

**Where the bar came from, and what else it could have been.** The seed's own parent main groups are
by definition this field, and they score:

| | `F16B47` | `B65G7` | `B65G49` | `B25J15` | `B25B11` | `B65G47` | `B66C1` |
|---|--:|--:|--:|--:|--:|--:|--:|
| density | 1.222 | 0.623 | 0.396 | 0.299 | 0.195 | 0.175 | 0.115 |

The best group outside the six core subclasses scores **0.069**, so the field's own core is 1.7x to
17x denser than anything adjacent to it. There is no natural gap in the tail below that, so the bar
is a stated choice, not a discovered one. `ops/niche_boundary.py --sweep` prints the whole curve, and
re-running it reproduces the checked-in `adjacent_groups` list exactly, 22 for 22:

| min_density | adjacent groups | world publications | world families |
|--:|--:|--:|--:|
| 0.07 and above | 0 | 0 | 0 |
| 0.05 | 9 | 133,587 | 50,337 |
| 0.04 | 14 | 355,163 | 117,204 |
| **0.03** | **22** | **1,226,600** | **391,974** |
| 0.02 | 46 | 2,049,906 | 753,663 |
| 0.015 | 64 | 2,565,415 | 987,099 |
| 0.01 | 94 | 4,200,720 | 1,712,343 |

0.03 is the last point at which the adjacency stays smaller than half the core (2.55M publications)
and the last at which every admitted group reads as handling art when its CPC title is looked up
afterwards, which is an independent check the rule never saw. Two measured alternatives were
rejected: anchoring the bar to the weakest seed parent (0.115) admits ten groups and excludes most
of the corpus we already hold, and the parameter-free Kolmogorov-Smirnov cut on the cumulative
evidence and cost curves lands at rank 228 and 9.7M publications, because the cost tail is so heavy
that maximising the gap is not a size constraint at all.

### What the rule admitted

22 main groups, each with its CPC title looked up **after** selection. Egg conveying and pick-up
(`A01K43`), shoe machines with conveyors (`A43D11`, `A43D111`, `A43D119`), household holders
(`A47G29`), sorting by feature (`B07C5`), assembly machines (`B23P21`), suspended railways
(`B61B3`), seven packaging groups covering bottle handling, fragile-article packaging, article
feeding and orientating, container setting-up and unpacking (`B65B5/21/23/33/35/43/69`), separating
articles from piles and separating superposed webs (`B65H3`, `B65H41`), severing and transporting
glass (`C03B33`, `C03B35`), **jet pumps, i.e. devices in which flow is induced by pressure drop
caused by the velocity of another fluid flow** (`F04F5`, which is the vacuum-generation art the
brief names), assembling electric components (`H05K13`) and **handling or holding of wafers,
substrates or devices during manufacture** (`H10P72`). Full evidence per group is in
`config/niche_boundary.json` under `derived.adjacent_evidence`.

`H10P72` is the one expensive admission: 632,815 world publications, 52% of the whole adjacency. It
earns it on evidence (20,690 attested documents, the third largest count in the table) and its title
is literally a handling class. It is one line in the config if a human disagrees.

### What the rule refused, and this is the important part

The branches the schmalz reach failure actually sits in:

| group | title | support | density | vs the bar |
|---|---|--:|--:|---|
| `A47L9` | details of suction cleaners | 156 | 0.0012 | 25x below |
| `F16L55` | devices for use in pipes, including silencers | 113 | 0.0009 | 33x below |
| `F01N1` | silencing apparatus | below the 100-document support floor | | not ranked |
| `G10K11` | directing sound, sound absorption | below the support floor | | not ranked |
| `A47L5` | structural features of suction cleaners | below the support floor | | not ranked |
| `B25F5` | details of portable power-driven tools | below the support floor | | not ranked |
| `F04D25` | pumping installations, i.e. blowers | below the support floor | | not ranked |
| `F04B37` | vacuum pumps | 100 | 0.0059 | 5x below |
| `B66D1`, `B66D3` | winches and hoists | 144, 223 | 0.0030, 0.0117 | 10x, 2.6x below |
| `B08B5` | cleaning by air or gas flow | 831 | 0.0126 | 2.4x below |

**Four of the seven branches the attorney's exhaust-silencer references live in do not clear a
support floor of one hundred documents across 80,308 field publications and 150,028 examiner
citations.** They are not adjacent to this field by any measurement available; the invention that
reached them reached them for a reason specific to itself. Admitting them means dropping the bar to
about 0.001, which admits several hundred groups and most of section F and G with them.

This is the decisive finding of the workstream. **The schmalz reach failure is not fixable by a
wider CPC boundary.** It is fixable by the citation closure below, by the global tier
(`retrieval.global_search`), and by nothing else that was measured here.

## 5. The closures, which is where the reach actually comes from

**Family closure.** Membership is decided at family level, so every publication sharing a
`simple_family_id` with a boundary member is a member. This is how unclassified documents get in
without a classification rule: MEASURED, family closure takes the boundary from 2,480,143
publications to 2,514,385, and more importantly it makes every subsequent count a family count, so
a German utility model with no CPC and no abstract is not silently dropped from its own family.

**Citation closure, one hop, X and Y only.** Every publication that examiner-cites, or is
examiner-cited by, a boundary member with relevance code X or Y.

| closure | reach | new families | niche families | niche publications | share of corpus |
|---|--:|--:|--:|--:|--:|
| none | 0 | 0 | 1,103,284 | 2,514,385 | 50.4% |
| X/Y only | 545,994 | 503,812 | **1,607,096** | **3,079,166** | **61.8%** |
| all examiner categories | 2,480,308 | 2,041,704 | 3,153,671 | 4,713,278 | 94.6% |

The unrestricted examiner closure takes the niche to 94.6% of the corpus, which is not a niche. X
and Y are the codes that threaten novelty and inventive step; A is background and the applicant's
IDS is a dump of everything the applicant knew, not the result of a search (one US publication here
carries 5,771 IDS citations against 11 from the search report). The restriction is applied
field-wide across 2.5M publications, not to the benchmark subjects, so it is not a gold criterion.

Cost comparison, which is the argument for doing it this way at all: the citation closure buys the
whole examiner-reachable neighbourhood of this field for **545,994 documents, 32,355 of which this
corpus does not hold**. Buying comparable reach by widening the CPC costs about **6.9M
publications** and still cannot touch the 35.9% that carries no classification.

## 6. The answer to "should the niche boundary match the shard boundary"

**Half of it should and half of it must not, and both halves are measured.** The core is a subclass,
identical to a shard domain, because the corpus already holds those six whole and a finer core would
only disown documents we have. The adjacency is a main group, five times cheaper than the same
evidence at subclass granularity, and it still rolls up to exactly one shard each.
`Boundary.shard_domains()` is the seam: it returns the four-character domains the niche touches plus
`shard_router.UNCLASSIFIED`, and `corpus_niche.subclass_of` is `retrieval.shard_router.domain_of` to
the letter, including for a symbol it cannot parse. That last detail is not cosmetic. If
`subclass_of` returned `""` for an unclassified publication where the router emits
`"unclassified"`, a shard registered under one name and a route emitted under the other would never
meet in `shard_manager.hot_domains`, and the 1,024,320 publications that carry no classification,
20.6% of the corpus and the population the gold lists are drawn from, would go quietly unreachable
in the tier built to reach them. `docs/shard_and_global_seams.md` rule 5.6 states the contract and
`tests/test_corpus_niche.py::test_subclass_of_is_domain_of_to_the_letter` holds the two functions to
it symbol by symbol. `corpus_niche.shard_domains_of(record["cpc"])` maps a manifest family onto its
shards and returns `["unclassified"]` for the 294,327 families that have no symbols at all.

## 7. Gold overlap, reported as an outcome

Measured by `ops/niche_gold_check.py` after this file and `config/niche_boundary.json` were frozen.
The numbers are in `docs/corpus_completeness.md` section 5. Nothing in that check may be fed back
into the boundary.

## 8. What this boundary does not settle

* **It does not make the niche complete.** Section 2 of `docs/corpus_completeness.md` says how
  incomplete it is and where.
* **It cannot reach art nobody has cited and nobody classified here.** That is the global tier's
  job, and the measurement in section 4 is the strongest available argument for funding it.
* **The 0.03 bar is a choice.** It is one line in `config/niche_boundary.json`, the sweep that
  would change it is one command, and both are recorded so a future decision is a decision.
