"""A limitation must be a span of its own claim, not a description of one.

THE BUG THIS PINS, measured on adhoc-db64a3dd7c98 (US 2026/0034666 A1). Claim 1 reads

    "1. A method for operating a handling system for transporting a plurality of gripping objects,
     in particular of different geometries, from a pick-up area to a deposit area, ..."

and the splitter returned, as claim 1[a],

    "a handling system for transporting a plurality of gripping objects from a pick-up area to a
     deposit area, the handling system comprising a vacuum gripper..."

A METHOD CLAIM BECAME AN APPARATUS CLAIM and "in particular of different geometries" vanished. The
claims themselves were extracted correctly and verbatim; the damage was entirely in the split. 21 of
45 limitations were paraphrase, and the ledger, the rescue's search queries, the BigQuery
limitation portfolios and the user-facing claim table all ran on them.
"""
import limitations as L


CLAIM_1 = (
    "1. A method for operating a handling system for transporting a plurality of gripping objects, "
    "in particular of different geometries, from a pick-up area to a deposit area, the handling "
    "system comprising:\na vacuum gripper having a plurality of individually activatable suction "
    "points; a manipulator for moving the vacuum gripper; a control device for controlling the "
    "handling system, comprising a data processing device and a non-volatile memory device, the "
    "method comprising: receiving or determining a gripping object data set which represents the "
    "position in the pick-up area, weight and geometry for each gripping object"
)

#  Exactly what the model returned on the measured run.
PARAPHRASE = (
    "a handling system for transporting a plurality of gripping objects from a pick-up area to a "
    "deposit area, the handling system comprising a vacuum gripper having a plurality of "
    "individually activatable suction points, a manipulator for moving the vacuum gripper, and a "
    "control device for controlling the handling system comprising a data processing device and a "
    "non-volatile memory device"
)


def test_the_paraphrase_is_detected_as_not_a_span():
    assert not L._is_span(PARAPHRASE, CLAIM_1)


def test_a_faithful_copy_is_a_span_despite_punctuation_and_case():
    quoted = "A MANIPULATOR, for moving the vacuum gripper"
    assert L._is_span(quoted, CLAIM_1)


def test_snap_repairs_the_paraphrase_into_a_real_span():
    span, _exact = L._snap(PARAPHRASE, CLAIM_1, at_head=True)
    assert L._is_span(span, CLAIM_1)


def test_snap_restores_the_statutory_class_and_the_dropped_qualifier():
    """The two things the paraphrase actually destroyed."""
    span, _exact = L._snap(PARAPHRASE, CLAIM_1, at_head=True)
    assert span.startswith("A method for operating"), span[:80]
    assert "in particular of different geometries" in span


def test_head_extension_only_applies_to_the_first_limitation():
    """A body clause must not be silently widened back to the start of the claim."""
    body = "a manipulator for moving the vacuum gripper"
    span, _exact = L._snap(body, CLAIM_1, at_head=False)
    assert not span.startswith("A method for operating")
    assert L._is_span(span, CLAIM_1)


def test_an_invention_that_is_not_in_the_claim_is_dropped_not_snapped():
    """The real claim 20[c]: a tautology the model assembled from vocabulary in the claim.

    Dropping beats keeping. A limitation nothing can disclose reads as uncovered art forever, and
    a limitation the claim does not contain is not a requirement of that claim at all.
    """
    invented = "A grip unit comprising a bracing structure protruding beyond the vacuum seal"
    assert L._snap(invented, CLAIM_1) is None


def test_split_claims_marks_every_limitation_with_whether_it_is_verbatim():
    lims = L.split_claims(
        [{"label": "claim 1", "claim_no": 1, "text": CLAIM_1, "independent": True}],
        use_llm=False)
    assert lims, "the structural fallback must never return an empty list"
    assert all("verbatim" in l for l in lims)
