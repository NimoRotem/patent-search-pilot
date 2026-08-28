# Previous review - verdict: fail

Scope note on the workspace: figures/ contains only the seven Markdown briefs - there is no rendered-*.png for any figure - and review/figure-audit-evidence.json is absent. I therefore performed no pixel, OCR, leader-endpoint or hatch-angle verification and assert nothing about rendered geometry; the two already-reported mechanical failures (31 numerals absent from every drawing, FIG. 1-FIG. 7 lacking sheets) are the same fact and I have not duplicated them as findings. prior_art/INDEX.md lists "(none)", and the draft contains no [REF:...] citation anywhere, so step 4 has nothing to mis-cite.

Source ledger result: the draft traces cleanly to input/disclosure.md and input/conversation.md on essentially every point. I walked all 20 claims limitation by limitation, all 31 numerals, and every structure, relationship, position and result asserted in the Summary, Detailed Description and figure briefs. The shell/floor/side walls, tray on feet with lower return passage, tray walls spaced inward forming two side supply passages, perforations, rigid spacer frame with two opposed cassettes, central inlet, two downward guide ducts beside the cassettes, peripheral outlet openings aligned with the side supply passages, the full circulation path, the "no fan, pump or powered air mover" negative limitation, the depending indexing rib entering the mating channel to shift the module into a registered position with the outlets overlapping the passages, the gasket sealing after registration, resilient feet pressed against shell ledges, the condensate channel/wick/pull tab/discharge openings, and the whole mechanical exposure indicator (stem thermally coupled to one cassette, temperature-responsive latch, spring, latched exposed position, manual reset after opening, no airflow control) all appear verbatim or as direct entailments in the inventor's own words. Claim 12's progressive-engagement sequence is entailed by the disclosed "gasket seals ... after registration"; claim 15's upward-opening top-region channel is entailed by a depending rib entering it as the lid closes; claim 5's removable cassettes is entailed by "conditioned cassettes are fitted to the spacer frame". Where the disclosure is silent (how air returns from the lower return passage to the product region, how the stem is thermally coupled across to the cassette), the candidate is faithfully silent too and invents no route - I raised no finding for those.

Internal consistency: all 31 spec numerals are defined in numerals.md, all 31 are used in the spec, and the union of the seven briefs' numeral lists is exactly those 31 with no numeral labelling two different parts. Every "seen in FIG. n" cross-reference in the Detailed Description matches that brief. The three enlargement callouts on FIG. 1 match FIG. 3/4/5 in both content and left/right orientation, and the enlargement designations are kept out of the numeral lists. I re-derived the FIG. 6 to FIG. 7 section geometry from scratch: FIG. 6 views the wall from outside, both cutting-plane arrows point left, and looking left puts the outboard side on the left of FIG. 7 - which is exactly what FIG. 7's brief shows, with a broken line, a matching "7" beside each arrow and no numeral leader on either designation. That is correct. The abstract is 141 words, one paragraph. No em dashes, placeholders or invented citations.

Three defects remain, all minor and all fixable in the existing text or briefs: one structural allocation of the condensate channel to the shell that the disclosure does not state, uncited factual characterisations of the prior art in the Background with prior_art/ empty, and a naming mismatch between "module body" in the FIG. 4/FIG. 5 briefs and "spacer frame 42" in the description and claims.

## Mechanical checks that did not pass

- **Every specification numeral appears in a drawing** (fail): 31 reference numeral(s) are used in the specification but absent from every drawing. Add each missing numeral to an appropriate focused sheet or redistribute the existing drawing plan. Do not remove a disclosed part, numeral definition, or supporting text to silence this check.
  - 10
  - 12
  - 14
  - 18
  - 20
  - 22
  - 24
  - 26
  - 28
  - 30
  - 40
  - 42
  - 44
  - 46
  - 48
  - 50
  - 54
  - 56
  - 58
  - 60
  - 62
  - 64
  - 70
  - 72
  - 74
  - 76
  - 80
  - 82
  - 84
  - 86
  - 100
- **Each described figure has a drawing sheet** (fail): A figure is described in the specification but no drawing has been prepared for it. Every described figure is required before the package can be published.
  - FIG. 1
  - FIG. 2
  - FIG. 3
  - FIG. 4
  - FIG. 5
  - FIG. 6
  - FIG. 7

## Reviewer findings

- **[minor] Detailed Description and FIG. 5 allocate the condensate channel to the outer shell; the disclosure only places it "around the product tray"** (draft/08-detailed-description.md, "Condensate management", first paragraph; figures/FIG-5.md, paragraph beginning "In th) - The inventor says only where the channel is relative to the tray, not which component carries it. The candidate resolves that open point in two places: the description asserts the channel itself stays behind in the shell when the tray is lifted out, and the FIG. 5 brief forms the trough in the top surface of the shell's bottom body. The wick staying accessible when the tray is removed is disclosed; the channel being formed in the shell rather than in or on the tray is not. This reads as the description being widened to match a concrete geometry the sheet needed. I mark it minor rather than critical because the disclosed sentence "A pull tab on the wick remains accessible when the product tray is removed" makes a shell-borne wick the natural reading, and because claims 16 and 19 recite only "within the outer shell", which is not the same assertion and needs no change.
  - Suggested fix: In 08-detailed-description.md replace "and the condensate channel 70 and the absorbent wick 72 remain in the outer shell 10 when the product tray 20 is removed" with "and the absorbent wick 72 remains in the outer shell 10 when the product tray 20 is removed", which is the relationship the disclosure states. In figures/FIG-5.md replace "In the upwardly facing top surface of the horizontal bottom body" with "At the bottom of the space outboard of the thin upright wall, shown schematically", so the sheet depicts the disclosed trough without asserting which body forms it.
- **[minor] Background states specific facts about existing carriers and data loggers with no source in prior_art/ or input/** (draft/05-background.md, paragraphs 2 and 4) - prior_art/INDEX.md lists no references at all, and the draft cites none, so every prior-art characterisation in the Background is untraceable to any document in the workspace, and the inventor's disclosure says nothing about the prior art. Most of the Background is unobjectionable field description, but several sentences assert definite facts about how existing products behave (a regulatory consequence for air transport, a temperature spread that grows with packing density, the cost and battery-management burden of loggers and what they report). The drafting brief itself records that no search was run. The filing-clean repair is to state these as characteristics that can occur rather than as established facts; no source needs to be added.
  - Suggested fix: Soften the three assertions to conditional form in 05-background.md: "Passive carriers are attractive because they need have no battery to charge, no motor to fail and no electrical approval burden for air transport"; "The result can be a temperature spread across the payload that tends to grow as the load is packed more densely, and that may vary from shipment to shipment"; "Electronic data loggers can record such events, but add cost, battery management and a reading step to every unit shipped, and they report a sensor location rather than the state of the refrigerant that the payload actually depends on."
- **[minor] FIG. 4 and FIG. 5 briefs call the sectioned body carrying guide duct 50, outlet 54 and discharge opening 76 the "module body", while the description and claims make those features part of spacer frame 42** (figures/FIG-4.md and figures/FIG-5.md (repeated use of "the module body"); compare figures/FIG-3.md and draft/08-detaile) - One thing carries two names across the drawing set. FIG. 3 names the same sectioned part "the sectioned spacer frame 42", FIG. 1 uses "thermal module 40" for the frame-plus-cassettes assembly, and FIG. 4 and FIG. 5 use "the module body" for the part in which guide duct 50, peripheral outlet opening 54 and discharge opening 76 are formed. The description and claims 7 and 16 attribute all three of those features to the spacer frame, not to the module as a whole, so "module body" invites the reader to associate them with the assembly that also includes the cassettes. No reference numeral is misapplied (42 and 40 are not called out on FIG. 4 or FIG. 5), so this is a naming inconsistency only, and coverage of the 31 numerals is unaffected by the repair.
  - Suggested fix: In figures/FIG-4.md and figures/FIG-5.md replace every occurrence of "the module body" with "the sectioned spacer frame" (and "this module body" with "this sectioned spacer frame"), matching FIG. 3 and the description. Leave the numeral lists of both briefs unchanged.

Fix every listed item before returning. If an advisory is a false positive, make
the wording or figure specification unambiguous enough that the check passes.
