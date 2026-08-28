# Previous review - verdict: fail

Source fidelity is clean: every independent and dependent claim limitation, all 29 reference numerals, and every structure, relationship, sequence and variant in draft/06-summary.md, draft/08-detailed-description.md and the four figure briefs traces to an exact passage in input/disclosure.md (input/conversation.md repeats it verbatim). Terminology is one name per thing throughout (all six preserved terms from input/brief.md are used), and the summary, description, claims, numeral table and briefs do not contradict one another. Numeral-to-part mapping is one-to-one across draft/numerals.md, all four briefs and the description, and each "FIG. N shows ..." list in the description matches exactly the numeral set of the corresponding brief; 18 (metering sleeve), 22 (concentrate port) and 44 (follower pin) each label the same part on both sheets that carry them, and the four briefs between them assign all 29 numerals with exactly one explicit target each. Neither FIG. 2 nor FIG. 3 is taken on a designated section line - both are enlargements of the FIG. 1 metering portion - so no cutting-plane line, section designation or arrows are called for anywhere in the set. prior_art/INDEX.md lists no references and the draft contains no [REF:] citation, so the citation check is vacuously clean; per input/brief.md no prior-art search was run, so the art available to this review may be incomplete. One defect blocks the package: all four described figures exist only as Markdown briefs. A workspace-wide search finds no .png file and no review/figure-audit-evidence.json, so no drawing sheet exists for FIG. 1 through FIG. 4 and no pixel, OCR, geometry, leader, hatch or endpoint verification of the drawings was possible; nothing in this review is evidence that eventual sheets will be correct.

## Mechanical checks that did not pass

- **Every specification numeral appears in a drawing** (fail): 29 reference numeral(s) are used in the specification but absent from every drawing. Add each missing numeral to an appropriate focused sheet or redistribute the existing drawing plan. Do not remove a disclosed part, numeral definition, or supporting text to silence this check.
  - 10
  - 12
  - 14
  - 16
  - 18
  - 20
  - 22
  - 24
  - 26
  - 28
  - 30
  - 32
  - 34
  - 36
  - 38
  - 40
  - 42
  - 44
  - 46
  - 48
  - 50
  - 52
  - 54
  - 56
  - 58
  - 60
  - 62
  - 64
  - 66
- **Each described figure has a drawing sheet** (fail): A figure is described in the specification but no drawing has been prepared for it. Every described figure is required before the package can be published.
  - FIG. 1
  - FIG. 2
  - FIG. 3
  - FIG. 4

## Reviewer findings

- **[critical] No drawing sheet exists for any of the four described figures, and no figure audit evidence exists** (figures/ (FIG-1.md, FIG-2.md, FIG-3.md, FIG-4.md); draft/07-drawings.md; draft/08-detailed-description.md; review/ (no f) - draft/07-drawings.md describes four figures and draft/08-detailed-description.md attributes specific numbered content to each of them, but figures/ holds only the four Markdown briefs. A recursive search of the workspace returns no .png (and no .svg) file at all and no review/figure-audit-evidence.json, so no image exists for any described figure and no pixel, OCR, leader-endpoint, geometry or hatch verification could be performed on this application. This single absence is the whole cause of both reported mechanical failures: the 29 numerals are 'absent from every drawing' only because no drawing file exists to read them from, and each described figure lacks a sheet for the same reason. The drawing plan itself is complete and should not be disturbed - the four briefs assign every numeral defined in draft/numerals.md (FIG. 1: 10, 12, 14, 16, 18, 52, 64, 66; FIG. 2: 18, 20, 22, 24, 30, 32, 34, 38; FIG. 3: 22, 26, 28, 44, 56, 58, 60, 62; FIG. 4: 36, 40, 42, 44, 46, 48, 50, 54, being 29 distinct numerals from 10 through 66), each numeral has exactly one explicit target in that sheet's Targets list, and each target names the same part the numeral labels in draft/numerals.md and in the detailed description. The mechanical check's suggested remedy of adding or redistributing numerals must therefore not be followed, because it would break a mapping that is already complete and correct.
  - Suggested fix: Render the four existing briefs exactly as written as figures/rendered-FIG-1.png through figures/rendered-FIG-4.png, and generate review/figure-audit-evidence.json against those exact image hashes. Do not add, move or redistribute any numeral between sheets and do not delete any numeral definition or supporting text; rendering the existing plan alone clears both mechanical failures.

Fix every listed item before returning. If an advisory is a false positive, make
the wording or figure specification unambiguous enough that the check passes.
