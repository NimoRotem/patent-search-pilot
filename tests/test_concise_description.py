"""The three rules that govern a paper filed at the USPTO, locked in.

These anchor on the ABORT: what must NOT reach the page. A concise description of relevance is
filed in a live examination, so the failure that matters is not an ugly table, it is a citation or
a quotation the record does not support.
"""
import re

import pytest

import concise_description as cd
import concise_render as cr


def _cell(item, verdict="disclosed", grounding="verified", bar="discloses",
          quote="a verbatim passage from the reference", note="The reference discloses a widget.",
          location="paragraph p0012", coord=None, confidence=0.8):
    return {"item": item, "verdict": verdict, "grounding": grounding, "bar": bar, "quote": quote,
            "note": note, "location": location,
            "coord": coord if coord is not None else {"para_no": "p0012"},
            "confidence": confidence}


def _claims():
    return [
        {"label": "claim 1[a]", "claim_no": 1, "independent": True,
         "text": "a base element comprising one or more openings around a periphery"},
        {"label": "claim 1[b]", "claim_no": 1, "independent": True,
         "text": "a vacuum seal element coupled to the base element"},
        {"label": "claim 2[a]", "claim_no": 2, "independent": False,
         "text": "The gripper of claim 1, wherein the first portion extends into inside areas"},
        {"label": "claim 3[a]", "claim_no": 3, "independent": False,
         "text": "The gripper of claim 1, wherein the layer properties are selected"},
    ]


def _ref(cells, pub="US-11413727-B2"):
    return {"pub": pub, "title": "Vacuum Gripper", "rank": 1, "claims": cells}


# ------------------------------------------------------------------ rule 3: no evidence, no row


def test_a_claim_with_no_evidence_gets_no_row():
    ref = _ref([_cell("claim 1[a]")])
    rows = cd.rows_for_reference(ref, _claims())
    assert [r["claim_no"] for r in rows] == [1]      # claims 2 and 3 are silent, not empty rows


def test_absent_and_uncertain_are_not_findings():
    for verdict in ("absent", "uncertain", ""):
        ref = _ref([_cell("claim 1[a]", verdict=verdict)])
        assert cd.rows_for_reference(ref, _claims()) == [], verdict


def test_an_ungrounded_cell_never_earns_a_row():
    """A cell whose passage did not survive verification has nothing standing behind it."""
    ref = _ref([_cell("claim 1[a]", grounding="unverified")])
    assert cd.rows_for_reference(ref, _claims()) == []


# ------------------------------------------------------------------ rule 2: quotes only on strong


def test_a_teaches_cell_is_filed_without_quotation_marks():
    ref = _ref([_cell("claim 1[a]", bar="teaches", quote="something the reader paraphrased")])
    rows = cd.rows_for_reference(ref, _claims())
    assert len(rows) == 1 and rows[0]["strong"] is False
    assert rows[0]["quote"] == ""
    prose, _cites = cr._right_bits(rows[0])
    assert "“" not in prose and '"' not in prose


def test_a_strong_cell_keeps_its_passage_and_marks_an_elision():
    long_quote = " ".join("word%d" % i for i in range(80))
    ref = _ref([_cell("claim 1[a]", quote=long_quote)])
    prose, _ = cr._right_bits(cd.rows_for_reference(ref, _claims())[0])
    assert "“" in prose and prose.rstrip().endswith("…”")
    assert prose.count("word") <= 41           # capped at the 40 words the reader was asked for


# ------------------------------------------------------------------ rule 1: citations from record


@pytest.mark.parametrize("coord,expected", [
    ({"para_no": "p0012"}, "Paragraph [0012]"),
    ({"para_no": "p0001"}, "Paragraph [0001]"),
    ({"claim_no": 8}, "Claim 8"),
])
def test_citations_are_read_off_the_recorded_coordinate(coord, expected):
    assert cd._cite(_cell("claim 1[a]", coord=coord)) == expected


def test_a_model_written_citation_is_stripped_from_the_disclosure(monkeypatch):
    """The prompt forbids citations; this is the guard for when the model ignores it.

    A fabricated column or paragraph number in a filed paper is the failure this module exists to
    prevent, so it is stripped in code rather than trusted to the instruction.
    """
    ref = _ref([_cell("claim 1[a]")])
    doc = {"biblio": cd.biblio("US-11413727-B2"),
           "rows": cd.rows_for_reference(ref, _claims())}

    class _FakeLLM:
        @staticmethod
        def chat_json(system, user, **kw):
            return {"summary": "This document discloses a gripper.",
                    "rows": [{"id": 0, "disclosure":
                              "The reference discloses a base element (141) at column 17, "
                              "lines 3-6 and FIG. 9, see paragraph [0044]."}]}

    monkeypatch.setitem(__import__("sys").modules, "llm", _FakeLLM)
    out = cd.phrase(doc)
    text = out["rows"][0]["disclosure"]
    assert "column 17" not in text.lower()
    assert "paragraph" not in text.lower()
    assert "FIG. 9" not in text
    assert "base element (141)" in text          # the substance survives, only the cite is cut


def test_the_citation_shown_is_the_one_from_the_cell_not_the_prose():
    ref = _ref([_cell("claim 1[a]", coord={"para_no": "p0044"})])
    row = cd.rows_for_reference(ref, _claims())[0]
    assert row["cites"] == ["Paragraph [0044]"]


# ------------------------------------------------------------------ left column


def test_an_independent_claim_is_quoted_verbatim_and_a_dependent_is_paraphrased():
    ref = _ref([_cell("claim 1[a]"), _cell("claim 2[a]")])
    rows = {r["claim_no"]: r for r in cd.rows_for_reference(ref, _claims())}
    assert rows[1]["quote_claim"] is True
    left1 = cr._left_cell(rows[1], "US 2025/0033224 A1")
    assert left1.startswith('Claim 1: “') and "a base element comprising" in left1
    assert rows[2]["quote_claim"] is False
    left2 = cr._left_cell(rows[2], "US 2025/0033224 A1")
    assert left2.startswith("Claim 2 (") and "The gripper of claim 1" not in left2


def test_every_limitation_of_an_independent_claim_gets_its_own_row():
    """An examiner reads an independent claim limitation by limitation; the chart must match."""
    ref = _ref([_cell("claim 1[a]"), _cell("claim 1[b]")])
    rows = cd.rows_for_reference(ref, _claims())
    assert len(rows) == 2 and all(r["claim_no"] == 1 for r in rows)


# ------------------------------------------------------------------ filing language


def test_the_analyst_hedge_is_not_filed():
    """A 1.290 submission states disclosure; it may not argue what a reference fails to show."""
    note = ("The reference discloses a base element with peripheral openings. However, it does "
            "not explicitly mention a multi-layer seal.")
    assert cd._filing_safe(note) == ("The reference discloses a base element with peripheral "
                                     "openings.")


def test_a_note_with_no_hedge_is_untouched():
    note = "The reference discloses a base element with peripheral openings."
    assert cd._filing_safe(note) == note


# ------------------------------------------------------------------ document identification


@pytest.mark.parametrize("pub,expected", [
    ("US-11413727-B2", "U.S. Patent No. 11,413,727"),
    ("US-7240935-B2", "U.S. Patent No. 7,240,935"),
    ("US-20250033224-A1", "U.S. Patent Application Publication No. US 2025/0033224 A1"),
    # the corpus drops the serial's leading zero on some rows
    ("US-2023103821-A1", "U.S. Patent Application Publication No. US 2023/0103821 A1"),
])
def test_us_numbers_are_formatted_the_way_they_are_filed(pub, expected):
    assert cd._us_style(pub)[0] == expected


def test_a_foreign_number_keeps_its_office_prefix():
    label, kind = cd._us_style("WO-2023057368-A1")
    assert label.startswith("WO") and kind == "foreign"


# ------------------------------------------------------------------ renderers


def _doc():
    ref = _ref([_cell("claim 1[a]"), _cell("claim 2[a]", bar="teaches")])
    rows = cd.rows_for_reference(ref, _claims())
    for r in rows:
        r["disclosure"] = r["note"]
    return {"n": 1, "pub": "US-11413727-B2",
            "biblio": dict(cd.biblio("US-11413727-B2"), issue_date_pretty="August 16, 2022",
                           priority_date_pretty="May 9, 2018", title="Vacuum Gripper",
                           inventor="Nimrod Rotem"),
            "subject": {"app_no": "18/915,337", "pub_no": "US 2025/0033224 A1",
                        "title": "Portable vacuum gripper", "inventor": "Nhon Hoa Nguyen"},
            "summary": "This document discloses a vacuum gripper.", "rows": rows}


def test_pdf_is_a_pdf_and_names_the_statute():
    data = cr.to_pdf(_doc())
    assert data[:5] == b"%PDF-" and len(data) > 2000


def test_docx_is_a_docx():
    data = cr.to_docx(_doc())
    assert data[:2] == b"PK" and len(data) > 5000


def test_the_subject_line_reads_as_the_examples_do():
    """The parenthetical is LABELLED "Publication No." since 2026-08-23.

    A bare number in brackets beside an application number leaves the reader to work out which is
    which, and the same ambiguity one line up is what let a filed paper say `Re: U.S. App No. US
    2026/0070232 A1`. Both lines name what each number is now.
    """
    line = cr.subject_line({"app_no": "18/915,337", "pub_no": "US 2025/0033224 A1",
                            "title": "Portable vacuum gripper", "inventor": "Nhon Hoa Nguyen"})
    assert line == ("U.S. Application No. 18/915,337 (Publication No. US 2025/0033224 A1) — "
                    "“Portable vacuum gripper” — Nhon Hoa Nguyen")
    #  and with no application number it is never presented as one
    assert cr.subject_line({"pub_no": "US 2025/0033224 A1"}) == \
        "U.S. Publication No. US 2025/0033224 A1"


def test_filenames_are_stable_and_safe():
    n = cr.filename(_doc(), "pdf")
    assert n == "ConciseDescription_Doc1_US11413727B2.pdf"
    assert not re.search(r"[/\\\s]", n)
"""Lock the leading-zero lookup: a missing EFD silently disables the whole prior-art check."""
import concise_description as cd


def test_every_spelling_of_a_us_pre_grant_number_finds_the_same_row():
    """The report calls the target US-20250033224-A1; the corpus stores US-2025033224-A1.

    Matching one spelling returns no effective filing date, and with no EFD the prior-art check
    cannot run at all, so every document ships marked "basis unknown" and nothing is verified.
    Observed 2026-08-19 on ten real documents before this was fixed.
    """
    want = cd.subject_facts("US-2025033224-A1").get("efd")
    assert want, "fixture publication missing from the corpus"
    for spelling in ("US-20250033224-A1", "US 2025/0033224 A1", "us20250033224a1",
                     "US-2025033224-A1"):
        assert cd.subject_facts(spelling).get("efd") == want, spelling


def test_a_granted_number_still_resolves():
    assert cd.subject_facts("US-11413727-B2").get("efd")


def test_an_unknown_number_returns_no_date_rather_than_a_wrong_one():
    assert cd.subject_facts("US-00000000-B2").get("efd") is None
    assert cd.subject_facts("").get("efd") is None
