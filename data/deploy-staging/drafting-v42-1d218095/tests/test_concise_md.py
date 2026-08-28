"""Markdown is the editable form, so it must be a real round trip.

If `to_markdown` and `from_markdown` are not inverses, editing one sentence silently rewrites
something else — and the thing being edited is filed at the USPTO. Every test here is about what
survives an edit, or about refusing to save rather than saving something lossy.
"""
import pytest

import concise_md
import concise_render


def _doc():
    return {
        "n": 3, "pub": "US-11413727-B2", "family_id": "F1",
        "biblio": {"pub": "US-11413727-B2", "label": "U.S. Patent No. 11,413,727",
                   "kind": "patent", "title": "Vacuum Gripper", "inventor": "Nimrod Rotem",
                   "assignee": "Grabo Ltd", "issue_date_pretty": "August 16, 2022",
                   "priority_date_pretty": "May 9, 2018"},
        "subject": {"app_no": "18/915,337", "pub_no": "US 2025/0033224 A1",
                    "title": "Portable vacuum gripper", "inventor": "Nhon Hoa Nguyen"},
        "summary": "This document discloses a vacuum gripper with a multi-layer seal.",
        "compliance": {"qualify": {"basis": "public_prior_art", "blocked": False}},
        "rows": [
            {"claim_no": 1, "label": "claim 1[a]", "quote_claim": True,
             "claim_text": "a base element comprising one or more openings around a periphery",
             "claim_paraphrase": "", "verdict": "disclosed", "bar": "discloses", "strong": True,
             "quote": "base element 141 with an elliptical track 148", "note": "n",
             "disclosure": "The reference discloses a base element (141) with peripheral openings.",
             "cites": ["Paragraph [0012]", "Claim 8"], "confidence": 0.9},
            {"claim_no": 2, "label": "claim 2[a]", "quote_claim": False, "claim_text": "",
             "claim_paraphrase": "first portion extending into inside areas",
             "verdict": "partial", "bar": "teaches", "strong": False, "quote": "", "note": "n2",
             "disclosure": "The reference describes an inner portion coupled to the base element.",
             "cites": ["Paragraph [0044]"], "confidence": 0.6},
        ],
    }


def test_a_round_trip_changes_nothing_the_renderer_uses():
    src = _doc()
    md = concise_md.to_markdown(src)
    back = concise_md.from_markdown(md, src)
    assert back["n"] == src["n"]
    assert back["summary"] == src["summary"]
    assert len(back["rows"]) == len(src["rows"])
    for a, b in zip(src["rows"], back["rows"]):
        for field in ("claim_no", "quote_claim", "claim_text", "claim_paraphrase",
                      "disclosure", "quote", "cites", "bar", "strong"):
            assert a[field] == b[field], field
    #  And the rendering is byte-identical, which is the property that actually matters.
    assert concise_render.to_pdf(back)[:5] == b"%PDF-"


def test_the_invisible_record_survives_an_edit():
    """The compliance record and the family id are findings, not prose, and are not on the page."""
    src = _doc()
    md = concise_md.to_markdown(src).replace("multi-layer seal", "multi-layer sealing element")
    back = concise_md.from_markdown(md, src)
    assert back["compliance"] == src["compliance"]
    assert back["family_id"] == "F1"
    assert back["summary"].endswith("multi-layer sealing element.")


def test_editing_the_prose_of_one_row_leaves_the_other_alone():
    src = _doc()
    md = concise_md.to_markdown(src).replace(
        "The reference discloses a base element (141) with peripheral openings.",
        "The reference discloses a rigid base plate (141) having openings about its rim.")
    back = concise_md.from_markdown(md, src)
    assert back["rows"][0]["disclosure"].startswith("The reference discloses a rigid base plate")
    assert back["rows"][1]["disclosure"] == src["rows"][1]["disclosure"]


def test_deleting_a_row_deletes_exactly_that_row():
    src = _doc()
    md = concise_md.to_markdown(src)
    head, _, tail = md.partition("### Claim 2 (")
    back = concise_md.from_markdown(head, src)
    assert [r["claim_no"] for r in back["rows"]] == [1]


def test_a_removed_claim_chart_heading_refuses_to_save():
    """Renaming the heading would otherwise produce an empty submission, silently."""
    src = _doc()
    md = concise_md.to_markdown(src).replace("## Claim chart", "## Claims")
    with pytest.raises(concise_md.MarkdownShapeError) as e:
        concise_md.from_markdown(md, src)
    assert "Claim chart" in str(e.value)


def test_a_chart_with_no_rows_refuses_to_save():
    src = _doc()
    md = concise_md.to_markdown(src).split("### Claim 1")[0]
    with pytest.raises(concise_md.MarkdownShapeError):
        concise_md.from_markdown(md, src)


def test_a_malformed_claim_heading_names_itself_instead_of_being_skipped():
    src = _doc()
    md = concise_md.to_markdown(src).replace('### Claim 1: "', '### Claim one: "')
    with pytest.raises(concise_md.MarkdownShapeError) as e:
        concise_md.from_markdown(md, src)
    assert "claim heading" in str(e.value).lower()


def test_a_quotation_deleted_by_hand_drops_the_strong_bar_with_it():
    """A row whose passage the practitioner removed is no longer backed by a verbatim quotation."""
    src = _doc()
    md = concise_md.to_markdown(src).replace(
        "> base element 141 with an elliptical track 148\n", "")
    back = concise_md.from_markdown(md, src)
    assert back["rows"][0]["quote"] == ""
    assert back["rows"][0]["strong"] is False


def test_citations_can_be_edited_and_reordered():
    src = _doc()
    md = concise_md.to_markdown(src).replace("- Paragraph [0012]\n- Claim 8",
                                             "- Claim 8\n- Paragraph [0013]")
    back = concise_md.from_markdown(md, src)
    assert back["rows"][0]["cites"] == ["Claim 8", "Paragraph [0013]"]


def test_the_biblio_block_is_editable():
    src = _doc()
    md = concise_md.to_markdown(src).replace("First Named Inventor: Nimrod Rotem",
                                             "First Named Inventor: N. Rotem")
    back = concise_md.from_markdown(md, src)
    assert back["biblio"]["inventor"] == "N. Rotem"
    assert back["biblio"]["title"] == "Vacuum Gripper"
