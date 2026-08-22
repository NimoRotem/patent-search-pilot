# The parsed document: the seam between acquisition and embedding

Acquisition (workstream C) and embedding (workstream D) are one streaming pipeline, not two
phases. C writes a parsed document; D parses, chunks and embeds it while C fetches the next. This
file is the only thing the two need to agree on.

Reader: `ops/parsed_sources.py`. Normaliser: `src/parsed_norm.py`. Chunker: `src/stage_chunks.py`.

## Where a document lands

Two addresses, both already read by `ops/parsed_embed.py`. Either is enough; both can be used.

| Address | Written by | Read by |
|---|---|---|
| `gs://nimo-patents-fulltext/parsed/{PUBLICATION}/{PROVIDER}.json` | workstream C, bulk acquisition | `GcsParsedSource` |
| `sources_docstore` (`sql/008`) | the search path's demand fetch | `DocstoreSource` |

**The bucket is `nimo-patents-fulltext`, and the object is named for the PROVIDER, not `doc.json`.**
This document said `gs://nimo-patents-v3/parsed/{PUBLICATION}/doc.json` until 2026-08-22 and that
address has never held a single object. C is configured with `FULLTEXT_GCS_BUCKET`, and
`parsed_sources.PARSED_BUCKET` now reads that same setting rather than a constant of its own, so
the two cannot drift apart again. Measured 2026-08-22: 16,586 objects under
`gs://nimo-patents-fulltext/parsed/`, `serp_self.json` 16,124, `corpus_family.json` 391,
`himmpat.json` 45. Override with `PARSED_BUCKET` and `PARSED_PREFIX`.

One object per provider means a publication can arrive TWICE, as two documents with two source
keys. `parsed_doc_ledger` is keyed on the source object, so both are parsed; `stage_chunks.item_key`
is keyed on the PUBLICATION and the text digest, and `parsed_embed_done` remembers what has already
been staged, so identical text from the second provider costs nothing and genuinely different text
from it is new work.

Any object under the prefix whose name ends `.json` is read; the publication number is taken from
the payload, and from the first path segment after the prefix when the payload does not carry one.
The listing is resumed with the API's own `startOffset`, so the prefix can grow to millions of
objects without the reader getting slower. The watermark advances over every object LISTED,
including the ones that are not JSON and the ones that fail to read, because an object this worker
can never parse must not be able to pin the cursor in front of the objects after it.

**Write the object once and do not rewrite it.** A document is keyed by `gcs:{bucket}/{name}` in
`parsed_doc_ledger`, and a key that has been staged is never parsed or paid for again. Better text
for a publication already staged is a NEW object (`.../doc.2.json`), not an overwrite.

## The payload

```json
{
  "publication_number": "US-2025033224-A1",
  "title": "Vacuum gripping device",
  "abstract": "A vacuum gripping device comprising ...",
  "abstract_lang": "en",
  "abstract_orig": {"text": "Vakuumgreifvorrichtung mit ...", "lang": "de"},
  "lang": "en",
  "n_claims": 20,
  "claims": ["1. A vacuum gripping device ...", "2. The device of claim 1 ..."],
  "description": ["[0001] The invention relates to ...", "[0002] ..."],
  "figure_captions": [{"figure_no": "1", "caption": "FIG. 1 is a perspective view ..."}],
  "source": "epo:ops",
  "fetched_at": "2026-08-22T15:00:00Z"
}
```

Every field except `publication_number` is optional. `claims` and `description` may each be a list
of strings, a list of `{text: ...}` objects, or one string; all three are flattened the same way.
`claims_text` and `description_text` are accepted as aliases.

### `n_claims` is the field that matters

It is the count the SOURCE says the document has, taken from the bibliographic record or the
front page, and it is the only count that is independent of how the text was split. **Send it
whenever you have it.** `num_claims`, `claim_count`, `claims_count` and `number_of_claims` are
accepted, at the top level or inside `meta`, a `Claims (15)` header at the top of the claims text
is read, and a front-page string in `cover` or `front_page` ("20 Claims, 36 Drawing Sheets") is
parsed as a last resort.

**Do not strip the `Claims (N)` header.** Measured 2026-08-22 across the 542 documents then in
`sources_docstore`: every Google Patents record carries it, and for the machine-translated DE
records the claims that follow are separated by NEWLINES AND NOTHING ELSE, with no numbers at all.
Both numeric splitters return ONE claim for a fifteen-claim document, which is the same silent
loss as the glued blob wearing different clothes. That header is the only thing that catches it,
and it recovered 84 documents in that sample. `strip_source_preamble()` removes it, and the
"Translated from German" line, only from the first three lines and only after the count has been
read.

### How the count arbitrates

Four deterministic readings of the claims text are produced: `patent_doc.split_claims`,
`patent_text.split_claims`, the source's own item list, and one claim per line. When a declared
count exists, **the reading that matches it wins**, tried in that order; when none matches, the
document's claims are refused. When no count was declared, the highest yield wins, because a
source that split correctly is never re-split into fewer claims and a source that glued nineteen
claims together is always corrected.

## What the normaliser asserts, and why it rejects

Measured on US 2025/0033224 A1, the application a patent attorney actually attacked: the record
came back as TWO items, claims 1 to 19 glued into one 17,309-character blob and claim 20 on its
own. Positional numbering made that a "claim 1" that was nineteen claims and a "claim 2" that was
claim 20, and the limitation splitter only ever sees the first 4,000 characters of a claim, so
claims 5 through 19 were invisible to the whole pipeline. Nothing raised. The ledger had two rows
and the subject was lost.

So the shape you send is recorded and then verified, never trusted. The claims text is re-split by
two independent splitters (`patent_doc.split_claims`, which anchors each boundary to the claim
number expected next and is CJK aware, and `patent_text.split_claims`, which splits on line
leading "N."), the highest yield wins, and then:

| Code | The assertion |
|---|---|
| `CLAIM_COUNT_MISMATCH` | the parsed count equals `n_claims`, when `n_claims` was sent |
| `CLAIM_NUMBERING` | the claim numbers are a contiguous `1..N` |
| `CLAIMS_GLUED` | no claim body still holds a run of three or more later claim numbers starting at the next expected one and implying claims we do not have |
| `CLAIM_TRUNCATED` | no claim is exactly 4,000, 8,000 or 12,000 characters long, which is what a silent clip looks like from outside |
| `EMPTY_CLAIMS` | a document that declared claims carried some |
| `NO_TEXT` | the document has claims, description or abstract |

A failure is never a repair. **No claim that failed an assertion is ever staged**, which is the
harm this exists to prevent: a "claim 1" that is nineteen claims is what feeds the limitation
splitter, and the limitation is the unit of work in a Type B search.

What is thrown away depends on what else the document carries, and both outcomes are recorded:

| Ledger `state` | When | What is staged |
|---|---|---|
| `staged` | every assertion passed | everything |
| `partial` | the claims failed, the abstract or description did not | abstract, description, figure captions. `code` and `reason` name the defect |
| `rejected` | nothing readable survived | nothing |

`partial` exists because of a measurement: of the 542 documents in `sources_docstore` on
2026-08-22, 111 are mostly 1970s German publications whose OCR is beyond any splitter ("ι 2
kubaren suction head consists of two"), and discarding those records whole would throw away a
good abstract and a good description along with the unreadable claims.

`SELECT state, code, count(*) FROM parsed_doc_ledger GROUP BY 1, 2` is the report back to
acquisition. A rising `CLAIM_COUNT_MISMATCH` means a source's claim splitting has changed.

### What the assertion is actually worth, measured on C's output

300 documents drawn at random from `gs://nimo-patents-fulltext/parsed/` on 2026-08-22, parsed
without staging:

| | count | share |
|---|---|---|
| documents that declared a claim count | 292 | 97.3% |
| of those, whose claims had to be RE-SPLIT to match it | 262 | 87.3% |
| `staged`, every assertion passed | 271 | 90.3% |
| `partial`, claims refused, abstract and description kept | 29 | 9.7% |
| `rejected`, nothing readable | 0 | 0% |

So on nearly nine documents in ten the shape the source handed over was **wrong**, and the count
in the `Claims (N)` header is what corrected it. Almost none of those documents carry an
`n_claims` field: the header is the only declared count these records have, which is why
`strip_source_preamble` reads it before removing it.

The 29 refusals are 28 `CLAIM_COUNT_MISMATCH` and 1 `CLAIMS_GLUED`, and they are overwhelmingly
machine-translated KR and DE records whose claims are separated by newlines that also fall inside
claims, so no deterministic reading produces the declared count. A reading that splits on a
newline following a sentence-terminating character matches the declared count on 12 of the 32
defective documents in that sample, and it is **deliberately not used**: matching a count is not
the same as finding the right boundaries, and a claim that is really two claims is precisely the
harm this module exists to prevent. Those 29 keep their abstract, description and figure captions
and are recorded `partial` with the code, so the gap is visible in one query rather than invisible
in a search result.

## What comes out

Chunks in `chunks_stage_v3`, kind for kind and clip for clip as `src/chunker.py` built the live
corpus: `whole` (title plus abstract, 2,000 characters), `abstract` (8,000, plus the original
language abstract as a second row when it differs), `claim_own` and `claim_resolved` (a dependent
claim carries its parent's limitations; an independent claim emits only its own), `paragraph`
(`patent_text.split_paragraphs`, the same function that built the corpus's `paragraphs` rows) and
`figure_caption`.

`ref_id` is always NULL and `coord` always carries `pub`. Both are deliberate: `ref_id IS NOT NULL`
is how `ops/desc_backfill.py`'s rows are told apart from these, and a publication the corpus does
not hold has no id that resolves to a number.

### `coord` also carries the provenance

`chunks_stage_v3` has no provenance column and must not grow one: it is shared with the running
description backfill. `coord` is jsonb and is the row's own record of where its words came from.

| key | when | meaning |
|---|---|---|
| `pub` | always | the publication number, in the CORPUS spelling where the corpus knows it |
| `src` | when the record names a source | `serp_self`, `himmpat`, `corpus:family`, `docstore` |
| `donor_text` | family-donor records only | the words on this row are not this publication's |
| `donor` | family-donor records only | the publication whose words they are |
| `family` | family-donor records only | the DOCDB simple family both belong to |

Workstream C fills a publication that has no text of its own from a family sibling that does, and
marks the record `source="corpus:family"` with the donor's number. The disclosure is the same
document; the WORDS are the donor's. Flattening that away would put a staged chunk under a
publication whose text nobody has ever read with nothing on the row to say so, and there is no way
to notice afterwards. Measured 2026-08-22: 391 of C's 16,586 objects are `corpus_family.json`.

`text_is_family_donor` arrives as a JSON boolean from some writers and as the STRING `"True"` from
others, a Python bool that went through `str()` on its way into JSON. `bool("False")` is True and
`"True" is True` is False, so `parsed_norm._truthy` reads it and neither of the obvious tests does.

### The publication number is spelled two ways and they are different strings

C and `sources_docstore` spell a publication compactly, `DE10023344C2`; the corpus spells it
hyphenated, `DE-10023344-C2`. An equality join on the raw string matches NOTHING, and the
consequence is not an empty result. Every fetched document is handed a negative surrogate id, so
workstream F cannot join a staged row back to a real publication, and
`publications_with_paragraphs()` (the query that keeps this pipeline off `patents-desc-backfill`'s
population) is asked about ids that do not exist and answers "none of them".

`src/corpus_pub.py` resolves it against the functional index `ix_pub_number_norm`, which is exactly
the compact form, plus `pubnorm.mongo_candidates` for the US pre-grant ladder where BigQuery drops
a leading zero from the serial. Measured 2026-08-22: **all 16,586 of C's publications resolve**,
in 1.4 seconds. The ledger records both spellings, `publication_number` for the join and
`fetched_number` for the object name.

There is no `title` chunk kind. The live schema's kinds are
`whole|abstract|claim_own|claim_resolved|paragraph|figure_caption` (`sql/001_schema.sql`, line
123) and the title is carried inside `whole`. Adding a seventh kind here would put rows in staging
that the corpus has never held and that nothing downstream knows how to weight.

## Publications the corpus does not hold

`chunks_stage_v3.publication_id` is `NOT NULL` and `publications` may not be written by anything
in this pipeline. A fetched document that is not in the corpus is given a surrogate id from
`parsed_stage_pub` and staged under its NEGATIVE value, so such a row can never be joined to a
real publication by accident. `coord->>'pub'` carries the publication number on every row.

## What this pipeline will not touch

A publication that already has rows in `paragraphs` has its description embedded by
`patents-desc-backfill`, and this pipeline stages no `paragraph` chunk for it. The two jobs are
disjoint by query, not by care: see `publications_with_paragraphs()` in `ops/parsed_embed.py`.
Claims, abstract and figure captions for such a publication are still staged here, because the
description backfill never writes those kinds.
