# The parsed document: the seam between acquisition and embedding

Acquisition (workstream C) and embedding (workstream D) are one streaming pipeline, not two
phases. C writes a parsed document; D parses, chunks and embeds it while C fetches the next. This
file is the only thing the two need to agree on.

Reader: `ops/parsed_sources.py`. Normaliser: `src/parsed_norm.py`. Chunker: `src/stage_chunks.py`.

## Where a document lands

Two addresses, both already read by `ops/parsed_embed.py`. Either is enough; both can be used.

| Address | Written by | Read by |
|---|---|---|
| `gs://nimo-patents-v3/parsed/{PUBLICATION}/doc.json` | workstream C, bulk acquisition | `GcsParsedSource` |
| `sources_docstore` (`sql/008`) | the search path's demand fetch | `DocstoreSource` |

The bucket is `nimo-patents-v3`, region `us-central1`, created 2026-08-22. Override with
`PARSED_BUCKET` and `PARSED_PREFIX`. Any object under the prefix whose name ends `.json` is read;
the publication number is taken from the payload, and from the first path segment after the prefix
when the payload does not carry one. The listing is resumed with the API's own `startOffset`, so
the prefix can grow to millions of objects without the reader getting slower.

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
