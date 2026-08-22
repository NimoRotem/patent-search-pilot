# The niche corpus manifest: what it is and how to read it while it is being written

Owner: workstream B (niche corpus). Primary consumer: workstream C (full text acquisition).
The executable copy of everything below is `src/corpus_niche.py` and `ops/niche_enumerate.py`.
If this file and the code disagree, the code is right and this file is a bug.

**This contract is written so C can start against a partial manifest.** The enumeration emits
finished, immutable batches as it goes; it does not hold anything back until the end.

---

## 1. Where it is

```
<repo>/data/manifests/<release_id>/
    boundary.json        the frozen boundary definition this release was built from
    index.json           the append log. THE ONLY THING A READER SHOULD TRUST
    part-00000.jsonl     one family record per line, newline delimited JSON
    part-00001.jsonl
    ...
    COMPLETE             written last, and only once. Presence means the release is closed
```

On the patents VM the absolute path for the current release is

```
/home/nimrod_rotem/v3/B-corpus-manifest/data/manifests/<release_id>/
```

`release_id` is `niche-YYYY-MM-DD` (with `-N` appended if a second release is cut the same day).
`data/manifests/LATEST` is a text file holding the newest `release_id`; read it rather than
globbing, because a re-run creates a new directory and never edits an old one.

A mirror of the same records in Postgres is optional and off by default: `sql/010_corpus_release.sql`
defines `corpus_niche_release` and `corpus_niche_family`, and `ops/niche_enumerate.py --emit db`
fills them. Files are canonical. The table exists for the release workstream, not for C.

## 2. The record

One JSON object per line. Exactly these fields, always all of them, `null` never used where a
list or a bool is declared:

| field | type | meaning |
|---|---|---|
| `family_id` | string | the family key. `publications.simple_family_id` when it is set, otherwise the publication number of the only member. Unique within a release. |
| `publications` | list of string | every publication number in the family that this corpus holds, sorted |
| `cpc` | list of string | every distinct classification symbol carried by any member, sorted. Includes IPC symbols, Y tagging codes and 2000-series indexing codes: they are excluded from the *boundary*, not from the record |
| `title` | string | the representative member's title, `""` when none is held |
| `abstract` | string | the representative member's abstract, `""` when none is held |
| `has_claims` | bool | at least one member holds claim text of at least `MIN_CLAIMS_CHARS` (200) characters |
| `has_description` | bool | at least one member holds description text of at least `MIN_DESC_CHARS` (800) characters |
| `has_complete_text` | bool | one single member holds both, at or above those floors. Not "one member has claims and a different member has a description" |
| `best_source` | string or null | where the missing text should be fetched from. `null` when nothing is missing |
| `missing_fields` | list of string | subset of `["title", "abstract", "claims", "description"]`, sorted. Empty when nothing is missing |

`MIN_CLAIMS_CHARS` and `MIN_DESC_CHARS` are not new numbers: they are the floors
`src/sources/fulltext.py` already uses to decide whether a document has been read in full, imported
from there so the manifest and the fetcher cannot disagree about what "held" means.

### `best_source`

Values, and they are the rungs of the ladder in `src/sources/fulltext.py` in the order that module
tries them, so a value is directly actionable:

| value | when | what C should do |
|---|---|---|
| `local:family_member` | another member of the same family already holds complete text locally | nothing to fetch. This is a join, not an acquisition |
| `pqai` | a member is US | PQAI `/patents/{pn}`, free and not quota counted |
| `epo_ops` | a member is EP or WO | EPO OPS full text, free inside the 4 GB/week tier |
| `himmpat` | a member is CN, JP or KR | HimmPat English translation, metered, 250/day |
| `gpatents_direct` | anything else | Google Patents direct or ScrapingBee, every jurisdiction |
| `null` | `has_complete_text` is true and nothing is missing | nothing |

The value names the CHEAPEST rung that can serve this family, not the only one. C is free to fall
through the rest of the ladder; it should not fall *up*.

## 3. How to read it while it is still being written

Five rules. Following them makes a partial read safe; ignoring them makes it a race.

1. **Only `index.json` names a readable part.** A `part-*.jsonl` present on disk but absent from
   `index.json` is being written right now. Do not open it.
2. **A part named in `index.json` is finished and immutable.** It has been fully written, flushed
   and checksummed before its entry appeared. Its `sha256` will never change.
3. **`index.json` is replaced atomically** (write to `.tmp`, `os.replace`), so a reader always sees
   a whole, valid list, never a half-written one.
4. **Parts are disjoint and ordered.** Records are sorted by `family_id` across the whole release,
   each part covers a contiguous range, and `first_family_id` / `last_family_id` are recorded. To
   resume, remember the last part name you consumed and take the ones after it.
5. **A release is closed when `state == "complete"` and the `COMPLETE` file exists.** Until then,
   more parts may appear. A re-run never edits a closed release; it cuts a new `release_id`.

`index.json`:

```json
{
  "release_id": "niche-2026-08-22",
  "manifest_version": 1,
  "state": "in_progress",
  "boundary_sha256": "…",
  "started_at": "2026-08-22T15:40:11Z",
  "updated_at": "2026-08-22T15:52:03Z",
  "parts": [
    {"name": "part-00000.jsonl", "families": 50000, "bytes": 41203311,
     "sha256": "…", "first_family_id": "10000123", "last_family_id": "10493887",
     "written_at": "2026-08-22T15:41:52Z"}
  ],
  "totals": {"families": 50000}
}
```

### The reader, in full

```python
import json, os, time

def read_manifest(release_dir, after_part=None):
    """Yield (part_name, record). Safe to call repeatedly on a growing release."""
    idx = json.load(open(os.path.join(release_dir, "index.json")))
    started = after_part is None
    for p in idx["parts"]:
        if not started:
            started = (p["name"] == after_part)
            continue
        with open(os.path.join(release_dir, p["name"])) as fh:
            for line in fh:
                yield p["name"], json.loads(line)

def follow(release_dir, poll=30):
    last = None
    while True:
        for name, rec in read_manifest(release_dir, after_part=last):
            last = name
            yield rec
        idx = json.load(open(os.path.join(release_dir, "index.json")))
        if idx["state"] == "complete":
            return
        time.sleep(poll)
```

`src/corpus_niche.py` exports `read_manifest`, `follow` and `latest_release_dir` so C does not have
to copy this.

## 4. What C should filter on

The manifest is the whole niche, not a work queue. The rows that are work for C are

```python
rec["best_source"] and rec["best_source"] != "local:family_member"
```

and within those, `missing_fields` says what to ask for. Families whose `best_source` is
`local:family_member` are a retrieval-wiring problem, not an acquisition one: the text is already
in this database under a sibling publication number.

## 5. What this contract does NOT promise

* **It is not a list of everything that exists.** It is one record per family the corpus holds, at
  the boundary in `boundary.json`. The families in the niche that the corpus does not hold at all
  are counted in `docs/corpus_completeness.md` and are not rows here, because there is no local
  publication to describe.
* **`cpc` is what this corpus recorded**, not what the office publishes today. 20.4% of the corpus
  carries no classification at all and those families still appear, with `cpc: []`, whenever they
  entered the niche through the family or citation closure.
* **No ordering by importance.** `family_id` order is an implementation choice that makes resuming
  cheap. It carries no priority.
