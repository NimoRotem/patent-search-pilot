# How complete the niche corpus is

Release `niche-2026-08-22`, boundary sha256 `430e1170…`, measured 2026-08-22 against the live
corpus and `patents-public-data`. Regenerate with

```
python ops/niche_extract.py        # ~4 min of sequential reads against the live corpus
python ops/niche_world.py --run    # ~$0.35 of BigQuery
python ops/niche_enumerate.py      # ~6 min, no database access
python ops/niche_report.py --markdown
python ops/niche_gold_check.py     # after the boundary is frozen, never before
```

The boundary itself and how it was chosen are in `docs/niche_boundary.md`. Every number below
carries its method. "Held" means a publication ROW exists, which is not the same as a document:
that distinction is the whole point of section 3.

---

## 1. The niche, in one table

| | families | publications | method |
|---|--:|--:|---|
| in the niche at this boundary, **worldwide** | **1,398,816** | **3,482,514** | `COUNT(DISTINCT family_id)` over `patents-public-data.patents.publications` restricted to the boundary; 19.4 GB scanned |
| this corpus holds, from the CPC boundary | 1,103,690 | 2,492,930 | `classifications`, then family closed |
| this corpus holds, whole niche incl. closures | 1,607,502 | 3,057,711 | the manifest, 1.9 GB of NDJSON |
| holds claim text | 261,479 | | at least one member with >= 200 claim characters |
| holds description text | 288,171 | | at least one member with >= 800 description characters |
| **holds COMPLETE text** | **173,725** | | one member with both |
| carries no classification at all | 294,327 | | `cpc == []` on the record |
| reachable only from an external source | 32,355 | | X/Y cited by the niche, no local publication row |

**Coverage of the CPC boundary: 78.9% of the world's families (1,103,690 of 1,398,816) and 71.6%
of its publications (2,492,930 of 3,482,514).** The missing 295,126 families are real: the six core
subclasses are held at 100.0%, so essentially the whole gap is in the 22 adjacent main groups,
where the corpus holds 210,520 of 1,226,600 world publications (17.2%).

**Coverage with text is 10.8%.** 173,725 of 1,607,502 niche families hold a document that can be
read end to end. That is the number that decides whether a claim chart can be grounded, and it is
four times smaller than the "held" number that a coverage statement usually quotes.

## 2. What the niche is made of

| step | families | publications | method |
|---|--:|--:|---|
| named by a CPC symbol in the boundary | | 2,480,143 | `classifications`, one sorted pass |
| after family closure | 1,103,690 | 2,492,930 | every publication sharing a `simple_family_id` |
| plus the one-hop X/Y examiner citation closure | 1,607,502 | 3,057,711 | `citations` where category in SEA/EXA/ISR and origin contains X or Y |

The closures add **503,812 families, 31.3% of the niche.** They are not a refinement of the CPC
rule; they are a third of the answer, and section 5 shows they admit as many gold references as the
CPC core does.

## 3. What "held" hides

Across the whole niche: 1,607,502 families held, 261,479 with claims, 288,171 with a description,
**173,725 with both**. So **89.2% of the niche is a title, an abstract and a classification**, which
retrieves and embeds and grounds nothing. `eval/textstate.py` made the same point on the dev gold
set at a much smaller corpus size; this is that measurement at niche scale.

## 4. By jurisdiction and by decade

A family is counted once per office it was filed in, so the office column sums to more than the
total. `held %` compares only the CPC-boundary part of the niche with the world count for the same
boundary, because the citation closure reaches families the boundary does not name and mixing the
two makes "held" exceed "exists".

| office | in niche | from CPC boundary | world at boundary | held % | complete text | complete % |
|---|--:|--:|--:|--:|--:|--:|
| CN | 795,448 | 468,422 | 609,436 | 76.9% | 44,979 | **5.7%** |
| US | 390,082 | 313,037 | 406,460 | 77.0% | 168,626 | 43.2% |
| DE | 192,867 | 175,234 | 204,421 | 85.7% | 37,915 | 19.7% |
| JP | 152,241 | 77,812 | 136,327 | 57.1% | 44,385 | 29.2% |
| KR | 129,309 | 122,924 | 222,832 | **55.2%** | 18,786 | 14.5% |
| EP | 114,375 | 98,329 | 124,364 | 79.1% | 57,424 | 50.2% |
| WO | 95,414 | 94,798 | 138,691 | 68.4% | 49,862 | 52.3% |
| FR | 67,556 | 61,862 | 71,066 | 87.0% | 9,766 | 14.5% |
| GB | 63,255 | 57,112 | 66,290 | 86.2% | 13,102 | 20.7% |
| CA | 33,622 | 32,513 | 38,869 | 83.6% | 25,684 | 76.4% |
| AU | 26,050 | 25,263 | 30,840 | 81.9% | 15,935 | 61.2% |
| ES | 24,614 | 24,116 | 28,724 | 84.0% | 14,259 | 57.9% |
| AT | 19,718 | 19,360 | 23,281 | 83.2% | 8,786 | 44.6% |
| TW | 17,615 | 15,824 | 53,685 | **29.5%** | 8,970 | 50.9% |
| NL | 14,785 | 14,405 | 17,753 | 81.1% | 3,338 | 22.6% |
| IT | 14,163 | 13,964 | 17,278 | 80.8% | 5,430 | 38.3% |
| BE | 12,999 | 12,736 | 14,898 | 85.5% | 2,158 | 16.6% |
| SE | 12,933 | 12,737 | 14,450 | 88.1% | 3,613 | 27.9% |

**CN is 49.5% of the niche by families and 5.7% of it by readable text.** TW at 29.5% and KR at
55.2% are the two worst-held offices at the boundary.

Counting each family once, not once per office: **590,053 niche families (36.7%) have a member in
`US`, `EP`, `WO` or `DE`, the four-office scope `config.JURISDICTIONS` still names as a fallback.
1,021,287 (63.5%) have a CN, JP, KR or TW member, and 898,377 (55.9%) have ONLY CJK members**, so
for more than half the niche there is no western sibling to substitute and no English text to fall
back on. That is the same 39.9% CJK share the brief quotes, measured at family level inside the
niche, where it is larger.

A family is dated by its earliest held publication; the world column dates it by its earliest
publication at the boundary.

| decade | in niche | from CPC boundary | world at boundary | held % | complete text | complete % |
|---|--:|--:|--:|--:|--:|--:|
| (no date) | 11,724 | 11,448 | 13,687 | 83.6% | 5 | 0.0% |
| 1830s to 1890s | 22,658 | 22,274 | 25,078 | 88.8% | 0 | **0.0%** |
| 1900s | 14,184 | 14,006 | 15,893 | 88.1% | 27 | 0.2% |
| 1910s | 17,706 | 17,212 | 19,945 | 86.3% | 0 | 0.0% |
| 1920s | 22,958 | 21,800 | 25,086 | 86.9% | 8 | 0.0% |
| 1930s | 18,802 | 17,283 | 20,562 | 84.1% | 1 | 0.0% |
| 1940s | 13,987 | 12,709 | 14,364 | 88.5% | 6 | 0.0% |
| 1950s | 33,683 | 30,856 | 34,474 | 89.5% | 143 | 0.4% |
| 1960s | 51,277 | 46,013 | 52,345 | 87.9% | 7,356 | 14.3% |
| 1970s | 62,716 | 50,660 | 57,228 | 88.5% | 15,367 | 24.5% |
| 1980s | 77,769 | 56,073 | 66,507 | 84.3% | 16,821 | 21.6% |
| 1990s | 100,197 | 61,065 | 81,228 | 75.2% | 19,853 | 19.8% |
| 2000s | 158,896 | 93,038 | 136,636 | 68.1% | 30,089 | 18.9% |
| 2010s | 528,399 | 276,453 | 348,579 | 79.3% | 51,892 | 9.8% |
| 2020s | 472,546 | 372,800 | 487,204 | 76.5% | 32,157 | 6.8% |

**Not one family published before 1900 holds readable text, and only 185 published before 1960 do.**
Coverage by ROW is flat across the whole 190 years at 68% to 90%; coverage by TEXT collapses to zero
before 1960 and is only 9.8% and 6.8% in the last two decades, where the volume is. Both tails are
thin, for different reasons: the old art was never digitised as text, the new art is Chinese.

## 5. Gold overlap, as an outcome

Measured after the boundary was frozen, by `ops/niche_gold_check.py`, which imports no boundary
decision from the gold set. 509 distinct gold publications across `eval/attorney_gold.json` (10),
`eval/nguyen_gold.json` (6) and the X/Y eligible rows of `eval/benchmark_gold.csv`.

| | publications | share | reading |
|---|--:|--:|---|
| in the corpus at all | 366 | 71.9% | |
| in the niche manifest | 325 | 63.9% | 88.8% of everything held |
| admitted by the CPC core | 176 | 34.6% | the six subclasses |
| admitted by the family or citation closure | 143 | 28.1% | **as many as the core** |
| admitted by an adjacent group | 6 | 1.2% | |
| held, but outside the niche | 41 | 8.1% | |
| not in the corpus at all | 143 | 28.1% | |
| gold with complete text | 92 | 18.1% | |

**The closures admit 143 gold references and the CPC core admits 176.** A boundary made of CPC alone
would have missed 44.9% of the gold this corpus holds. That is the strongest evidence for the design
in `docs/niche_boundary.md` section 5, and it was produced by a check the boundary never saw.

### The schmalz reach failure is measured, not fixed

Of the attorney's ten filed references: seven are in the niche through the CPC core, three are not
in the corpus at all (`US-9107549-B2`, `US-11206961-B2`, `US-7207874-B2`), and two are held but
outside the niche:

* `US-5269665-A`, Sadler, portable hand-held blower/vacuum with an internal muffler, `F04D25/02` and
  `A47L5/14`
* `US-2966138-A`, the attorney's most comprehensive match, `B25F5/00` and `B23B45/04`

Both sit in branches section 4 of `docs/niche_boundary.md` shows to be below the evidence floor
(`F04D25`, `A47L5`, `B25F5` do not clear 100 attested documents across 80,308 field publications and
150,028 examiner citations), and neither is reached by a one-hop X/Y citation from any of the
2,480,143 boundary publications. **No defensible CPC boundary and no one-hop citation closure
reaches them.** The `retrieval.global_search` tier is the remaining mechanism, and this is the
measurement that justifies funding it.

## 6. What has to be acquired, and from where

`best_source` on each record names the cheapest rung of `src/sources/fulltext.py` that can serve
that family.

| best_source | families | what it means |
|---|--:|---|
| `himmpat` | **900,463** | CN, JP or KR only. Metered at 250/day |
| `gpatents_direct` | 240,482 | every other office. Rate limited, self-disabling, ScrapingBee at scale |
| `pqai` | 220,532 | a US member exists. Free and not quota counted |
| `none_needed` | 168,645 | nothing is missing |
| `epo_ops` | 71,375 | an EP or WO member exists. Free inside 4 GB/week |
| `local:family_member` | 6,005 | a sibling already holds the text here. A join, not a fetch |

**62.6% of the niche's missing text is behind HimmPat's 250 a day.** At that rate 900,463 families
is about 3,600 days, so the CN/JP/KR share of this niche cannot be acquired through that adapter and
needs a different route: this is a hard blocker for workstream C, not a throughput problem.
`gpatents_direct` plus ScrapingBee reaches every office including CN, so the realistic plan for the
Chinese share is ScrapingBee volume, and it should be sized before it is started.

The `pqai` and `epo_ops` rungs together are 291,907 families and both are free, which is where an
acquisition run should start.

### The 32,355 with no local row at all

These are X/Y examiner citations from inside the niche that this corpus holds nothing for, not even
a stub. Source assigned from the office in the publication number, by the same ladder:

| source | publications | offices |
|---|--:|---|
| `epo_ops` | 13,243 | WO 12,898, EP 345 |
| `himmpat` | 11,504 | CN 9,106, JP 2,219 |
| `pqai` | 6,833 | US 6,833 |
| `gpatents_direct` | 775 | DE 292, and a long tail |

**20,076 of the 32,355 (62.0%) are on a free rung**, `epo_ops` or `pqai`, so the part of the niche
this corpus cannot reach at all is mostly cheap to reach. The full list is
`data/manifests/<release>/external_only.txt`. Note that acquiring them means INSERTING publications,
which `docs/corpus_write_policy.md` routes through `corpus_ingest_queue` and a permanent release,
not through the search path.

## 7. The 20.4% with no classification

1,015,875 of 4,975,809 corpus publications carry no CPC symbol. A niche defined purely by CPC is
blind to all of them, so this boundary is not defined purely by CPC.

| | value | method |
|---|--:|---|
| niche families with `cpc == []` | 294,327 | 18.3% of the niche |
| how they got in | family closure and X/Y citation closure | no CPC rule names any of them |
| unclassified share of the corpus | 20.4% | `publications` with no `classifications` row |
| unclassified share of examiner-cited art in this field | **35.9%** | 53,540 of 148,942 |

The examiner-cited population is 1.76x more likely to be unclassified than the corpus average, which
is the measured version of the claim in `docs/shard_and_global_seams.md` that unclassified documents
skew old and foreign and are exactly the population the gold lists are drawn from. The 294,327
unclassified families in this manifest would not exist in a CPC-only manifest.

## 8. What this release does not claim

* **It is not the complete niche.** 295,126 families exist at this boundary that this corpus holds
  no row for, plus 32,355 X/Y-cited publications with no local row. Both lists are derivable:
  `data/manifests/<release>/external_only.txt` holds the second one.
* **It does not cover art outside the boundary.** By construction. Section 5 says what that costs
  against a real attorney's work product.
* **A family id of `-1` is not a family.** DOCDB writes it for "no simple family" and the ingest
  stored it verbatim on 21,862 publications. `corpus_niche.family_key` treats it as absent, so each
  of those is its own family here. `src/retrieval/base.py:157` and `src/retrieval/family.py:93`
  still key on `COALESCE(NULLIF(simple_family_id,''), publication_number)`, which collapses all
  21,862 into one family: in family collapse at most one of them can be returned by any search, and
  `src/retrieval/citations.py` joining `p.simple_family_id = s.simple_family_id` treats all 21,862
  as one document's family neighbours. That is a live retrieval defect, not a manifest one, and it
  belongs to whoever owns retrieval.
* **`cpc` is what this corpus recorded**, not what the office publishes today. The classification
  snapshot moves; a publication reclassified into `H10P` after our ingest still carries its old
  `H01L` symbol here.
