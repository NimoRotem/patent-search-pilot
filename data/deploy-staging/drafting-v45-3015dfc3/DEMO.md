# 30-second demo

**Open:** http://127.0.0.1:8631  (localhost on the pilot box — view via `rotem.ai/browsertool`).

The home page has **two entry points**: 11 one-click frozen example searches (instant) and a
**free-text invention box** with example prompts.

## Click these 3 gold searches first (each opens instantly)

1. **`grabo_gripper_novelty`** — GRABO's own vacuum gripper vs its examiner-cited prior art.
   The showcase: rich claim chart, 22 US references with real drawings + PDFs.
2. **`schmalz_vacuum_clamp`** — a German (Schmalz) inventive-step search. Shows it generalizes:
   German query, mixed US/DE references, **real German patent drawings**, cross-lingual rationale.
3. **`nl_porous_surface_gripper`** — a pure natural-language query (no subject patent), to show the
   agent decomposing a plain-English invention into elements.

## What to look for in a report (top → bottom)

- **Summary bar** — mode, subject patent, families surfaced, channels, LLM calls.
- **Element × Reference claim chart** — the headline. Rows = invention elements, columns = the
  strongest references; each cell shows the evidence score + the claim/¶/figure coordinate + the
  prior-art basis (◆ public / 🔒 secret). Click a cell to jump to that reference.
- **Inventive-step combination view** — primary reference + secondaries ("supplies …") + which
  elements are *not* found in the corpus (the apparently-novel features).
- **Reference cards** — click one to expand: **real patent drawings** (click for a lightbox),
  **PDF facsimile**, section tabs (abstract / claims / description / figures / citations) with the
  **matched claim/paragraph highlighted**, an AI "why relevant" line, and a **citation graph**
  (backward / forward / similar / more-like-this).
- **Triage** — flag each reference Relevant / Maybe / Not + a note; filter to relevant-only.

## The litigation-grade export (the money feature)

1. On any report, tick the checkboxes on ~5 references. An export bar appears at the bottom.
2. Click **Export PDF** and **Export DOCX** → a clean prior-art report downloads: cover, executive
   summary, the claim chart, per-reference biblio + embedded key drawing + quoted matched claim +
   rationale, inventive-step combination analysis, and a full-ranked appendix.
3. **⇔ Compare** 2–3 selected references side-by-side (drawings + matched claims + elements covered).

## Run your own (free-text)

Type an invention in the box on the home page (or click an example prompt), pick a mode, and search.
The first run for a new query takes ~1–3 minutes (the agent decomposes it and searches 8 channels);
a progress screen shows what it's doing, and the result is cached. Example that works well:

> *A cordless handheld vacuum lifter for glass and stone panels, with a flexible sealing lip, an
> electric vacuum pump that keeps running to hold grip on rough or porous surfaces, and a pressure
> sensor that alarms the operator when grip vacuum is lost.*

## Health / ops

- `curl http://127.0.0.1:8631/healthz` → `{"ok": true}`
- `./run_tests.sh` → 33 unit/integration tests (~7 s). `./regression.sh` → 30 live E2E checks.
- Managed by supervisor (`patent-results`) — autostarts, auto-restarts, survives reboot.
