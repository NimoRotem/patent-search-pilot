# Patent figure compiler

The figure compiler is the deterministic filing-artifact path inside each drafting project. It is
not the raster concept-sketch path in `draft_figures.py`, and no image-generation model produces
its final SVG or PDF.

## Boundary and current scope

The MVP compiles graph, flow, network, control, and basic relationship schematics from facts that
already exist in an immutable draft version. It also produces simple component schematics for a
human to review. Exact physical contours and unrestricted text-to-CAD are intentionally outside
this vertical slice; those need supplied geometry, CAD, or an approved tracing workflow rather
than inferred shapes.

Every visible entity, relation, reference sign, and leader retains its source-span IDs. An
unsupported visible object, an unresolved material reference conflict, an uncovered drawable
claim limitation, a text/drawing numeral mismatch, a semantic-render mismatch, or content outside
the usable sheet surface blocks final approval and export.

## Workflow

```text
immutable draft version
  -> ingest snapshot
  -> typed PIR + source spans
  -> canonical registry and conflict reconciliation  [human approval]
  -> claim decomposition and coverage
  -> minimum figure manifest                         [human approval]
  -> typed Figure DSL
  -> deterministic SVG, numerals, leaders, sheet composition
  -> formal + semantic + disclosure + cross-document validation
  -> typed repair patch -> new artifact version
  -> final review                                    [human approval]
  -> SVG/PDF export
```

The run stages are persisted in `app_figure_compiler_runs`. Inputs and every intermediate/final
value are content-hashed and versioned in `app_figure_compiler_artifacts`; manual repairs are kept
in `app_figure_compiler_patches`. A database trigger rejects updates or deletion of an approved
artifact. Starting from a newer draft creates a new active run and leaves the old approvals intact.

## Rulesets

Rules live in JSON rather than renderer code:

- `uspto-letter-2026.1`: 216 x 279 mm sheet; 25/25/15/10 mm top/left/right/bottom margins.
- `pct-a4-2026.1`: 210 x 297 mm sheet; 170 x 262 mm usable surface; the same minimum drawing
  margins and a 3.2 mm minimum drawing-character height.

The profiles record the authority URL and the date checked. The sources are the current USPTO
MPEP drawing guidance / 37 CFR 1.84 and WIPO PCT Rule 11. Changing a profile means adding a new
version; an approved package remains pinned to the old one.

## Account and web boundary

Every service method receives a `drafting.Principal` and re-runs the drafting project's ownership
check. Browser mutations require the existing session CSRF token. Export reads only an approved
package. The studio uses a hash-routed `#/compiler` pane, so compilation never leaves the draft.

API surface:

- `GET /api/drafts/<id>/figure-compiler`
- `POST /drafts/<id>/figure-compiler/start`
- `POST /drafts/<id>/figure-compiler/model/resolve`
- `POST /drafts/<id>/figure-compiler/model/approve`
- `POST /drafts/<id>/figure-compiler/manifest/approve`
- `POST /drafts/<id>/figure-compiler/compile`
- `POST /drafts/<id>/figure-compiler/patch`
- `POST /drafts/<id>/figure-compiler/approve`
- `GET /drafts/<id>/figure-compiler/export.svg?sheet=N`
- `GET /drafts/<id>/figure-compiler/export.pdf`

## Testing

`tests/test_figure_compiler.py` covers typed contracts, stable provenance, conflict blocking and
resolution, claim coverage, approval gates, semantic SVG metadata, numeral consistency in both
directions, usable-surface compliance, immutable repairs, deterministic PDF, and exact golden SVG
fixtures. Service and Flask integration suites cover version/audit behavior, ownership, CSRF, all
three gates, and export locking.

Run the focused suite with:

```bash
.venv/bin/pytest -q \
  tests/test_figure_compiler.py \
  tests/test_figure_compiler_service.py \
  tests/test_figure_compiler_web.py
```
