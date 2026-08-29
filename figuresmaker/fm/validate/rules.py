"""The rules being checked, and where each one comes from.

Every finding carries a citation and a basis. ``rule`` means the requirement is stated in the
regulation or the manual and the wording is the Office's; ``practice`` means the regulation gives
a requirement without a number and this is the threshold the checker uses to make it testable.

That distinction is the whole point of putting the table in one place. "Lines must be uniformly
thick and well-defined" is a rule. "No line thinner than 0.15 mm" is one way of testing it, and
telling an attorney that a drawing violates 37 CFR 1.84(l) when what actually happened is that it
failed somebody's threshold is how a compliance report stops being trusted.
"""
from __future__ import annotations

from typing import NamedTuple


class Rule(NamedTuple):
    code: str
    cite: str
    basis: str          # rule | practice
    title: str


# Numeric thresholds. Only those marked practice are ours.
MIN_CHARACTER_MM = 3.2          # 37 CFR 1.84(p)(3), 0.32 cm
MIN_STROKE_MM = 0.15            # practice
MIN_FIGURE_GAP_MM = 6.0         # practice, for 1.84(i) "separated by adequate space"
MIN_HATCH_SPACING_MM = 0.7      # practice, for 1.84(h)(3) "distinguished without difficulty"
MAX_GREY_FRACTION = 0.15        # practice, for 1.84(m); grey not adjacent to any black line
LEADER_TOUCH_MM = 0.6           # practice, for 1.84(q) "extend to the feature indicated"

PAPER_SIZES_CM = ((21.0, 29.7), (21.6, 27.9))

RULES: dict[str, Rule] = {r.code: r for r in (
    # -------------------------------------------------------------------------- the drawing
    Rule("not_black_and_white", "37 CFR 1.84(a)(1)", "rule",
         "Drawings must be made in black ink, in India ink or its equivalent."),
    Rule("line_too_thin", "37 CFR 1.84(l)", "practice",
         "Every line must be durable, clean, black, sufficiently dense and dark, and uniformly "
         "thick and well-defined."),
    Rule("lines_not_uniform", "37 CFR 1.84(l)", "practice",
         "Lines must be uniformly thick."),
    Rule("shading_present", "37 CFR 1.84(m)", "rule",
         "Shading, where used, must be by line; solid black areas are not permitted except as a "
         "symbol or to show a bar graph or colour."),

    # ------------------------------------------------------------------------------- paper
    Rule("bad_paper_size", "37 CFR 1.84(f)", "rule",
         "Sheets must be 21.0 by 29.7 cm (DIN size A4) or 21.6 by 27.9 cm (8 1/2 by 11 inches)."),
    Rule("outside_margins", "37 CFR 1.84(g)", "rule",
         "Margins: 2.5 cm top, 2.5 cm left, 1.5 cm right and 1.0 cm bottom; the sight must not "
         "exceed 17.0 by 26.2 cm."),
    Rule("figure_overruns_sheet", "37 CFR 1.84(g)", "rule",
         "The whole of a view must fall within the sight of the sheet."),
    Rule("figures_crowded", "37 CFR 1.84(i)", "rule",
         "Views must not be crowded, and must be separated by adequate space."),
    Rule("sheet_number_missing", "37 CFR 1.84(t)", "rule",
         "Sheets are numbered in consecutive Arabic numerals within the sight."),

    # ------------------------------------------------------------- characters and lead lines
    Rule("numeral_too_small", "37 CFR 1.84(p)(3)", "rule",
         "Numbers, letters and reference characters must be at least 0.32 cm high."),
    Rule("numeral_on_hatching", "37 CFR 1.84(p)(2)", "rule",
         "Reference characters must not be placed upon hatched or shaded surfaces; where that is "
         "unavoidable, a blank space must be left in the hatching."),
    Rule("numeral_on_ink", "37 CFR 1.84(p)(1)", "practice",
         "Reference characters must be plain and legible."),
    Rule("numerals_overlap", "37 CFR 1.84(p)(1)", "rule",
         "Reference characters must be plain and legible and must not be used with brackets or "
         "inverted commas or enclosed within outlines."),
    Rule("leaders_cross", "37 CFR 1.84(q)", "rule",
         "Lead lines must not cross each other."),
    Rule("leader_not_touching", "37 CFR 1.84(q)", "rule",
         "A lead line must extend to the feature indicated."),
    Rule("leader_crosses_geometry", "37 CFR 1.84(q)", "practice",
         "Lead lines should be as short as possible and must be clear of other detail."),
    Rule("leader_missing", "37 CFR 1.84(q)", "rule",
         "Each reference character must be tied to the feature it indicates by a lead line, "
         "unless it rests on and points to the surface itself."),

    # -------------------------------------------------------------------------------- text
    Rule("impermissible_text", "37 CFR 1.84(o)", "rule",
         "The drawing may carry only descriptive legends, which are subject to approval by the "
         "Office and should contain as few words as possible."),
    Rule("legend_used", "37 CFR 1.84(o)", "rule",
         "Descriptive legends are subject to approval by the Office."),
    Rule("legend_overflows", "37 CFR 1.84(o)", "practice",
         "A legend should contain as few words as possible, and must fit the feature it names."),

    # ---------------------------------------------------------------- numerals and the spec
    Rule("numeral_not_in_registry", "37 CFR 1.84(p)(4)", "rule",
         "Reference characters not mentioned in the description must not appear in the drawings."),
    Rule("registry_numeral_undrawn", "37 CFR 1.84(p)(4)", "rule",
         "Reference characters mentioned in the description must appear in the drawings."),
    Rule("numeral_reused", "37 CFR 1.84(p)(5)", "rule",
         "The same reference character must never be used to designate different parts."),
    Rule("part_two_numerals", "37 CFR 1.84(p)(5)", "rule",
         "The same part appearing in more than one view must always carry the same reference "
         "character."),

    # -------------------------------------------------------------------------------- views
    Rule("figures_not_sequential", "37 CFR 1.84(u)(1)", "rule",
         "Views must be numbered consecutively in Arabic numerals, preceded by the abbreviation "
         "FIG."),
    Rule("figure_label_malformed", "37 CFR 1.84(u)(1)", "rule",
         "A view is designated FIG. followed by its number."),
    Rule("section_without_hatching", "37 CFR 1.84(h)(3)", "rule",
         "The cut surface of a sectional view must be hatched with regularly spaced oblique "
         "parallel lines."),
    Rule("hatching_too_close", "37 CFR 1.84(h)(3)", "practice",
         "Hatching must be spaced far enough apart to be distinguished without difficulty."),
    Rule("section_line_missing", "37 CFR 1.84(h)(3)", "rule",
         "The plane of a sectional view must be indicated on the view from which it is taken by "
         "a broken line whose ends are designated by the same letter as the section."),
    Rule("figure_set_truncated", "", "practice",
         "This run drew fewer views than the draft asked for."),
    Rule("no_figures", "37 CFR 1.81(a)", "rule",
         "A drawing is required where it is necessary for the understanding of the subject "
         "matter."),

    # -------------------------------------------------------------------------------- claims
    Rule("claim_element_not_depicted", "37 CFR 1.83(a)", "rule",
         "The drawing must show every feature of the invention specified in the claims."),
    Rule("claim_element_unmatched", "37 CFR 1.83(a)", "practice",
         "A claimed feature that carries no reference character cannot be checked against the "
         "drawings."),
    Rule("brief_description_mismatch", "37 CFR 1.74", "rule",
         "The specification must refer to the different views by specifying the numbers of the "
         "figures."),
    Rule("brief_description_missing", "37 CFR 1.77(b)(7)", "rule",
         "The specification should contain a brief description of the several views of the "
         "drawings."),
    Rule("element_not_drawn", "37 CFR 1.84(p)(4)", "rule",
         "Reference characters mentioned in the description must appear in the drawings."),

    # -------------------------------------------------- what the registry finds in the draft
    Rule("element_unnumbered", "37 CFR 1.84(p)(4)", "rule",
         "A part the description names must carry a reference character if a drawing is to show "
         "it, because a character in a drawing must be mentioned in the description."),
    Rule("numeral_two_terms", "37 CFR 1.84(p)(5)", "rule",
         "The same reference character must never be used to designate different parts."),
    Rule("term_two_numerals", "37 CFR 1.84(p)(5)", "rule",
         "The same part appearing in more than one view must always carry the same reference "
         "character."),
    Rule("numeral_no_figure", "37 CFR 1.84(p)(4)", "practice",
         "A reference character the description never ties to a view leaves the drawing set to "
         "guess where it belongs."),
    Rule("figure_never_discussed", "37 CFR 1.84(p)(4)", "practice",
         "A view the brief description promises and no paragraph discusses has no stated "
         "contents."),

    # ------------------------------------------------------------------ the renderer's own
    Rule("geometry_not_authoritative", "", "practice",
         "A mechanical view must be compiled from geometry the applicant supplied. A view "
         "blocked out from the description alone is a draft, not a drawing of the invention."),
    Rule("source_unusable", "", "practice",
         "A supplied source could not be read."),
    Rule("part_not_in_supplied_geometry", "37 CFR 1.84(p)(4)", "practice",
         "A part the description names that the supplied geometry does not contain. It cannot "
         "be drawn without being modelled, and it will not be invented."),
    Rule("components_unassigned", "37 CFR 1.84(p)(4)", "practice",
         "A part present in the supplied geometry that no reference character names."),
    Rule("element_not_legible", "37 CFR 1.84(l)", "practice",
         "A part must be drawn large enough to be seen; a reference character pointing at a "
         "speck depicts nothing."),
    Rule("assembly_disconnected", "37 CFR 1.84(h)(1)", "practice",
         "An assembled view shows the parts assembled; parts shown separated need an exploded "
         "view, with the separated parts embraced by a bracket or joined by a projection line."),
    Rule("numeral_inside_other_part", "37 CFR 1.84(p)(2)", "rule",
         "Reference characters must not be placed upon the drawing figure itself."),
    Rule("figure_not_drawn", "37 CFR 1.81(a)", "practice",
         "A view that was planned but could not be produced."),
    Rule("raster_check_skipped", "", "practice",
         "The pixel checks could not run, so colour, line density and stray ink were not "
         "verified."),
    Rule("numeral_discarded", "", "practice",
         "A number in the description that was read as a quantity rather than a reference "
         "character."),
)}


def decorate(code: str) -> tuple[str, str]:
    """The citation and basis for a finding code. Unknown codes carry neither, deliberately."""
    rule = RULES.get(code)
    return (rule.cite, rule.basis) if rule else ("", "practice")


def title(code: str) -> str:
    rule = RULES.get(code)
    return rule.title if rule else ""
