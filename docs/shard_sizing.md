# Does the eight-way split fit? Re-derived 2026-08-22, workstream O

**Yes. Every domain shard carries about 4.76M chunks and 18.4 GiB of index against a 96.9 GiB
resident budget, so eight shards fit the post-backfill corpus with roughly 5x headroom.**

That answer is new. The plan the fleet was being built against reported `fits: true` without
having tested anything, and the reason is worth writing down because the same mistake is easy to
repeat.

---

## 1. What was wrong with the first plan

`ops/release_measurements/plan.json` (built 2026-08-22 15:28) reports:

```
capacity  25,911,302 chunks per shard
mass      domain_01..06: 142,148   domain_07..08: 142,147
fits      true
```

Six identical values in a row look like a placeholder or a cap. **They are neither.** They are a
real longest-processing-time bin-pack, and it converged to within one chunk because the mass
distribution has a very long thin tail: of the 602 CPC subclasses in that file, 75 hold 10 chunks
or fewer and 177 hold 100 or fewer. Items that small are fine-grained filler, and LPT with filler
lands on the exact average. `1,137,182 / 8 = 142,147.75`, and that is what the file says.

The defect is elsewhere, and it is worse. **The masses are raw counts from a 0.4% `TABLESAMPLE`,
and they were compared against a capacity computed for the whole corpus.**

| | plan.json | the corpus |
|---|---:|---:|
| chunk mass, all domains | 1,137,182 | 36,548,679 |
| `unclassified` chunk mass | 5,930 | 2,528,746 |
| publications behind the mass | 118,016 | 4,984,254 |
| CPC subclasses seen | 602 | 669 |

So `fits: true` was `142,148 <= 25,911,302`: a sample's per-shard mass against a corpus's per-shard
capacity, understated 32-fold overall and **426-fold for the unclassified population**, which is
the single largest home domain in the corpus. Nothing was tested. That it came out true anyway is
luck, not measurement.

## 2. How the real mass was derived

No sampling, no scaling. Every family, on the builder database.

1. `mirror_light_tables` had already copied all **4,984,254** publications and their **53,392,859**
   classification rows into the `relbuild` cluster. Verified complete against the live
   `max(id)` watermark of 6,436,391.
2. `assign_family_homes` placed all **3,431,375** families, 240 s, entirely on the mirror. One
   home domain per family, from `corpus.assign`, which imports `shard_router.domain_of` rather
   than reimplementing it.
3. `fill_family_chunk_counts` counted `chunks` **and** `chunks_stage_v3` per publication against
   the live corpus, in key ranges of 5,000 publication ids. One `Index Only Scan using
   ix_chunks_pub` per batch, measured at 0.13 s for 5,000 publications and 244,173 chunks. Whole
   corpus: **54 s of reads**, 110 s including the merge. No sequential scan, `EXPLAIN` checked
   before the first batch and the pass refuses to run if the plan is one.
4. `fill_family_backlog_counts` counted, per publication, the rows in `paragraphs` that have no
   chunk in `chunks` or in `chunks_stage_v3`. 924 s. The gap is clamped per publication, not per
   family: a family with one over-chunked publication and one un-chunked one would otherwise net
   its own backlog to zero.

## 3. The numbers

```
chunks in `chunks` today                       27,623,460
chunks staged in `chunks_stage_v3`              8,992,335   all kind='paragraph'
  -> mass attributable to mirrored families     36,548,679
description paragraphs in `paragraphs`         14,379,681
  ... already chunked in `chunks`                1,690,534
  ... already staged                             8,992,335
  -> description backlog, no chunk anywhere      3,576,933   over 173,257 families
POST-BACKFILL CORPUS                           40,125,612
```

Split hot-tier-first, which is the order that keeps both the subgroup definition of the niche and
the subclass definition of a domain true:

```
hot tier (the eight seed subgroups)   35,119 families    2,024,262 chunks
eight cold domain shards              38,101,350 chunks -> 4,762,670 each
```

Against one shard's 124 GiB of RAM and 250 GB of disk:

```
per-chunk index cost      4,142 B   = 3,654 HNSW + 44 pkey + 22 pub + 16 kind + 406 Tantivy
per-chunk disk cost       9,691 B   = 1,441 heap + 4,108 toast + 4,142 index
resident index budget      96.9 GiB = (124 - 6 OS - 4 backends) x 0.85 heap-cache share
usable disk               230.0 GiB = 250 - 20 for WAL, checkpoints and a staged snapshot

per shard, post-backfill   4,762,670 chunks -> index 18.4 GiB, disk 43.0 GiB
per-shard ceiling         25,119,648 chunks (RAM-bound)
headroom                  20,356,979 chunks per shard, 5.3x
```

**Verdict: eight cold shards plus a hot tier fit, and are over-provisioned for this corpus.**
The 124 GiB machine is sized for something five times bigger than what it will hold.

## 4. Where eight stops being enough

Eight cold shards stop fitting at **200,957,184 chunks**, which at the measured 56.1 chunks per
fully texted publication is **3,582,124 fully texted publications**, or 72% of the 4,984,254 in the
corpus.

So the one scenario that does not fit is *every publication reaching full text*:

```
4,984,254 x 56.1 = 279,616,649 chunks
  over 9 shards (hot + 8), fp32:   31.07M each, index 119.8 GiB  DOES NOT FIT, needs 12
  over 9 shards, halfvec:                       index  67.1 GiB, disk 227.7 GiB  fits
```

That is not the current trajectory and nothing should be re-planned for it today. It is the line
to watch: **when full-text coverage passes about 72% of the corpus, either the fleet grows past
eight or the vectors become `halfvec`.** halfvec halves the HNSW at 1,831 B/chunk and moves the
binding constraint from RAM to disk. It has not been measured for recall here, and swapping the
index type without measuring recall is the sort of change `eval/RESULTS.md` exists to prevent.

## 5. What the arithmetic must keep reproducing

`sizing.py` is checked by `tests/test_corpus_assignment.py`. The load-bearing one:

```python
sizing.plan_verdict(27_622_168, 1, ram_gib=62.0)["fits"] is False
```

Today's box, today's corpus, one shard. If that ever comes out True the constants have drifted and
the arithmetic no longer describes the defect V3 exists to fix.

## 6. A fresh index is smaller than the live one

MEASURED on release `hot_v2`: 511,783 chunks, HNSW built once over a static partition, in 99.7 s
with `maintenance_work_mem = 4GB`. The live `ix_chunks_hnsw` costs 3,664 B/chunk; `hot_v1` measured
3,202 B/chunk freshly built, 12.6% less, at the same `m` and `ef_construction`. The difference is
that the live graph was grown by insert while being queried and this one was built once. The
planning constant deliberately stays at the larger figure: a capacity estimate should not assume
the best case.

The Tantivy index measured 155,221,187 bytes for 511,783 documents on `hot_v2`, **303.3 B/doc**,
text indexed and not stored. `LEXICAL_BYTES_PER_CHUNK` is 406, which remains the safe direction.

## 7. The builder box cannot build a full shard

8 vCPU, 31 GiB RAM. pgvector builds the HNSW graph inside `maintenance_work_mem` and falls back to
a much slower two-phase on-disk build when it does not fit. One domain shard's 4.76M chunks need
**16.2 GiB** of `maintenance_work_mem` for an in-memory build. That is not available here with
`patent-results` on the same host. The domain shards have to be built on the shard VMs, or on a
box sized for them. `sizing.build_ram_required_gib()` prints the requirement.
