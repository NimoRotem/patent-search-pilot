"""A citation is (publication INCLUDING kind code, location, text). Two thirds were never checked.

Counsel found three citations in one packet that do not resolve, and all three are the same defect:
an abstract cited on a document whose filed copy has no abstract, paragraph numbers belonging to
another member of the same family, and a quotation carrying a number its source states
qualitatively. The existence check verified that the TEXT appeared somewhere.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import citation as ct                                                    # noqa: E402
import concise_description as cd                                         # noqa: E402
import submission_compliance as sc                                       # noqa: E402

#  US 2022/0045594 A1 [0061], as it actually reads. The packet quoted it with a numeric tolerance.
P61 = ("the size of the projection and the gap should approximately match the thickness of the "
       "ferromagnetic workpieces being handled by the magnetic gripper assembly")
P47 = ("a pole shoe is fastened to the housing such that the workpiece contact surfaces together "
       "form a holding face for the workpiece")

SOURCE = {
    "found": True, "pub": "US-20220045594-A1",
    "passages": [
        {"kind": "abstract", "coord": {}, "label": "abstract",
         "text": "A magnetic gripper assembly for lifting ferromagnetic workpieces."},
        {"kind": "claim", "coord": {"claim_no": 1}, "label": "claim 1",
         "text": "A magnetic gripper comprising a housing and a pole shoe."},
        {"kind": "paragraph", "coord": {"para_no": "p0047"}, "label": "paragraph p0047",
         "text": P47},
        {"kind": "paragraph", "coord": {"para_no": "p0061"}, "label": "paragraph p0061",
         "text": P61},
    ],
}


def _doc(rows, pub="US-20220045594-A1"):
    return {"pub": pub, "rows": rows,
            "biblio": {"pub": pub, "country": "US", "abstract": ""}}


# --------------------------------------------------------------------------- the grammar


def test_one_place_written_four_ways_is_one_place():
    k = (ct.PARA, "47")
    assert ct.of_text("Paragraph [0047]") == k
    assert ct.of_text("paragraph p0047") == k
    assert ct.key("paragraph", {"para_no": "p0047"}, "") == k
    assert ct.key("", {}, "para 47") == k


def test_an_abstract_and_a_claim_are_not_the_same_place():
    assert ct.of_text("Abstract") != ct.of_text("Claim 7")
    assert ct.same_place(ct.of_text("Abstract"), ct.of_text("Abstract"))
    assert not ct.same_place(ct.of_text("Abstract"), ct.of_text("Claim 7"))


def test_a_place_that_cannot_be_turned_to_does_not_resolve():
    assert ct.resolves(ct.of_text("Paragraph [0047]")) is True
    assert ct.resolves(ct.of_text("")) is False
    assert ct.resolves(ct.key("description", {}, "description")) is False


def test_the_writer_and_the_checker_agree_on_every_shape():
    for cell, want in (({"coord": {"para_no": "p0047"}}, "Paragraph [0047]"),
                       ({"coord": {"claim_no": 14}}, "Claim 14"),
                       ({"location": "abstract"}, "Abstract"),
                       ({"coord": {"fig_no": 3}}, "FIG. 3")):
        rendered = cd._cite(cell)
        assert rendered == want
        assert ct.of_text(rendered) == ct.of_cell(cell)


def test_a_cell_with_no_locatable_coordinate_gets_no_citation_rather_than_a_fragment():
    assert cd._cite({"location": "somewhere in the description"}) == ""


# --------------------------------------------------------------------------- kind codes


def test_two_kind_codes_of_one_application_are_two_documents():
    assert ct.same_publication("DE-102019131000-A1", "DE-102019131000-B4") is False
    assert ct.same_publication("DE-102019131000-A1", "DE-102019131000-A1") is True
    #  A row the corpus stored without a kind code is not a mismatch, it is silence.
    assert ct.same_publication("DE-102019131000", "DE-102019131000-B4") is True


def test_a_quotation_read_from_one_family_member_is_never_filed_on_another():
    """A1 and B4 of one German application: 96 paragraphs against 99, fourteen with no twin, and
    offsets between matching text of -5, 0, +1, +2 and +3. A cite carried across resolves to
    nothing."""
    doc = _doc([{"quote": P47, "cites": ["Paragraph [0047]"], "strong": True}],
               pub="US-20220045594-B2")
    got = sc.verify_quotes(doc, SOURCE)
    assert got["dropped"] == 1
    assert doc["rows"][0]["quote"] == ""
    assert "different publications" in got["note"]


# --------------------------------------------------------------------------- the location


def test_a_quotation_cited_to_the_wrong_paragraph_is_corrected_from_the_data():
    """The Schunk case: cited (p0047, p0053), and the sentence is at [0068] in the A1."""
    doc = _doc([{"quote": P61, "cites": ["Paragraph [0047]"], "strong": True}])
    got = sc.verify_quotes(doc, SOURCE)
    assert got["dropped"] == 0
    assert got["relocated"] == 1
    assert doc["rows"][0]["cites"][0] == "Paragraph [0061]"
    assert doc["rows"][0]["cite_corrected"] is True
    assert "Paragraph [0047] -> Paragraph [0061]" in got["note"]


def test_a_quotation_cited_to_an_abstract_it_did_not_come_from_is_moved_to_the_paragraph():
    """Document 6's description cited every quotation to "Abstract" and the filed copy has none."""
    doc = _doc([{"quote": P47, "cites": ["Abstract"], "strong": True}])
    sc.verify_quotes(doc, SOURCE)
    assert doc["rows"][0]["cites"][0] == "Paragraph [0047]"


def test_a_correct_citation_is_left_exactly_as_it_was():
    doc = _doc([{"quote": P61, "cites": ["Paragraph [0061]"], "strong": True}])
    got = sc.verify_quotes(doc, SOURCE)
    assert got["relocated"] == 0 and got["dropped"] == 0
    assert doc["rows"][0]["cites"] == ["Paragraph [0061]"]
    assert "cite_corrected" not in doc["rows"][0]


def test_an_unlocatable_quotation_loses_its_pinpoint_rather_than_keeping_an_invented_one():
    """A quotation whose words are in the document but whose passage cannot be established. The
    quotation is filed; the pinpoint is not. An invented pinpoint is a false statement."""
    spread = {"found": True, "pub": "US-20220045594-A1", "passages": [
        {"kind": "description", "coord": {}, "label": "description", "text": P61}]}
    doc = _doc([{"quote": P61, "cites": ["Paragraph [0061]"], "strong": True}])
    got = sc.verify_quotes(doc, spread)
    assert got["dropped"] == 0
    assert got["unpinpointed"] == 1
    assert doc["rows"][0]["quote"] == P61, "the quotation stands"
    assert doc["rows"][0]["cites"] == []
    assert doc["rows"][0]["cite_unresolved"] is True
    assert "without a pinpoint" in got["note"]


def test_a_quotation_the_document_does_not_contain_is_still_dropped():
    """US 2022/0045594 A1 was quoted as saying "within approximately +/- 25% the thickness". The
    document says the two "should approximately match", qualitatively, with no number."""
    invented = ("the projection width is within approximately +/- 25% the thickness of the "
                "ferromagnetic workpiece")
    doc = _doc([{"quote": invented, "cites": ["Paragraph [0061]"], "strong": True}])
    got = sc.verify_quotes(doc, SOURCE)
    assert got["dropped"] == 1
    assert doc["rows"][0]["quote_unverified"] is True


def test_every_verified_row_records_the_publication_its_pinpoint_belongs_to():
    doc = _doc([{"quote": P47, "cites": ["Paragraph [0047]"], "strong": True}])
    sc.verify_quotes(doc, SOURCE)
    assert doc["rows"][0]["cite_pub"] == "US-20220045594-A1"


def test_a_plain_string_source_still_verifies_the_text_it_always_did():
    """An older caller, or a document whose text came from somewhere with no passages. The text
    check must not become conditional on the structure being available."""
    doc = _doc([{"quote": P47, "cites": ["Paragraph [0047]"], "strong": True},
                {"quote": "a hydraulic accumulator", "cites": ["Claim 3"], "strong": True}])
    got = sc.verify_quotes(doc, P47)
    assert (got["checked"], got["dropped"]) == (2, 1)
    assert doc["rows"][0]["quote"] == P47
    assert doc["rows"][1]["quote"] == ""
