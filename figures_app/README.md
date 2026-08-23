# Patent figure compiler

Served at <https://nimo.iptorch.com/figures>. Give it a patent as a PDF or a link; it returns
the figure set the patent describes, drawn from the description, validated against it, and shown
beside the drawings the applicant actually filed.

It is a compiler, not an image generator:

```
patent language -> semantic representation -> constrained visual program -> validated vector figure
```

No part of any figure is produced by an image model. Every numeral, line, arrow and leader is
drawn by code from a typed specification, and every element of that specification points at the
paragraph of the patent it came from.

## Where it sits

A separate process from the prior-art search application at the root of the same domain, on
purpose. A deep search on that service runs for hours and does not survive a restart, so a
component still under development has no business sharing its process. What it does share:

| Shared | How |
|---|---|
| Sign-in | The search app's signed session cookie, verified here with the same key and checked against `app_users`. No password of its own. |
| Document fetching and parsing | `patent_pdf`, `drawings`, `enrich_display`, `ingest_input` from `~/patent-search-pilot/src`, imported over `PILOT_ROOT`. |
| The publication cache | Reads `~/patent-search-pilot/data/{pdfs,figures}`; downloads a facsimile only when it is absent. |

Its own: port 8637, supervisor program `patent-figures`, venv at `figures_app/.venv`, job storage
at `PFC_DATA_DIR` (default `~/patent-figures-data`).

## The pipeline

A fixed state machine. Every stage consumes a typed object and returns one; nothing crosses a
stage boundary as prose.

```
INGEST -> PARSE -> EXTRACT -> RECONCILE -> PLAN -> SPEC
       -> LAYOUT -> RENDER -> VALIDATE -> VISION -> CORRECT -> FINAL -> EXPORT
```

| Stage | Module | What it does |
|---|---|---|
| Ingest | `pfc/ingest.py` | PDF or link to one document. Prefers the facsimile PDF over the publication record, because the record usually carries no description and reference numerals live there. |
| Parse | `pfc/parse.py` | Sections and paragraphs with stable ids (`p0001`). Slices the cover page off, so a citation table's fifty name-then-number pairs never become components. |
| Numerals | `pfc/numerals.py` | The reference registry, deterministically. Tolerates drafting variants; refuses to resolve a genuine collision. |
| Extract | `pfc/extract.py` | The one model task: which of a closed list of predicates a sentence states. Three filters before anything enters the graph. |
| Ground | `pfc/ground.py` | A second reader, in a fresh context, shown one paragraph and one sentence. |
| Plan | `pfc/plan.py` | The figure set from the brief description of the drawings, with its numbering. |
| Spec | `pfc/spec.py` | What one figure shows. Bound to the paragraphs that name that figure. |
| Layout | `pfc/layout/` | Layered for diagrams, top-to-bottom for flowcharts, containment-nested for physical views. Leaders by scored search. |
| Render | `pfc/render/` | One renderer per figure family. SVG is the artifact; PDF and PNG are conversions of it. |
| Validate | `pfc/validate/` | 30-odd rules over grounding, references, semantics, geometry and office rules. |
| Vision | `pfc/vision.py` | The sheet read back independently, then compared in ordinary code. |
| Correct | `pfc/correct.py` | The narrowest repair that owns the defect. Three attempts, then blocked with a diagnosis. |

## Figure states

| State | Meaning |
|---|---|
| `VALIDATED` | Every blocking check passed, including the independent reading. |
| `NEEDS_TEXT_UPDATE` | The drawing is fine; the patent is not. Normally a part the caption names and the description never numbered. The compiler will not invent a numeral. |
| `BLOCKED` | Could not be made correct. The report names the defect and the paragraphs behind it. |

## Two places this departs from the written specification

**The vision verifier is not primed with the expected numerals.** The specification says to hand
it the list of expected reference numerals with the image. Doing that primes it: a reader told to
expect 110, 120 and 130 reports 110, 120 and 130, and the "reference numeral recall = 100%"
threshold then measures nothing. It gets the image and the output schema. `prime_with_expected`
in `pfc/vision.py` turns the specified behaviour back on for anyone who wants to compare; it is
off by default.

**Connection-line crossings are a warning, not a blocker.** Real patent drawings cross lines
routinely and examiners accept them. Blocking would refuse good figures. Leader crossings, which
genuinely make it ambiguous which numeral belongs to which part, do block.

Drawing profiles are versioned YAML in `drawing_profiles/`; prompts are versioned files in
`prompts/` and every artifact records which content hash produced it.

## Running it

```bash
cd ~/patent-figures/figures_app
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh                       # :8637, behind nginx at /figures

.venv/bin/python -m pytest tests/ -q      # 65 tests, no network, no model calls
.venv/bin/python -m eval.run --dataset test_patents/golden
```

Environment: `PILOT_ROOT`, `PFC_DATA_DIR`, `PFC_MAX_CONCURRENT`, `PFC_RETENTION_DAYS`,
`PFC_TEXT_MODEL`, `PFC_VISION_MODEL`, `PATENTS_LOGIN_URL`.

## API

```
POST   /v1/jobs                    file= | url=, jurisdiction, verification_level  -> {job_id}
GET    /v1/jobs/{id}               state, stage, per-figure counts
GET    /v1/jobs/{id}/figures       the figure index with preview urls
GET    /v1/jobs/{id}/validation    the full validation report
GET    /v1/jobs/{id}/graph         the semantic model, with its evidence
GET    /v1/jobs/{id}/manifest      artifacts and provenance
GET    /v1/jobs/{id}/download      everything, as a zip
DELETE /v1/jobs/{id}               delete the job and its artifacts
```

Jobs are owned by the account that created them and are invisible to any other; artifacts are
served only through the owning session. Retention defaults to 30 days.

## What it will not do

1. Use an image model anywhere in the pipeline.
2. Let a model emit an SVG string or a coordinate.
3. Invent a reference numeral, or move an existing one.
4. Draw a component's shape because it recognises the component's name.
5. Draw two embodiments as though they held at once.
6. Trace, or read geometry from, the drawings the applicant filed.
7. Mark a figure `VALIDATED` while any blocking check fails.
8. Repair a semantic defect by redrawing until it stops being reported.
