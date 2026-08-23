"""The drafting conversation: the checks, the workspace, the validator and the filing package.

Every test here is anchored on a defect that a machine-written patent application actually
produces — a numeral used and never defined, a claim depending on a claim that does not exist, a
citation to a publication that resolves to nothing, an abstract over the 150-word cap.  A check
that has never been shown to bite on a real failure is a check nobody should trust, so each one is
defect-injected: the good draft passes, and the same draft with one thing broken fails on exactly
that thing and nothing else.
"""
import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

import draft_agent
import draft_cite
import draft_export
import draft_figures
import draft_qa
import draft_studio
import draft_uspto
import draft_workspace
import drafting


# =============================================================================================
# A small, internally consistent application to break in one place at a time
# =============================================================================================
GOOD = {
    "title": "Vacuum Lifting Tool With Interchangeable Sealing Ring",
    "cross_reference": "This application claims no priority.",
    "field": "The disclosure relates to portable vacuum lifting tools.",
    "background": "Handheld vacuum lifters are known [REF:US-11223344-B2]. Such tools use a "
                  "single fixed seal, which limits the surfaces they can grip.",
    "summary": "A vacuum lifting tool has a body carrying a pump and a sealing ring that is "
               "removable from the body without a tool.",
    "drawing_descriptions": "FIG. 1 is a side elevation of the vacuum lifting tool.\n\n"
                            "FIG. 2 is an exploded view of the sealing ring and the body.",
    "detailed_description": "Referring to FIG. 1, a vacuum lifting tool 10 has a body 12 that "
                            "carries a pump 14. A sealing ring 16 is received in a groove 18 in "
                            "the body 12. As shown in FIG. 2, the sealing ring 16 is removable "
                            "from the groove 18 by hand, so that a ring suited to a rough "
                            "surface may replace one suited to glass. The pump 14 draws air "
                            "through a passage 20 in the body 12.",
    "claims": "1. A vacuum lifting tool comprising a body, a pump carried by the body, a groove "
              "in the body, and a sealing ring received in the groove and removable from the "
              "groove without a tool.\n\n"
              "2. The vacuum lifting tool of claim 1, wherein the body defines a passage between "
              "the pump and the groove.\n\n"
              "3. The vacuum lifting tool of claim 2, wherein the pump is battery powered.",
    "abstract": "A vacuum lifting tool has a body carrying a pump and a sealing ring received in "
                "a groove in the body. The sealing ring is removable by hand so a ring suited to "
                "one surface can replace a ring suited to another.",
}
NUMERALS = [
    {"numeral": "10", "part": "vacuum lifting tool"}, {"numeral": "12", "part": "body"},
    {"numeral": "14", "part": "pump"}, {"numeral": "16", "part": "sealing ring"},
    {"numeral": "18", "part": "groove"}, {"numeral": "20", "part": "passage"},
]
FIGURES = [
    {"label": "FIG. 1", "caption": "side elevation", "numerals": ["10 vacuum lifting tool",
                                                                  "12 body", "14 pump"]},
    {"label": "FIG. 2", "caption": "exploded view", "numerals": ["16 sealing ring", "18 groove",
                                                                    "20 passage"]},
]
ALLOWED = ["US-11223344-B2"]


@pytest.fixture(autouse=True)
def no_corpus(monkeypatch):
    """Citations resolve against a stub, so these tests never touch Postgres or a paid API."""
    known = {"US-11223344-B2": {"found": True, "publication_number": "US-11223344-B2",
                                "title": "Handheld vacuum lifter", "source": "corpus",
                                "publication_date": "2021-01-05", "kind_code": "B2",
                                "assignee": "Example Co", "url": "https://example.test/1"}}
    monkeypatch.setattr(draft_cite, "resolve",
                        lambda pub, **k: dict(known.get(draft_cite.normalize(pub) or "",
                                                        {"found": False,
                                                         "publication_number": str(pub),
                                                         "reason": "not in the local corpus"})))


def checks_for(sections=None, numerals=None, figures=None, allowed=ALLOWED):
    return {c["name"]: c for c in draft_qa.run_checks(
        sections=sections or GOOD, numerals=NUMERALS if numerals is None else numerals,
        figures=FIGURES if figures is None else figures, allowed_references=allowed,
        allow_remote=False)}


# =============================================================================================
# The clean draft passes
# =============================================================================================
def test_a_consistent_draft_passes_every_mechanical_check():
    checks = checks_for()
    failed = [name for name, check in checks.items() if check["status"] == "fail"]
    assert failed == [], f"a consistent draft should not fail anything: {failed}"
    assert draft_qa.verdict_for(list(checks.values()), []) in ("pass", "warn")


def test_verdict_never_fails_on_an_advisory_check_alone():
    """A heuristic must not be able to condemn a draft — see the calibration note in draft_qa."""
    advisory = [{"name": "Antecedent basis", "status": "fail", "severity": "advisory",
                 "detail": "", "items": []}]
    assert draft_qa.verdict_for(advisory, []) == "warn"
    proven = [{"name": "Numerals", "status": "fail", "severity": "error", "detail": "",
               "items": []}]
    assert draft_qa.verdict_for(proven, []) == "fail"


# =============================================================================================
# Reference numerals
# =============================================================================================
def test_a_numeral_used_but_never_defined_is_caught():
    broken = dict(GOOD)
    broken["detailed_description"] += " A trigger 22 is mounted on the body 12."
    check = checks_for(broken)["Every numeral in the text is defined"]
    assert check["status"] == "fail"
    assert any(item.startswith("22") for item in check["items"])


def test_two_numerals_for_the_same_part_are_caught():
    numerals = NUMERALS + [{"numeral": "24", "part": "sealing ring"}]
    broken = dict(GOOD)
    broken["detailed_description"] += " A second sealing ring 24 may be fitted."
    check = checks_for(broken, numerals=numerals)["One numeral per part"]
    assert check["status"] == "fail" and "sealing ring" in check["items"][0]


def test_every_numeral_entry_has_one_valid_number_and_one_named_part():
    broken = NUMERALS + [{"numeral": "22", "part": ""},
                         {"numeral": "12", "part": "different body"},
                         {"numeral": "part-x", "part": "invalid"}]
    check = checks_for(numerals=broken)["Every numeral-table row is complete and unique"]
    assert check["status"] == "fail"
    assert any("22" in item and "no part" in item for item in check["items"])
    assert any("12" in item and "more than once" in item for item in check["items"])
    assert any("part-x" in item and "invalid" in item for item in check["items"])


def test_a_numeral_on_a_drawing_that_the_table_does_not_define_is_caught():
    figures = [dict(FIGURES[0]), {"label": "FIG. 2", "caption": "exploded",
                                  "numerals": ["44 mystery bracket"]}]
    check = checks_for(figures=figures)["Numerals on the drawings are defined"]
    assert check["status"] == "fail" and "44" in check["items"]


def test_a_numeral_visible_on_a_drawing_but_absent_from_the_text_is_caught():
    numerals = NUMERALS + [{"numeral": "22", "part": "trigger"}]
    figures = FIGURES + [{"label": "FIG. 3", "caption": "trigger detail",
                          "numerals": ["22 trigger"]}]
    check = checks_for(numerals=numerals, figures=figures)[
        "Every drawing numeral appears in the specification"]
    assert check["status"] == "fail" and "22" in check["items"]


def test_a_text_numeral_missing_from_every_drawing_is_caught():
    figures = [{**FIGURES[0]}, {**FIGURES[1], "numerals": ["16 sealing ring", "18 groove"]}]
    check = checks_for(figures=figures)["Every specification numeral appears in a drawing"]
    assert check["status"] == "fail" and "20" in check["items"]
    assert "Do not remove a disclosed part" in check["detail"]


def test_a_reference_numeral_printed_twice_is_caught():
    figures = [{**FIGURES[0], "numerals": FIGURES[0]["numerals"] + ["10"]}, FIGURES[1]]
    check = checks_for(figures=figures)["Each drawing numeral appears once"]
    assert check["status"] == "fail" and check["items"] == ["FIG. 1: 10"]


def test_an_overcrowded_drawing_sheet_is_a_filing_blocker():
    numerals = [
        {"numeral": str(value), "part": f"part {value}"}
        for value in range(10, 28, 2)
    ]
    figures = [{
        "label": "FIG. 1", "caption": "assembly view",
        "numerals": [item["numeral"] for item in numerals],
    }]

    check = checks_for(numerals=numerals, figures=figures)[
        "Drawing sheets are not overcrowded"]

    assert check["status"] == "fail" and "9 numerals" in check["items"][0]


def test_an_overlong_drawing_brief_is_refused_before_image_generation():
    figures = [
        {**FIGURES[0], "caption": "plain rectangular body " * 160},
        FIGURES[1],
    ]
    check = checks_for(figures=figures)["Drawing briefs are concise and renderable"]
    assert check["status"] == "fail"
    assert "FIG. 1" in check["items"][0]

    with pytest.raises(
            draft_studio.FilingPreflightError,
            match="Drawing briefs are concise and renderable") as caught:
        draft_studio.validate_snapshot(
            {"sections": GOOD, "numerals": NUMERALS, "figures": figures}, ALLOWED)
    assert caught.value.category == "figures_and_numerals"


def test_letter_qualified_reference_numerals_are_compared_exactly():
    version = {**GOOD, "detailed_description":
               GOOD["detailed_description"] + " A secondary spacer 10a is beside a lug A12."}
    numerals = NUMERALS + [{"numeral": "10A", "part": "secondary spacer"},
                           {"numeral": "A12", "part": "lug"}]
    figures = [{**FIGURES[0], "numerals": FIGURES[0]["numerals"] + ["10a", "A12"]},
               FIGURES[1]]
    checks = checks_for(version, numerals=numerals, figures=figures)
    assert checks["Numerals on the drawings are defined"]["status"] == "pass"
    assert checks["Every drawing numeral appears in the specification"]["status"] == "pass"
    assert checks["Every specification numeral appears in a drawing"]["status"] == "pass"


def test_qa_uses_numerals_detected_in_the_active_drawing_pixels(monkeypatch):
    import draft_figures
    monkeypatch.setattr(draft_figures, "listing", lambda project_id, user_id: [{
        "figure_label": "FIG. 1", "active_version": 2,
        "versions": [{"version_no": 2, "detected_numerals": ["10", "44"],
                      "numeral_audit": {"inspected": True}}],
    }])
    merged = draft_studio.figures_for_qa(
        7, 91, [{"label": "FIG. 1", "caption": "view", "numerals": ["10 body", "12 pump"]}])
    assert merged[0]["numerals"] == ["10", "44"]


def test_qa_fails_closed_when_drawing_pixels_cannot_be_inspected(monkeypatch):
    import draft_figures
    monkeypatch.setattr(draft_figures, "listing", lambda project_id, user_id: [{
        "figure_label": "FIG. 1", "active_version": 2,
        "versions": [{"version_no": 2, "detected_numerals": [],
                      "numeral_audit": {"inspected": False, "error": "vision unavailable"}}],
    }])
    merged = draft_studio.figures_for_qa(
        7, 91, [{"label": "FIG. 1", "caption": "view", "numerals": ["10 body"]}])
    checks = {item["name"]: item for item in draft_qa.run_checks(
        sections=GOOD, numerals=NUMERALS, figures=merged, allow_remote=False)}
    assert checks["Drawing pixels were inspected"]["status"] == "fail"


def test_qa_fails_closed_when_drawing_geometry_does_not_match_the_spec(monkeypatch):
    import draft_figures
    monkeypatch.setattr(draft_figures, "listing", lambda project_id, user_id: [{
        "figure_label": "FIG. 1", "active_version": 2,
        "versions": [{"version_no": 2, "detected_numerals": ["10"],
                      "numeral_audit": {"inspected": True, "ok": True},
                      "semantic_audit": {"inspected": True, "ok": False,
                                         "errors": ["pump is absent"]}}],
    }])
    merged = draft_studio.figures_for_qa(
        7, 91, [{"label": "FIG. 1", "caption": "view", "numerals": ["10 body"]}])
    checks = {item["name"]: item for item in draft_qa.run_checks(
        sections=GOOD, numerals=NUMERALS, figures=merged, allow_remote=False)}
    assert checks["Drawing content matches its specification"]["status"] == "fail"


def test_qa_fails_closed_when_a_printed_leader_does_not_reach_its_named_feature(monkeypatch):
    import draft_figures
    monkeypatch.setattr(draft_figures, "listing", lambda project_id, user_id: [{
        "figure_label": "FIG. 1", "active_version": 2,
        "versions": [{"version_no": 2, "detected_numerals": ["10"],
                      "numeral_audit": {"inspected": True, "ok": True},
                      "semantic_audit": {"inspected": True, "ok": True},
                      "leader_audit": {"inspected": True, "ok": False,
                                         "errors": ["10 ends in blank space"]}}],
    }])
    merged = draft_studio.figures_for_qa(
        7, 91, [{"label": "FIG. 1", "caption": "view", "numerals": ["10 body"]}])
    checks = {item["name"]: item for item in draft_qa.run_checks(
        sections=GOOD, numerals=NUMERALS, figures=merged, allow_remote=False)}
    assert checks["Drawing leaders identify the named features"]["status"] == "fail"
    assert "blank space" in checks["Drawing leaders identify the named features"]["items"][0]


def test_qa_fails_closed_when_the_drawing_store_is_unavailable(monkeypatch):
    import draft_figures

    def unavailable(project_id, user_id):
        raise RuntimeError("database offline")

    monkeypatch.setattr(draft_figures, "listing", unavailable)
    merged = draft_studio.figures_for_qa(
        7, 91, [{"label": "FIG. 1", "caption": "view", "numerals": ["10 body"]}])
    checks = {item["name"]: item for item in draft_qa.run_checks(
        sections=GOOD, numerals=NUMERALS, figures=merged, allow_remote=False)}
    assert checks["Drawing pixels were inspected"]["status"] == "fail"
    assert merged[0]["numerals"] == []


def test_an_undrawn_figure_spec_is_not_counted_as_visible_pixels(monkeypatch):
    import draft_figures
    monkeypatch.setattr(draft_figures, "listing", lambda project_id, user_id: [])
    merged = draft_studio.figures_for_qa(
        7, 91, [{"label": "FIG. 1", "caption": "view", "numerals": ["10 body"]}])
    assert merged == [{"label": "FIG. 1", "caption": "view", "numerals": [], "drawn": False}]
    checks = {item["name"]: item for item in draft_qa.run_checks(
        sections=GOOD, numerals=NUMERALS, figures=merged, allow_remote=False)}
    assert checks["Every specification numeral appears in a drawing"]["status"] == "fail"
    assert checks["Each described figure has a drawing sheet"]["status"] == "fail"
    assert checks["Each described figure has a drawing sheet"]["severity"] == "error"


def test_an_application_without_a_drawing_plan_cannot_pass_the_filing_gate():
    sections = {**GOOD, "drawing_descriptions": "Not applicable.",
                "detailed_description": re.sub(r"\bFIG\.\s*\d+\b", "the drawings",
                                                GOOD["detailed_description"])}
    check = checks_for(sections, figures=[])["Application includes a drawing plan"]
    assert check["status"] == "fail" and check["severity"] == "error"


def test_an_orphaned_drawing_remains_in_bidirectional_qa(monkeypatch):
    import draft_figures
    monkeypatch.setattr(draft_figures, "listing", lambda project_id, user_id: [{
        "figure_label": "FIG. 9", "caption": "obsolete", "active_version": 1,
        "versions": [{"version_no": 1, "detected_numerals": ["44"],
                      "numeral_audit": {"inspected": True}}],
    }])
    merged = draft_studio.figures_for_qa(7, 91, [])
    assert merged[0]["orphan"] is True and merged[0]["numerals"] == ["44"]
    checks = {item["name"]: item for item in draft_qa.run_checks(
        sections=GOOD, numerals=NUMERALS, figures=merged, allow_remote=False)}
    assert checks["Every drawing sheet is described"]["status"] == "fail"
    assert checks["Numerals on the drawings are defined"]["items"] == ["44"]


def test_measurements_and_years_are_not_read_as_reference_numerals():
    """The generous failure here would report every dimension as an undefined part."""
    text = ("The body 12 is 45 mm long, weighs 2.5 kg, holds 90 percent vacuum at 25 degrees, "
            "and was first described in 2019. See FIG. 3 and claim 7.")
    assert set(draft_qa.numerals_used(text)) == {"12"}


# =============================================================================================
# Figures
# =============================================================================================
def test_a_figure_used_in_the_description_but_never_described_is_caught():
    broken = dict(GOOD)
    broken["detailed_description"] += " FIG. 4 shows an alternative pump."
    check = checks_for(broken)["Every figure used is described"]
    assert check["status"] == "fail" and "FIG. 4" in check["items"]


def test_a_figure_range_counts_every_figure_in_it():
    assert draft_qa.figures_mentioned("As shown in FIGS. 1-3 and FIG. 7") == {"1", "2", "3", "7"}


# =============================================================================================
# Claims
# =============================================================================================
def test_a_claim_depending_on_a_claim_that_does_not_exist_is_caught():
    broken = dict(GOOD)
    broken["claims"] += "\n\n4. The vacuum lifting tool of claim 9, wherein the body is aluminium."
    check = checks_for(broken)["Claim dependencies are valid"]
    assert check["status"] == "fail" and "claim 9" in " ".join(check["items"])


def test_a_claim_depending_on_a_later_claim_is_caught():
    broken = dict(GOOD)
    broken["claims"] = broken["claims"].replace(
        "2. The vacuum lifting tool of claim 1,", "2. The vacuum lifting tool of claim 3,")
    assert checks_for(broken)["Claim dependencies are valid"]["status"] == "fail"


def test_claims_must_be_numbered_consecutively_from_one():
    broken = dict(GOOD)
    broken["claims"] = broken["claims"].replace("3. The vacuum", "5. The vacuum")
    assert checks_for(broken)["Claims are numbered consecutively"]["status"] == "fail"


def test_a_multiple_dependent_claim_on_another_is_a_rule_violation():
    broken = dict(GOOD)
    broken["claims"] += ("\n\n4. The vacuum lifting tool of any one of claims 1 or 2, wherein the "
                         "groove is annular."
                         "\n\n5. The vacuum lifting tool of any one of claims 3 or 4, wherein the "
                         "body is aluminium.")
    checks = checks_for(broken)
    assert checks["No multiple dependent claim depends on another"]["status"] == "fail"


def test_claim_dependency_parsing_handles_the_forms_attorneys_write():
    assert draft_qa.claim_dependencies("The tool of claim 1, wherein") == [1]
    assert draft_qa.claim_dependencies("The tool according to claim 2") == [2]
    assert draft_qa.claim_dependencies("The tool as set forth in claim 3") == [3]
    assert draft_qa.claim_dependencies("The tool of any one of claims 1 to 3") == [1, 2, 3]
    assert draft_qa.claim_dependencies("The tool of claims 1 or 4") == [1, 4]
    assert draft_qa.claim_dependencies("A tool comprising a body") == []


def test_antecedent_basis_is_advisory_and_finds_a_real_miss():
    broken = dict(GOOD)
    broken["claims"] = ("1. A vacuum lifting tool comprising a body and a pump carried by the "
                        "body, wherein the sealing ring is received in the body.")
    check = checks_for(broken)["Antecedent basis in the claims"]
    assert check["severity"] == "advisory"
    assert check["status"] == "warn" and any("sealing ring" in i for i in check["items"])


def test_a_claim_term_absent_from_the_description_is_flagged_but_cannot_fail_the_draft():
    broken = dict(GOOD)
    broken["claims"] += "\n\n4. The vacuum lifting tool of claim 1, wherein the body is titanium."
    check = checks_for(broken)["Claim terms appear in the description"]
    assert check["severity"] == "advisory" and check["status"] == "warn"
    assert any("titanium" in item for item in check["items"])


# =============================================================================================
# Citations
# =============================================================================================
def test_a_citation_that_resolves_to_nothing_is_caught():
    broken = dict(GOOD)
    broken["background"] += " A second lifter is described in [REF:US-9999999-B9]."
    check = checks_for(broken, allowed=ALLOWED + ["US-9999999-B9"])[
        "Every citation resolves to a real publication"]
    assert check["status"] == "fail" and "US-9999999-B9" in check["items"][0]


def test_a_citation_to_a_reference_the_project_was_never_given_is_caught():
    broken = dict(GOOD)
    broken["background"] += " See [REF:US-11223344-B2] and [REF:EP-1234567-A1]."
    check = checks_for(broken)["Citations are to supplied references"]
    assert check["status"] == "fail" and "EP-1234567-A1" in check["items"]


def test_a_citation_in_the_claims_is_a_drafting_error():
    broken = dict(GOOD)
    broken["claims"] = broken["claims"].replace(
        "1. A vacuum", "1. A vacuum lifter improving on [REF:US-11223344-B2]. A vacuum")
    check = checks_for(broken)["Citations sit where they belong"]
    assert check["status"] == "fail" and "Claims" in check["items"][0]


def test_a_malformed_citation_token_is_caught_rather_than_normalised():
    broken = dict(GOOD)
    broken["background"] += " See [REF:the Smith patent]."
    assert checks_for(broken)["Citation tokens are well formed"]["status"] == "fail"


def test_a_publication_number_written_without_a_token_is_surfaced():
    broken = dict(GOOD)
    broken["background"] += " US 7,654,321 B1 describes a related tool."
    check = checks_for(broken)["Publication numbers use citation tokens"]
    assert check["status"] == "warn" and "US-7654321-B1" in check["items"][0]


def test_supplied_art_that_is_never_cited_is_reported_without_failing():
    check = checks_for(allowed=ALLOWED + ["US-8888888-B2"])["Supplied art is addressed"]
    assert check["status"] == "warn" and "US-8888888-B2" in check["items"]


def test_citation_token_extraction():
    assert draft_cite.citations_in("a [REF:US-1-A] b [REF:EP-2222222-A1]") == ["US-1-A",
                                                                              "EP-2222222-A1"]
    assert draft_cite.malformed_citations_in("see [REF:the thing]") == ["the thing"]


# =============================================================================================
# Formalities
# =============================================================================================
def test_an_abstract_over_the_word_cap_fails():
    broken = dict(GOOD)
    broken["abstract"] = " ".join(["word"] * 200)
    check = checks_for(broken)["Abstract is in filing form"]
    assert check["status"] == "fail" and "200 words" in check["detail"]


def test_an_abstract_in_two_paragraphs_is_a_warning_not_a_failure():
    broken = dict(GOOD)
    broken["abstract"] = "First paragraph of the abstract.\n\nSecond paragraph."
    check = checks_for(broken)["Abstract is in filing form"]
    assert check["status"] == "warn" and "single paragraph" in check["detail"]


def test_a_puffed_title_is_objected_to():
    broken = dict(GOOD)
    broken["title"] = "New and Improved Vacuum Lifting Tool"
    assert checks_for(broken)["Title is in filing form"]["status"] == "warn"


def test_an_empty_section_fails():
    broken = dict(GOOD)
    broken["summary"] = ""
    assert checks_for(broken)["Every section is written"]["status"] == "fail"


def test_open_drafting_notes_are_a_filing_blocker():
    broken = dict(GOOD)
    broken["detailed_description"] += " [DRAFTING NOTE: confirm the ring material.]"
    check = checks_for(broken)["No unresolved drafting notes"]
    assert check["status"] == "fail" and check["severity"] == "error"
    assert "ring material" in check["items"][0]


@pytest.mark.parametrize("placeholder", [
    "[TODO: add dimensions]", "TBD", "TO BE PROVIDED", "<INSERT MATERIAL>",
    "{{applicant_name}}", "_______", "Part names are for the draftsperson only.",
])
def test_every_placeholder_form_is_a_filing_blocker(placeholder):
    broken = dict(GOOD)
    broken["summary"] += " " + placeholder
    check = checks_for(broken)["No unresolved drafting notes"]
    assert check["status"] == "fail" and check["items"]


# =============================================================================================
# Workspace
# =============================================================================================
def test_sections_survive_a_write_and_read_round_trip(tmp_path):
    draft_workspace.write_sections(tmp_path, GOOD)
    assert draft_workspace.read_sections(tmp_path) == GOOD


def test_a_heading_the_agent_added_back_is_dropped_but_real_headings_survive(tmp_path):
    draft_workspace.write_sections(tmp_path, GOOD)
    path = tmp_path / "draft" / "04-background.md"
    path.write_text("## Background\n\nHandheld vacuum lifters are known.\n", encoding="utf-8")
    detail = tmp_path / "draft" / "07-detailed-description.md"
    detail.write_text("## The pump\n\nThe pump 14 draws air.\n", encoding="utf-8")
    out = draft_workspace.read_sections(tmp_path)
    assert out["background"] == "Handheld vacuum lifters are known."
    assert out["detailed_description"].startswith("## The pump")


def test_the_numeral_table_round_trips(tmp_path):
    draft_workspace.write_numerals(tmp_path, NUMERALS)
    assert draft_workspace.read_numerals(tmp_path) == NUMERALS


def test_a_valid_numeral_table_may_carry_extra_audit_columns(tmp_path):
    directory = tmp_path / "draft"
    directory.mkdir()
    (directory / "numerals.md").write_text(
        "# Reference numerals\n\n"
        "| Numeral | Part | First introduced |\n"
        "| --- | --- | --- |\n"
        "| 10 | vacuum lifting tool | FIG. 1 |\n"
        "| 12 | body | paragraph 12 |\n",
        encoding="utf-8")
    assert draft_workspace.read_numerals(tmp_path) == [
        {"numeral": "10", "part": "vacuum lifting tool"},
        {"numeral": "12", "part": "body"},
    ]


def test_figures_round_trip(tmp_path):
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "agent-created.svg").write_text("<svg/>", encoding="utf-8")
    (figures / "rendered-stale.png").write_bytes(b"stale")
    draft_workspace.write_figures(tmp_path, FIGURES)
    out = draft_workspace.read_figures(tmp_path)
    assert [f["label"] for f in out] == ["FIG. 1", "FIG. 2"]
    assert out[1]["numerals"] == ["16 sealing ring", "18 groove", "20 passage"]
    assert not (figures / "agent-created.svg").exists()
    assert not (figures / "rendered-stale.png").exists()


def test_an_existing_draft_is_split_on_its_headings():
    document = ("Vacuum Lifting Tool\n\nBACKGROUND OF THE INVENTION\n\nLifters are known.\n\n"
                "SUMMARY OF THE INVENTION\n\nA better lifter.\n\n"
                "DETAILED DESCRIPTION\n\nThe tool 10 has a body 12.\n\n"
                "WHAT IS CLAIMED IS:\n\n1. A tool.\n\nABSTRACT\n\nA tool.")
    out = draft_workspace.seed_sections_from_document(document)
    assert out["title"] == "Vacuum Lifting Tool"
    assert out["background"] == "Lifters are known."
    assert out["claims"] == "1. A tool."
    assert "body 12" in out["detailed_description"]


def test_a_document_with_no_recognisable_headings_is_kept_whole_not_dropped():
    out = draft_workspace.seed_sections_from_document("Just a paragraph about a tool.")
    assert out == {"detailed_description": "Just a paragraph about a tool."}


def test_numerals_are_harvested_from_an_existing_draft():
    found = {item["numeral"]: item["part"]
             for item in draft_workspace.numerals_from_sections(
                 {"detailed_description": "The vacuum lifting tool 10 has a body 12."})}
    assert found["10"].endswith("tool") and found["12"] == "body"


def test_the_workspace_lives_outside_the_home_directory_or_says_why():
    #  Claude Code collects CLAUDE.md files walking up from its working directory; a workspace
    #  under the operator's home would inherit that box's own operating instructions.
    assert draft_workspace.DEFAULT_ROOT.startswith("/srv") or \
        "DRAFT_WORKSPACE_ROOT" in Path(draft_workspace.__file__).read_text()


def test_the_lookup_tool_is_written_with_a_resolvable_source_path(tmp_path):
    draft_workspace.install_tools(tmp_path, src_dir="/opt/app/src")
    body = (tmp_path / "tools" / "patent_lookup.py").read_text()
    assert '"/opt/app/src"' in body and "draft_cite" in body
    compile(body, "patent_lookup.py", "exec")


# =============================================================================================
# The validator that decides whether a turn becomes a version
# =============================================================================================
def test_a_good_draft_validates():
    assert draft_studio.validate_sections(GOOD, ALLOWED)["title"] == GOOD["title"]


def test_an_empty_section_is_refused():
    with pytest.raises(Exception) as caught:
        draft_studio.validate_sections({**GOOD, "claims": ""}, ALLOWED)
    assert "Claims" in str(caught.value)


def test_a_citation_to_an_unsupplied_reference_is_refused():
    broken = {**GOOD, "background": GOOD["background"] + " [REF:US-4444444-A]"}
    with pytest.raises(Exception) as caught:
        draft_studio.validate_sections(broken, ALLOWED)
    assert "US-4444444-A" in str(caught.value)


def test_a_citation_is_refused_when_the_project_has_no_supplied_references():
    broken = {**GOOD, "background": GOOD["background"] + " [REF:US-11223344-B2]"}
    with pytest.raises(Exception) as caught:
        draft_studio.validate_sections(broken, [])
    assert "not among this project's sources" in str(caught.value)


def test_a_legal_conclusion_is_refused():
    broken = {**GOOD, "summary": GOOD["summary"] + " The claimed tool is clearly non-obvious."}
    with pytest.raises(Exception) as caught:
        draft_studio.validate_sections(broken, ALLOWED)
    assert "legal conclusion" in str(caught.value)


def test_a_placeholder_is_refused_before_a_version_can_be_saved():
    broken = {**GOOD, "cross_reference": "[DRAFTING NOTE: confirm priority.]"}
    with pytest.raises(Exception) as caught:
        draft_studio.validate_sections(broken, ALLOWED)
    assert "placeholder" in str(caught.value).lower()


@pytest.mark.parametrize("field,value", [
    ("numerals", [{"numeral": "10", "part": "TBD component"}]),
    ("figures", [{"label": "FIG. 1", "caption": "<INSERT VIEW>", "numerals": ["10"]}]),
    ("figures", [{"label": "FIG. 1",
                  "caption": "Part names are for the draftsperson only.",
                  "numerals": ["10"]}]),
])
def test_placeholders_outside_the_sections_are_refused_before_save(field, value):
    snapshot = {"sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}
    snapshot[field] = value
    with pytest.raises(draft_studio.FilingPreflightError, match="placeholder") as caught:
        draft_studio.validate_snapshot(snapshot, ALLOWED)
    assert caught.value.category == "figures_and_numerals"


def test_overcrowded_sheet_is_refused_before_any_image_call():
    snapshot = {
        "sections": GOOD,
        "numerals": [
            {"numeral": str(value), "part": f"part {value}"}
            for value in range(10, 28, 2)
        ],
        "figures": [{
            "label": "FIG. 1", "caption": "assembly view",
            "numerals": [str(value) for value in range(10, 28, 2)],
        }],
    }

    with pytest.raises(draft_studio.FilingPreflightError,
                       match="more than 8 numerals") as caught:
        draft_studio.validate_snapshot(snapshot, ALLOWED)
    assert caught.value.category == "figures_and_numerals"


def test_a_text_numeral_missing_from_every_figure_is_refused_before_any_image_call():
    figures = [dict(FIGURES[0]), {**FIGURES[1], "numerals": ["16", "18"]}]
    snapshot = {"sections": GOOD, "numerals": NUMERALS, "figures": figures}

    with pytest.raises(
            draft_studio.FilingPreflightError,
            match="Every specification numeral appears in a drawing") as caught:
        draft_studio.validate_snapshot(snapshot, ALLOWED)
    assert caught.value.category == "figures_and_numerals"


def test_agent_filing_artifacts_are_normalized_for_human_facing_text():
    numerals = [dict(item) for item in NUMERALS]
    numerals[0]["part"] = "vacuum lifting tool\u2014assembly"
    figures = [{**item, "numerals": list(item["numerals"])} for item in FIGURES]
    figures[0]["caption"] = "side\u2014elevation"
    snapshot = {
        "sections": {**GOOD, "summary": GOOD["summary"] + " One view\u2014shown below."},
        "numerals": numerals,
        "figures": figures,
    }
    clean = draft_studio.validate_snapshot(snapshot, ALLOWED)
    assert "\u2014" not in json.dumps(clean, ensure_ascii=False)
    assert "One view - shown below." in clean["sections"]["summary"]
    assert clean["numerals"][0]["part"] == "vacuum lifting tool - assembly"
    assert clean["figures"][0]["caption"] == "side - elevation"


def test_a_citation_in_the_detailed_description_is_allowed_here():
    """Incorporation by reference is real practice; the reviewer judges it, the validator does not
    throw fifteen minutes of drafting away over it."""
    ok = {**GOOD, "detailed_description":
          GOOD["detailed_description"] + " US-11223344-B2 is incorporated by reference "
          "[REF:US-11223344-B2]."}
    assert draft_studio.validate_sections(ok, ALLOWED)


def test_citations_are_collected_in_first_use_order():
    sections = {**GOOD, "detailed_description":
                GOOD["detailed_description"] + " [REF:US-11223344-B2]"}
    assert draft_studio.citations_of(sections) == ["US-11223344-B2"]


def test_the_rendered_markdown_keeps_the_application_order():
    rendered = draft_studio.render_markdown(GOOD)
    positions = [rendered.index(heading) for _k, _n, heading in draft_workspace.SECTION_FILES]
    assert positions == sorted(positions)


# =============================================================================================
# Filing
# =============================================================================================
def test_the_fee_profile_counts_a_multiple_dependent_claim_as_several():
    profile = draft_uspto.fee_profile(
        GOOD["claims"] + "\n\n4. The tool of any one of claims 1 to 3, wherein it is red.")
    assert profile["total"] == 4 and profile["independent"] == 1
    assert profile["multiple_dependent"] == 1 and profile["billable"] == 6
    assert any("multiple dependent" in s for s in profile["surcharges"])


def test_more_than_three_independent_claims_triggers_a_surcharge():
    claims = "\n\n".join(f"{n}. An apparatus comprising a part {n}." for n in range(1, 6))
    assert any("independent" in s for s in draft_uspto.fee_profile(claims)["surcharges"])


def checked_figures(*labels):
    labels = labels or tuple(spec["label"] for spec in FIGURES)
    out = []
    for label in labels:
        spec = next(item for item in FIGURES
                    if draft_figures.figure_key(item["label"]) == draft_figures.figure_key(label))
        expected = draft_figures.expected_entries(spec, NUMERALS)
        digest = draft_figures.specification_hash(spec["label"], spec["caption"], expected)
        out.append({"figure_label": label, "active_version": 1, "versions": [{
            "version_no": 1, "numeral_audit": {"ok": True},
            "leader_audit": {
                "ok": True, "inspected": True, "specification_hash": digest,
                "model_name": draft_figures.vision_model(),
                "prompt_version": draft_figures.LEADER_PROMPT_VERSION,
                "review_count": draft_figures.LEADER_REVIEW_COUNT,
            },
            "semantic_audit": {
                "ok": True, "inspected": True, "specification_hash": digest,
                "model_name": draft_figures.vision_model(),
                "prompt_version": draft_figures.SEMANTIC_PROMPT_VERSION,
                "review_count": draft_figures.SEMANTIC_REVIEW_COUNT,
                "pixel_anchor_audit": {
                    "ok": True, "inspected": True,
                    "version": draft_figures.PIXEL_ANCHOR_VERSION,
                },
                "topology_audit": {
                    "ok": True, "inspected": False, "required": False,
                    "version": draft_figures.CLOSED_REGION_AUDIT_VERSION,
                },
                "marked_anchor_audit": {
                    "ok": True, "inspected": True, "specification_hash": digest,
                    "model_name": draft_figures.vision_model(),
                    "prompt_version": draft_figures.MARKED_ANCHOR_PROMPT_VERSION,
                    "review_count": draft_figures.MARKED_ANCHOR_REVIEW_COUNT,
                },
            }}]})
    return out


def clean_version(version_no=1, sections=None):
    return {"version_no": version_no, "sections": sections or GOOD,
            "citations": ["US-11223344-B2"], "numerals": NUMERALS,
            "figure_specs": FIGURES}


def clean_qa(version_no=1):
    return {"version_no": version_no, "status": "complete", "verdict": "pass",
            "checks": [], "findings": []}


def test_readiness_blocks_on_an_open_drafting_note_and_on_a_failed_check():
    version = clean_version(sections={**GOOD, "summary":
                                    GOOD["summary"] + " [DRAFTING NOTE: get the ring material.]"})
    qa = {"version_no": 1, "status": "complete", "verdict": "fail",
          "checks": [{"name": "Every numeral in the text is defined",
                                         "status": "fail", "severity": "error",
                                         "detail": "22 is undefined", "items": ["22"]}],
          "findings": []}
    report = draft_uspto.readiness(project={"inventors": "Dana Drafter", "applicant": "Example"},
                                   version=version, qa=qa, figures=checked_figures())
    titles = [b["title"] for b in report["blockers"]]
    assert not report["ready"]
    assert any("drafting note" in t for t in titles)
    assert "Every numeral in the text is defined" in titles


def test_readiness_blocks_when_no_inventor_is_named():
    report = draft_uspto.readiness(project={"inventors": "", "applicant": ""},
                                   version=clean_version(),
                                   qa=clean_qa(), figures=checked_figures())
    assert any("inventor" in b["title"].lower() for b in report["blockers"])


def test_an_unreviewed_draft_is_never_reported_as_ready():
    report = draft_uspto.readiness(project={"inventors": "Dana", "applicant": "Example"},
                                   version=clean_version(),
                                   qa=None, figures=[{"id": 1}])
    assert not report["ready"]


def test_a_clean_draft_reports_no_blockers_but_still_lists_what_a_person_must_do():
    report = draft_uspto.readiness(project={"inventors": "Dana", "applicant": "Example"},
                                   version=clean_version(),
                                   qa=clean_qa(), figures=checked_figures())
    assert report["ready"] and len(report["remaining"]) >= 5
    assert any("oath or declaration" in item for item in report["remaining"])


def test_readiness_rechecks_the_active_drawing_instead_of_trusting_old_qa():
    figures = [{"figure_label": "FIG. 1", "active_version": 2, "versions": [{
        "version_no": 2, "numeral_audit": {"ok": True},
        "semantic_audit": {"ok": False}}]}]
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"},
        version=clean_version(),
        qa=clean_qa(), figures=figures)
    assert not report["ready"]
    assert any("active drawings" in item["title"] for item in report["blockers"])


def test_readiness_blocks_a_sheet_without_final_leader_placement_approval():
    figures = checked_figures()
    figures[0]["versions"][0]["leader_audit"] = {
        "inspected": True, "ok": False, "errors": ["12 points to the body"]}
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"},
        version=clean_version(), qa=clean_qa(), figures=figures)
    assert not report["ready"]
    assert any("leader placement" in item["items"] for item in report["blockers"])


def test_readiness_rejects_a_leader_review_from_an_older_gate():
    figures = checked_figures()
    figures[0]["versions"][0]["leader_audit"]["prompt_version"] = "old"
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"},
        version=clean_version(), qa=clean_qa(), figures=figures)
    assert not report["ready"]
    assert any("leader" in item["items"] for item in report["blockers"])


def test_readiness_rejects_a_semantic_review_from_an_older_gate():
    figures = checked_figures()
    figures[0]["versions"][0]["semantic_audit"]["prompt_version"] = "old"
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"},
        version=clean_version(), qa=clean_qa(), figures=figures)
    assert not report["ready"]
    assert any("semantic" in item["items"] for item in report["blockers"])


def test_readiness_requires_review_for_the_exact_exported_version():
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"},
        version=clean_version(version_no=2),
        qa=clean_qa(version_no=1), figures=checked_figures())
    assert not report["ready"]
    assert any("exact version" in item["title"].lower() for item in report["blockers"])


def test_readiness_blocks_every_review_finding_and_nonpassing_check():
    qa = clean_qa() | {
        "verdict": "warn",
        "checks": [{"name": "Ambiguous term", "status": "warn", "severity": "advisory",
                    "detail": "The term may be unclear.", "items": []}],
        "findings": [{"severity": "minor", "title": "Description drift",
                      "detail": "One phrase differs.", "where": "summary"}],
    }
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"},
        version=clean_version(),
        qa=qa, figures=checked_figures())
    titles = [item["title"] for item in report["blockers"]]
    assert not report["ready"]
    assert "Ambiguous term" in titles and "Description drift" in titles


def test_readiness_rejects_todos_and_missing_or_mismatched_drawing_audits():
    version = clean_version(sections={**GOOD, "summary": GOOD["summary"] + " TODO"})
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"}, version=version,
        qa=clean_qa(), figures=[{"figure_label": "FIG. 2", "active_version": 0}])
    titles = [item["title"].lower() for item in report["blockers"]]
    assert not report["ready"]
    assert any("unfinished" in title for title in titles)
    assert any("drawing" in title for title in titles)


def test_readiness_rejects_a_previous_sheet_for_a_changed_figure_specification():
    figures = checked_figures()
    figures[0]["versions"][0]["semantic_audit"]["specification_hash"] = "old-specification"
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"},
        version=clean_version(), qa=clean_qa(), figures=figures)
    assert not report["ready"]
    assert any("different drawing specification" in item["items"]
               for item in report["blockers"])


def test_readiness_rejects_duplicate_active_sheet_numbers():
    figures = checked_figures()
    figures.append(dict(figures[0]))
    report = draft_uspto.readiness(
        project={"inventors": "Dana", "applicant": "Example"},
        version=clean_version(), qa=clean_qa(), figures=figures)
    assert not report["ready"]
    assert any("duplicate" in item["title"].lower() for item in report["blockers"])


def test_the_filing_text_numbers_its_paragraphs_and_orders_the_parts():
    text = draft_uspto.filing_text({"title": "x"}, {"sections": GOOD})
    assert "[0001]" in text
    assert text.index("BACKGROUND OF THE INVENTION") < text.index("CLAIMS")
    assert text.index("CLAIMS") < text.index("ABSTRACT OF THE DISCLOSURE")


def test_standard_exports_contain_only_clean_application_text():
    from docx import Document
    version = {"version_no": 1, "sections": GOOD, "status": "approved"}
    markdown = draft_export.render_markdown({"title": GOOD["title"]}, version, [])
    assert "WORKING DRAFT" not in markdown
    assert "DRAFTING SOURCE TRACE" not in markdown
    document = Document(draft_export.render_docx({"title": GOOD["title"]}, version, []))
    text = "\n".join(
        [paragraph.text for paragraph in document.paragraphs] +
        [paragraph.text for section in document.sections
         for paragraph in list(section.header.paragraphs) + list(section.footer.paragraphs)])
    assert "WORKING DRAFT" not in text
    assert "attorney review" not in text.lower()
    assert "DRAFTING SOURCE TRACE" not in text
    assert "1. A vacuum lifting tool comprising" in text
    assert "3. The vacuum lifting tool of claim 2" in text


def test_filing_docx_includes_every_checked_drawing_sheet():
    import io
    from PIL import Image
    from docx import Document
    image = Image.new("RGB", (640, 420), "white")
    png = io.BytesIO()
    image.save(png, format="PNG")
    version = {"version_no": 1, "sections": GOOD, "citations": []}
    output = draft_uspto.render_filing_docx(
        {"title": GOOD["title"]}, version,
        readiness_report={"blockers": [], "formalities": [], "remaining": [],
                          "fees": {"total": 3, "independent": 1,
                                   "multiple_dependent": 0, "billable": 3}},
        figure_images=[{"label": "FIG. 1", "png": png.getvalue()}])
    document = Document(output)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert len(document.inline_shapes) == 1
    assert "DRAWING SHEETS" in text and "FIG. 1" in text
    for forbidden in ("(not supplied)", "STILL REQUIRED", "NOT READY", "legal advice"):
        assert forbidden.lower() not in text.lower()


def test_filing_docx_refuses_to_export_a_blocked_version():
    with pytest.raises(drafting.DraftingValidationError, match="filing gate"):
        draft_uspto.render_filing_docx(
            {"title": GOOD["title"]}, {"version_no": 1, "sections": GOOD},
            readiness_report={"blockers": [{"title": "Drawing mismatch"}]})


def test_paragraph_numbering_is_continuous_across_sections():
    first, cursor = draft_uspto.numbered_paragraphs("one\n\ntwo", 1)
    second, _ = draft_uspto.numbered_paragraphs("three", cursor)
    assert first[0].startswith("[0001]") and second[0].startswith("[0003]")


def test_no_dollar_amount_is_printed_anywhere_in_the_filing_module():
    """Fee amounts change by rulemaking; a number baked in here would be quietly wrong."""
    body = Path(draft_uspto.__file__).read_text()
    body = re.sub(r'""".*?"""', "", body, flags=re.S)          # docstrings may discuss the policy
    assert not re.search(r"\$\s?\d", body)


def test_the_ads_fields_never_invent_a_value():
    fields = draft_uspto.ads_fields({"inventors": "", "applicant": ""},
                                    {"sections": GOOD})
    inventor = next(f for f in fields if f["field"] == "Inventor(s)")
    assert inventor["value"] == "(not supplied)"


# =============================================================================================
# The agent bridge
# =============================================================================================
def test_a_structured_result_is_parsed_from_the_json_string_the_cli_returns():
    parsed = draft_agent._parse_result('{"action":"revised","summary":"did a thing"}')
    assert parsed["action"] == "revised"


def test_a_fenced_result_is_repaired_rather_than_thrown_away():
    assert draft_agent._parse_result('```json\n{"a":1}\n```')["a"] == 1


def test_a_result_that_is_not_an_object_is_a_failure_not_an_empty_dict():
    assert draft_agent._parse_result("no json here") is None
    assert draft_agent._parse_result("") is None


def test_tool_calls_are_summarised_to_workspace_relative_paths():
    steps = []
    draft_agent._summarize_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": "/srv/patent-drafts/p7/draft/08-claims.md"}},
            {"type": "text", "text": "narrowed claim 1"},
            {"type": "tool_use", "name": "StructuredOutput", "input": {"summary": "x"}}]}}, steps)
    assert steps[0] == {"kind": "tool", "tool": "Edit", "detail": "draft/08-claims.md"}
    assert steps[1]["kind"] == "say"
    assert len(steps) == 2, "the structured answer is the result, not a step"


def test_availability_reports_the_reason_it_cannot_run(monkeypatch):
    monkeypatch.setattr(draft_agent, "binary", lambda: "")
    state = draft_agent.availability()
    assert state["ok"] is False and "not installed" in state["reason"]


def test_strings_bounds_and_cleans_a_model_list():
    assert draft_agent.strings(["a", "", "b"]) == ["a", "b"]
    assert draft_agent.strings("single") == ["single"]
    assert len(draft_agent.strings(["x"] * 100)) == 40


# =============================================================================================
# The reviewer's own output contract
# =============================================================================================
def test_a_finding_without_evidence_is_dropped():
    """An unfalsifiable warning in a review panel is the one users learn to scroll past."""
    findings = draft_qa.normalize_findings([
        {"severity": "critical", "title": "Claim 1 unsupported", "evidence": "", "detail": "d",
         "where": "claims", "category": "claim_support", "fix": ""},
        {"severity": "major", "title": "Numeral 16 drifts", "evidence": "the ring 16 …",
         "detail": "d", "where": "detailed description", "category": "figures_and_numerals",
         "fix": "f"}])
    assert [f["title"] for f in findings] == ["Numeral 16 drifts"]


def test_findings_are_ordered_by_severity():
    findings = draft_qa.normalize_findings([
        {"severity": "minor", "title": "m", "evidence": "e", "detail": "", "where": "",
         "category": "terminology", "fix": ""},
        {"severity": "critical", "title": "c", "evidence": "e", "detail": "", "where": "",
         "category": "citations", "fix": ""}])
    assert [f["severity"] for f in findings] == ["critical", "minor"]


def test_the_review_schema_is_valid_json_schema_the_cli_will_accept():
    encoded = json.dumps(draft_qa.REVIEW_SCHEMA)
    assert '"additionalProperties": false' in encoded
    assert draft_qa.REVIEW_SCHEMA["required"] == ["summary", "findings"]
    assert json.dumps(draft_studio.TURN_SCHEMA)


def test_the_reviewer_never_resumes_the_drafting_session(monkeypatch):
    """The whole value of the second pass is that it has not heard the drafter's argument."""
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return draft_agent.AgentRun(ok=True, result={"summary": "s", "findings": []})

    monkeypatch.setattr(draft_agent, "run", fake_run)
    draft_qa.review(Path("/tmp"), checks=[])
    assert seen["resume"] is False
    assert "Bash" in seen["tools"] and "Write" not in seen["tools"]


def test_the_reviewer_is_told_which_checks_already_ran(monkeypatch):
    seen = {}
    monkeypatch.setattr(draft_agent, "run", lambda **k: seen.update(k) or draft_agent.AgentRun(
        ok=True, result={"summary": "", "findings": []}))
    draft_qa.review(Path("/tmp"), checks=[
        {"name": "Numerals", "status": "fail", "detail": "22 undefined", "items": ["22"]},
        {"name": "Claims", "status": "pass", "detail": "fine", "items": []}])
    assert "22 undefined" in seen["prompt"] and "Claims" not in seen["prompt"]
    assert "rendered-*.png" in draft_qa.REVIEW_PROMPT


def test_a_broken_reviewer_is_a_finding_not_an_exception(monkeypatch):
    def explode(**_kwargs):
        raise draft_agent.AgentUnavailable("no CLI here")
    monkeypatch.setattr(draft_agent, "run", explode)
    out = draft_qa.review(Path("/tmp"), checks=[])
    assert out["ok"] is False and "no CLI" in out["error"]


def test_the_drafting_prompt_states_the_rules_it_must_not_break():
    system = draft_studio.DRAFT_SYSTEM
    for phrase in ("prior_art/INDEX.md", "[REF:KEY]", "numerals.md",
                   "never state or imply", "Never invent", "Not applicable"):
        assert phrase in system, f"the drafting prompt no longer says: {phrase}"
    assert "[DRAFTING NOTE" not in system
    assert "Return `questions` as an empty array" in system
    assert "No placeholder" in system
    assert "at least one figure" in system
    assert "Normally use two to four figures" in system
    assert "Do not list more than eight numerals on one sheet" in system
    assert str(draft_qa.MAX_FIGURE_BRIEF_CHARS) in system
    assert "arbitrary exact counts" in system
    assert "Never address or mention a draftsperson" in " ".join(system.split())
    assert "broadest statement of the invention that the description fully supports" in system


def test_drawing_repairs_can_never_change_the_invention_to_match_bad_pixels():
    prompt = draft_studio.FINALIZE_PROMPT
    normalized = " ".join(prompt.split())
    assert "Generated drawing pixels are evidence to inspect, never authority" \
        in draft_studio.DRAFT_SYSTEM
    assert "Generated pixels are never authority for the invention" in prompt
    assert "Never change the claims, description, numeral table, or disclosed embodiments" \
        in prompt
    assert "regenerate the sheet from the authoritative text" in normalized


def test_the_independent_reviewer_checks_source_fidelity_before_internal_consistency():
    system = draft_qa.REVIEW_SYSTEM
    normalized = " ".join(system.split())
    assert "DISCLOSURE FIDELITY" in system
    assert "input/disclosure.md" in system
    assert "generated drawing artifact" in normalized
    assert "disclosure_fidelity" in json.dumps(draft_qa.REVIEW_SCHEMA)


def test_drawing_only_repairs_cannot_mutate_filing_sources_or_figure_membership(tmp_path):
    draft_workspace.write_sections(tmp_path, GOOD)
    draft_workspace.write_numerals(tmp_path, NUMERALS)
    changed_figures = [
        {**FIGURES[0], "caption": "A corrected geometry brief.",
         "numerals": [*FIGURES[0]["numerals"], "99 pixel artifact"]},
        {**FIGURES[1], "numerals": []},
        {"label": "FIG. 9", "caption": "A pixel-derived extra sheet.", "numerals": ["99"]},
    ]
    draft_workspace.write_figures(tmp_path, changed_figures)
    baseline = {"sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}
    draft_workspace.write_sections(tmp_path, {**GOOD, "summary": "Pixel-derived embodiment."})
    draft_workspace.write_numerals(tmp_path, [*NUMERALS, {"numeral": "99", "part": "artifact"}])
    report = {
        "checks": [{
            "name": "Every drawing sheet passes geometry, leader, and OCR inspection",
            "status": "fail",
        }],
        "findings": [{"category": "figures_and_numerals", "title": "Floating leader"}],
    }

    assert draft_studio.restore_text_after_drawing_only_review(tmp_path, baseline, report) is True
    assert draft_workspace.read_sections(tmp_path) == GOOD
    assert draft_workspace.read_numerals(tmp_path) == NUMERALS
    restored_figures = draft_workspace.read_figures(tmp_path)
    assert [item["label"] for item in restored_figures] == ["FIG. 1", "FIG. 2"]
    assert restored_figures[0]["caption"] == "A corrected geometry brief."
    assert restored_figures[0]["numerals"] == FIGURES[0]["numerals"]
    assert restored_figures[1]["numerals"] == FIGURES[1]["numerals"]


def test_figure_plan_repairs_keep_authoritative_text_and_numerals(tmp_path):
    baseline = {"sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}
    revised_sections = {
        **GOOD,
        "drawing_descriptions": "FIG. 1 is a focused body view.\n\nFIG. 2 is a ring detail.",
        "detailed_description": "Removed disclosed structure to make the picture easier.",
        "claims": "1. A different invention.",
    }
    revised_figures = [
        {**FIGURES[0], "numerals": ["10", "12"]},
        {**FIGURES[1], "numerals": ["14", "16", "18", "20"]},
    ]
    draft_workspace.write_sections(tmp_path, revised_sections)
    draft_workspace.write_numerals(tmp_path, NUMERALS[:-1])
    draft_workspace.write_figures(tmp_path, revised_figures)
    report = {
        "checks": [{
            "name": "Saved candidate passes the current filing preflight",
            "status": "fail", "category": "figures_and_numerals",
        }],
        "findings": [],
    }

    assert draft_studio.restore_sources_after_figure_plan_review(
        tmp_path, baseline, report) is True

    sections = draft_workspace.read_sections(tmp_path)
    assert sections["drawing_descriptions"] == revised_sections["drawing_descriptions"]
    assert sections["detailed_description"] == GOOD["detailed_description"]
    assert sections["claims"] == GOOD["claims"]
    assert draft_workspace.read_numerals(tmp_path) == NUMERALS
    assert [{key: item[key] for key in ("label", "caption", "numerals")}
            for item in draft_workspace.read_figures(tmp_path)] == revised_figures


def test_source_locks_never_restore_an_empty_or_incomplete_baseline(tmp_path):
    draft_workspace.write_sections(tmp_path, GOOD)
    draft_workspace.write_numerals(tmp_path, NUMERALS)
    draft_workspace.write_figures(tmp_path, FIGURES)
    before = draft_workspace.snapshot(tmp_path)
    figure_plan_report = {
        "checks": [{
            "name": "Drawing briefs are concise and renderable",
            "status": "fail", "category": "figures_and_numerals",
        }],
        "findings": [],
    }
    drawing_report = {
        "checks": [{
            "name": "Every drawing sheet passes geometry, leader, and OCR inspection",
            "status": "fail", "category": "figures_and_numerals",
        }],
        "findings": [],
    }

    assert not draft_studio.restore_sources_after_figure_plan_review(
        tmp_path, {}, figure_plan_report)
    assert draft_workspace.snapshot(tmp_path) == before
    assert not draft_studio.restore_text_after_drawing_only_review(
        tmp_path, {"sections": GOOD}, drawing_report)
    assert draft_workspace.snapshot(tmp_path) == before


def test_mixed_review_does_not_lock_a_legitimate_source_repair(tmp_path):
    revised = {**GOOD, "summary": "A source-faithful correction."}
    draft_workspace.write_sections(tmp_path, revised)
    draft_workspace.write_numerals(tmp_path, NUMERALS)
    draft_workspace.write_figures(tmp_path, FIGURES)
    report = {
        "checks": [{
            "name": "Saved candidate passes the current filing preflight",
            "status": "fail", "category": "figures_and_numerals",
        }],
        "findings": [{
            "title": "Unsupported relationship", "category": "disclosure_fidelity",
        }],
    }

    assert draft_studio.restore_sources_after_figure_plan_review(
        tmp_path, {"sections": GOOD, "numerals": NUMERALS, "figures": FIGURES},
        report) is False
    assert draft_workspace.read_sections(tmp_path) == revised


def test_a_non_drawing_review_may_repair_the_patent_text(tmp_path):
    draft_workspace.write_sections(tmp_path, {**GOOD, "summary": "Needs source repair."})
    baseline = {"sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}
    report = {
        "checks": [],
        "findings": [{"category": "disclosure_fidelity", "title": "Unsupported structure"}],
    }

    assert draft_studio.restore_text_after_drawing_only_review(tmp_path, baseline, report) is False
    assert draft_workspace.read_sections(tmp_path)["summary"] == "Needs source repair."


# =============================================================================================
# The workspace is a cache, not the record
# =============================================================================================
def test_the_figure_directory_mirrors_the_stored_version(tmp_path, monkeypatch):
    """A drawing removed from the draft must not survive on disk and reappear in the next review."""
    monkeypatch.setattr(draft_workspace, "root", lambda: tmp_path)
    project = {"id": 1, "title": "t", "disclosure_text": "d" * 60, "input_kind": "description"}
    draft_workspace.build(project=project, figures=FIGURES)
    assert len(draft_workspace.read_figures(draft_workspace.for_project(1))) == 2
    draft_workspace.build(project=project, figures=[FIGURES[0]])
    assert [f["label"] for f in draft_workspace.read_figures(draft_workspace.for_project(1))] == \
        ["FIG. 1"]


def test_prior_art_files_and_the_index_carry_the_citation_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_workspace, "root", lambda: tmp_path)
    workspace = draft_workspace.build(
        project={"id": 2, "title": "t", "disclosure_text": "d" * 60},
        references=[{"publication_number": "US-11223344-B2", "title": "Handheld lifter",
                     "origin": "report", "relevance_summary": "close art",
                     "snapshot": {"abstract": "A lifter.", "claims": "1. A lifter."}}],
        documents=[{"kind": "prior_art", "filename": "brochure.pdf", "title": "A brochure",
                    "body": "Some product text.", "note": "found at a trade show"}])
    index = (workspace / "prior_art" / "INDEX.md").read_text()
    assert "[REF:PUBLICATION]" in index and "US-11223344-B2" in index and "UPLOAD-01" in index
    reference = (workspace / "prior_art" / "US-11223344-B2.md").read_text()
    assert "`[REF:US-11223344-B2]`" in reference and "1. A lifter." in reference
    upload = (workspace / "prior_art" / "UPLOAD-01.md").read_text()
    assert "has NOT been ranked" in upload and "trade show" in upload


def test_a_project_with_no_prior_art_is_told_so_rather_than_left_to_guess(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_workspace, "root", lambda: tmp_path)
    workspace = draft_workspace.build(
        project={"id": 3, "title": "t", "disclosure_text": "d" * 60, "search_slug": ""})
    brief = (workspace / "input" / "brief.md").read_text()
    assert "no search was run" in brief.lower() or "none was run" in brief.lower()
    assert "may be incomplete" in brief


def test_the_previous_review_reaches_the_next_iteration(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_workspace, "root", lambda: tmp_path)
    workspace = draft_workspace.build(
        project={"id": 5, "title": "t", "disclosure_text": "d" * 60},
        qa_report={"verdict": "fail", "summary": "numeral 22 is undefined",
                   "checks": [{"name": "Every numeral in the text is defined", "status": "fail",
                               "detail": "22 is not in the table",
                               "items": ["FIG. 2: wrong fastener axis",
                                         "FIG. 7: missing process arrow"]}],
                   "findings": [{"severity": "critical", "title": "Claim 1 unsupported",
                                 "where": "claims", "detail": "no support", "fix": "add support"}]})
    review = (workspace / "review" / "previous-qa.md").read_text()
    assert "numeral 22 is undefined" in review and "Claim 1 unsupported" in review
    assert "add support" in review
    assert "FIG. 2: wrong fastener axis" in review
    assert "FIG. 7: missing process arrow" in review


def test_figure_labels_match_across_spellings():
    import draft_studio_service
    assert draft_studio_service._figure_key("FIG. 1") == draft_studio_service._figure_key("Fig 1")
    assert draft_studio_service._figure_key("FIGURE 2") == draft_studio_service._figure_key("FIG.2")
    assert draft_studio_service._figure_key("FIG. 1") != draft_studio_service._figure_key("FIG. 2")
    verbose = "FIG. 2: side elevation through the pressure chamber and all conduits"
    assert draft_studio_service._figure_key(verbose) == \
        draft_studio_service._figure_key(verbose[:28])


def test_filing_gate_requires_every_check_and_independent_review_to_be_clean():
    clean = {"status": "complete", "checks": [
        {"name": "Claims", "status": "pass"}], "findings": []}
    assert draft_studio.filing_blockers(clean) == []
    assert "Claims" in draft_studio.filing_blockers({
        **clean, "checks": [{"name": "Claims", "status": "warn"}]})[0]
    assert "Unsupported limitation" in draft_studio.filing_blockers({
        **clean, "findings": [{"title": "Unsupported limitation"}]})[0]
    assert "review" in draft_studio.filing_blockers({**clean, "status": "failed"})[0].lower()


def test_default_finalization_budget_allows_drawing_and_text_repair_rounds():
    assert draft_studio.MAX_FINALIZATION_ROUNDS == 6


def test_valid_candidate_is_checkpointed_before_the_long_drawing_gate(monkeypatch, tmp_path):
    repository = Mock()
    agent = Mock()
    agent.DRAFT_MODEL = "draft-model"
    agent.DRAFT_TIMEOUT = 60
    agent.new_session_id.return_value = "new-session"
    agent.run.return_value = draft_agent.AgentRun(
        ok=True, session_id="draft-session", model="draft-model",
        cost_usd=0.5, duration_ms=1000,
        result={"action": "revised", "summary": "complete candidate",
                "reasoning": [], "changes": [], "questions": [],
                "prior_art_strategy": "", "answer": ""})
    workspace = Mock()
    workspace.snapshot.return_value = {
        "sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=draft_qa, workspace=workspace)
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path,
        "project": {"user_id": 91, "agent_session_id": "", "latest_version_no": 0,
                    "disclosure_text": "disclosure"},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": False, "resuming_candidate": False,
        "previous_sections": {},
    })
    monkeypatch.setattr(
        runner, "_ensure_figures",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                    "turn_no": 1, "kind": "initial", "attempts": 1})

    checkpoint = repository.save_retry_candidate.call_args.kwargs
    assert checkpoint["snapshot"]["sections"] == GOOD
    assert checkpoint["report"]["_gate_resume"]["session_id"] == "draft-session"
    assert checkpoint["report"]["_gate_resume"]["result"]["summary"] == \
        "complete candidate"


def test_restart_resumes_a_checkpointed_candidate_without_rerunning_the_agent(
        monkeypatch, tmp_path):
    monkeypatch.setattr(draft_figures, "discard_project_figure_checkpoint",
                        lambda _turn_id: False)
    repository = Mock()
    repository.save_version.return_value = {"version_no": 2}
    repository.save_qa.return_value = {
        "id": 5, "verdict": "pass", "checks": [], "findings": [], "counts": {}}
    repository.complete_turn.return_value = {"status": "complete"}
    agent = Mock()
    agent.DRAFT_MODEL = "draft-model"
    agent.DRAFT_TIMEOUT = 60
    agent.strings.side_effect = lambda value, **_kwargs: list(value or [])
    workspace = Mock()
    workspace.snapshot.return_value = {
        "sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=draft_qa, workspace=workspace)
    gate_resume = {
        "session_id": "draft-session", "model": "draft-model", "cost_usd": 0.5,
        "duration_ms": 1000, "num_turns": 1, "steps": [],
        "result": {"action": "revised", "summary": "complete candidate",
                   "reasoning": [], "changes": [], "questions": [],
                   "prior_art_strategy": "", "answer": ""},
    }
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path,
        "project": {"user_id": 91, "agent_session_id": "draft-session",
                    "latest_version_no": 1, "disclosure_text": "disclosure"},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": True, "resuming_candidate": True,
        "resuming_candidate_turn_id": 3,
        "prepared_snapshot": {"sections": GOOD, "numerals": NUMERALS,
                              "figures": FIGURES},
        "prepared_qa": {"_gate_resume": gate_resume},
        "previous_sections": {},
    })
    monkeypatch.setattr(runner, "_ensure_figures", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(runner, "evaluate", lambda *args, **kwargs: {
        "status": "complete", "verdict": "pass", "summary": "ready",
        "checks": [], "findings": [], "counts": {}, "cost_usd": 0,
        "duration_ms": 1, "model_name": "review"})

    runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                "turn_no": 1, "kind": "initial", "attempts": 1})

    agent.run.assert_not_called()
    assert repository.save_version.call_count == 1
    assert repository.complete_turn.call_args.kwargs["session_id"] == "draft-session"


def test_turn_runner_publishes_only_after_automatic_repair_passes(monkeypatch, tmp_path):
    class Repository:
        def __init__(self):
            self.saved_versions = []
            self.saved_reports = []
            self.retry_candidates = []
            self.messages = []

        def heartbeat(self, *_args, **_kwargs):
            pass

        def save_version(self, *_args, **kwargs):
            self.saved_versions.append(kwargs)
            return {"version_no": 1}

        def save_qa(self, _project_id, **kwargs):
            self.saved_reports.append(kwargs)
            return {"id": 4, **kwargs["report"]}

        def save_retry_candidate(self, _turn_id, _lease, *, snapshot, report):
            self.retry_candidates.append((snapshot, report))

        def add_message(self, _project_id, role, body, **kwargs):
            self.messages.append((role, body, kwargs))

        def complete_turn(self, *_args, **kwargs):
            return {"status": "complete", **kwargs}

    class Agent:
        DRAFT_MODEL = "draft-model"
        DRAFT_TIMEOUT = 60

        def __init__(self, repository):
            self.calls = []
            self.repository = repository

        @staticmethod
        def new_session_id():
            return "new-session"

        @staticmethod
        def strings(value, **_kwargs):
            return list(value or [])

        def run(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 2:
                assert self.repository.saved_versions == [], \
                    "a failing intermediate draft must never be published"
            return draft_agent.AgentRun(
                ok=True, session_id="draft-session", model="draft-model",
                cost_usd=0.5, duration_ms=1000,
                result={"action": "revised", "summary": f"round {len(self.calls)}",
                        "reasoning": [], "changes": [], "questions": [],
                        "prior_art_strategy": "", "answer": ""})

    class Workspace:
        def __init__(self):
            self.review_reports = []

        def snapshot(self, _path):
            return {"sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}

        def _write_review(self, _path, report):
            self.review_reports.append(report)

    repository = Repository()
    agent = Agent(repository)
    workspace = Workspace()
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=object(), workspace=workspace)
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path, "project": {"user_id": 91, "agent_session_id": "",
                                             "latest_version_no": 0,
                                             "disclosure_text": "disclosure"},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": False,
        "previous_sections": {},
    })
    drawing_runs = iter([
        {"ok": False, "errors": ["FIG. 2: wrong fastener axis",
                                    "FIG. 7: missing process arrow"]},
        {"ok": True},
    ])
    monkeypatch.setattr(runner, "_ensure_figures", lambda **_kwargs: next(drawing_runs))
    reports = iter([
        {"status": "complete", "verdict": "pass", "summary": "ready",
         "checks": [{"name": "Claims", "status": "pass"}], "findings": [],
         "counts": {}, "cost_usd": 0, "duration_ms": 1, "model_name": "review"},
    ])
    monkeypatch.setattr(runner, "evaluate", lambda *args, **kwargs: next(reports))

    out = runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                      "turn_no": 1, "kind": "initial"})

    assert len(agent.calls) == 2
    assert agent.calls[1]["resume"] is True
    assert "did not pass" in agent.calls[1]["prompt"]
    assert len(repository.saved_versions) == 1
    assert len(repository.saved_reports) == 1
    assert repository.saved_reports[0]["report"]["verdict"] == "pass"
    assert all(message[0] != "qa" or message[1] == "ready" for message in repository.messages)
    assert workspace.review_reports[0]["checks"][0]["items"] == [
        "FIG. 2: wrong fastener axis", "FIG. 7: missing process arrow"]
    assert repository.retry_candidates[0][0]["sections"] == GOOD
    assert "_gate_resume" in repository.retry_candidates[0][1]
    assert any(report["verdict"] == "fail" for _snapshot, report
               in repository.retry_candidates)
    assert out["version"]["version_no"] == 1


def test_retry_preparation_uses_the_durable_checked_candidate_instead_of_published_text(
        monkeypatch, tmp_path):
    candidate_sections = {**GOOD, "summary": "Repaired candidate summary."}
    candidate_report = {
        "status": "complete", "verdict": "fail", "summary": "Fix claim wording.",
        "checks": [{"name": "Claims", "status": "fail", "items": ["claim 1"]}],
        "findings": [],
    }
    repository = Mock()
    repository.documents.return_value = []
    repository.messages.return_value = []
    repository.latest_qa.return_value = {"summary": "stale published review"}
    repository.retry_candidate.return_value = {
        "snapshot": {"sections": candidate_sections, "numerals": NUMERALS,
                     "figures": FIGURES},
        "qa_report": candidate_report,
    }
    workspace = Mock()
    workspace.build.return_value = tmp_path
    runner = draft_studio.TurnRunner(repository, Mock(), workspace=workspace)
    monkeypatch.setattr(runner, "_load", lambda _project_id: {
        "project": {"id": 7, "user_id": 91, "input_kind": "description"},
        "references": [{"publication_number": ALLOWED[0]}],
        "sections": GOOD, "numerals": NUMERALS, "figures": FIGURES,
    })

    context = runner.prepare({"id": 33, "project_id": 7, "attempts": 2,
                              "user_message": "Finish automatically."})

    values = workspace.build.call_args.kwargs
    assert values["sections"] == candidate_sections
    assert values["qa_report"] == candidate_report
    assert context["previous_sections"] == GOOD


def test_new_turn_preparation_uses_the_latest_failed_turn_candidate(
        monkeypatch, tmp_path):
    candidate_sections = {**GOOD, "summary": "Best unpublished candidate."}
    candidate_report = {
        "status": "complete", "verdict": "fail", "summary": "Finish claim repair.",
        "checks": [{"name": "Claims", "status": "fail", "items": ["claim 1"]}],
        "findings": [],
    }
    repository = Mock()
    repository.documents.return_value = []
    repository.messages.return_value = []
    repository.latest_qa.return_value = {"summary": "stale published review"}
    repository.retry_candidate.return_value = None
    repository.latest_retry_candidate.return_value = {
        "turn_id": 32,
        "snapshot": {"sections": candidate_sections, "numerals": NUMERALS,
                     "figures": FIGURES},
        "qa_report": candidate_report,
    }
    workspace = Mock()
    workspace.build.return_value = tmp_path
    runner = draft_studio.TurnRunner(repository, Mock(), workspace=workspace)
    monkeypatch.setattr(runner, "_load", lambda _project_id: {
        "project": {"id": 7, "user_id": 91, "input_kind": "description"},
        "references": [{"publication_number": ALLOWED[0]}],
        "sections": GOOD, "numerals": NUMERALS, "figures": FIGURES,
    })

    context = runner.prepare({"id": 33, "project_id": 7, "attempts": 1,
                              "user_message": "Finish automatically."})

    values = workspace.build.call_args.kwargs
    assert values["sections"] == candidate_sections
    assert values["qa_report"] == candidate_report
    repository.latest_retry_candidate.assert_called_once_with(7, before_turn_id=33)
    assert context["resuming_candidate"] is True
    assert context["previous_sections"] == GOOD


def test_a_candidate_blocked_by_a_new_preflight_gate_is_retained_for_repair(
        monkeypatch, tmp_path):
    candidate_figures = [{
        "label": "FIG. 1", "caption": "assembly view",
        "numerals": [str(value) for value in range(10, 28, 2)],
    }]
    candidate_snapshot = {
        "sections": GOOD,
        "numerals": [
            {"numeral": str(value), "part": f"part {value}"}
            for value in range(10, 28, 2)
        ],
        "figures": candidate_figures,
    }
    repository = Mock()
    repository.documents.return_value = []
    repository.messages.return_value = []
    repository.latest_qa.return_value = {"summary": "stale published review"}
    repository.retry_candidate.return_value = None
    repository.latest_retry_candidate.return_value = {
        "turn_id": 32,
        "snapshot": candidate_snapshot,
        "qa_report": {
            "status": "complete", "verdict": "fail", "summary": "Drawing repair needed.",
            "checks": [{"name": "Drafting run completed", "status": "fail"}],
            "findings": [],
        },
    }
    workspace = Mock()
    workspace.build.return_value = tmp_path
    runner = draft_studio.TurnRunner(repository, Mock(), workspace=workspace)
    monkeypatch.setattr(runner, "_load", lambda _project_id: {
        "project": {"id": 7, "user_id": 91, "input_kind": "description"},
        "references": [{"publication_number": ALLOWED[0]}],
        "sections": GOOD, "numerals": NUMERALS, "figures": FIGURES,
    })

    context = runner.prepare({"id": 33, "project_id": 7, "attempts": 1,
                              "user_message": "Finish automatically."})

    values = workspace.build.call_args.kwargs
    assert values["figures"] == candidate_figures
    assert values["qa_report"]["verdict"] == "fail"
    assert any("current filing preflight" in item["name"].lower()
               for item in values["qa_report"]["checks"])
    current_gate = next(item for item in values["qa_report"]["checks"]
                        if "current filing preflight" in item["name"].lower())
    assert current_gate["category"] == "figures_and_numerals"
    assert all(item["name"] != "Drafting run completed"
               for item in values["qa_report"]["checks"])
    assert "more than 8 numerals" in json.dumps(values["qa_report"])
    assert context["resuming_candidate"] is True
    assert context["prepared_snapshot"] == candidate_snapshot
    assert context["prepared_qa"] == values["qa_report"]
    repository.discard_retry_candidate.assert_not_called()


def test_a_structurally_corrupt_candidate_is_discarded_instead_of_repaired(
        monkeypatch, tmp_path):
    repository = Mock()
    repository.documents.return_value = []
    repository.messages.return_value = []
    repository.latest_qa.return_value = {"summary": "published review"}
    repository.retry_candidate.return_value = None
    repository.latest_retry_candidate.return_value = {
        "turn_id": 32,
        "snapshot": {"sections": {"title": "partial"}, "numerals": "bad", "figures": []},
        "qa_report": {"verdict": "fail"},
    }
    workspace = Mock()
    workspace.build.return_value = tmp_path
    runner = draft_studio.TurnRunner(repository, Mock(), workspace=workspace)
    monkeypatch.setattr(runner, "_load", lambda _project_id: {
        "project": {"id": 7, "user_id": 91, "input_kind": "description"},
        "references": [{"publication_number": ALLOWED[0]}],
        "sections": GOOD, "numerals": NUMERALS, "figures": FIGURES,
    })

    context = runner.prepare({"id": 33, "project_id": 7, "attempts": 1,
                              "user_message": "Finish automatically."})

    values = workspace.build.call_args.kwargs
    assert values["sections"] == GOOD and values["figures"] == FIGURES
    assert context["resuming_candidate"] is False
    repository.discard_retry_candidate.assert_called_once_with(32)


def test_an_agent_budget_stop_checkpoints_the_valid_workspace_for_retry(monkeypatch, tmp_path):
    repository = Mock()
    repository.documents.return_value = []
    agent = Mock()
    agent.DRAFT_MODEL = "draft-model"
    agent.DRAFT_TIMEOUT = 60
    agent.new_session_id.return_value = "new-session"
    agent.run.return_value = draft_agent.AgentRun(
        ok=False, session_id="session", model="draft-model",
        error="Reached maximum budget ($12)")
    workspace = Mock()
    workspace.snapshot.return_value = {
        "sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=draft_qa, workspace=workspace)
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path, "project": {"user_id": 91, "agent_session_id": "",
                                             "latest_version_no": 0,
                                             "disclosure_text": "disclosure"},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": False,
        "previous_sections": {},
    })

    with pytest.raises(draft_studio.StudioError, match="maximum budget"):
        runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                    "turn_no": 1, "kind": "initial"})

    snapshot = repository.save_retry_candidate.call_args.kwargs["snapshot"]
    report = repository.save_retry_candidate.call_args.kwargs["report"]
    assert snapshot["sections"] == GOOD
    assert report["verdict"] == "fail"
    assert "continue" in report["summary"].lower()


def test_terminal_failure_retains_candidate_until_a_project_completes(monkeypatch):
    queries = []

    class Cursor:
        def execute(self, query, params=()):
            queries.append((query, params))

        def fetchone(self):
            return {
                "id": 33, "project_id": 7, "turn_no": 4,
                "attempts": 3, "max_attempts": 3, "status": "failed",
            }

    @contextmanager
    def cursor_factory(**_kwargs):
        yield Cursor()

    repository = draft_studio.StudioRepository(cursor_factory, migrate=False)
    turn = {
        "id": 33, "project_id": 7, "turn_no": 4,
        "attempts": 3, "max_attempts": 3, "status": "running",
    }
    monkeypatch.setattr(repository, "_verify", lambda *_args: turn)

    result = repository.fail_turn(33, "lease", "budget stopped", retryable=True)

    assert result["status"] == "failed"
    assert not any("DELETE FROM app_draft_turn_candidates" in query for query, _ in queries)

    queries.clear()
    repository.complete_turn(
        33, "lease", result={}, session_id="session", cost_usd=1,
        duration_ms=1000, model_name="model")
    cleanup = next(query for query, _ in queries
                   if "DELETE FROM app_draft_turn_candidates" in query)
    assert "USING app_draft_turns" in cleanup and "t.project_id" in cleanup

    queries.clear()
    repository.complete_turn(
        33, "lease", result={}, session_id="session", cost_usd=1,
        duration_ms=1000, model_name="model", discard_candidates=False)
    assert not any("DELETE FROM app_draft_turn_candidates" in query for query, _ in queries)


def test_invalid_workspace_is_automatic_repair_input_not_a_failed_turn(monkeypatch, tmp_path):
    repository = Mock()
    repository.save_version.return_value = {"version_no": 1}
    repository.save_qa.return_value = {"id": 5, "verdict": "pass", "checks": [],
                                       "findings": [], "counts": {}}
    repository.complete_turn.return_value = {"status": "complete"}
    agent = Mock()
    agent.DRAFT_MODEL = "draft-model"
    agent.DRAFT_TIMEOUT = 60
    agent.new_session_id.return_value = "new-session"
    agent.strings.side_effect = lambda value, **_kwargs: list(value or [])
    agent.run.side_effect = [
        draft_agent.AgentRun(ok=True, session_id="session", model="draft-model",
                             result={"action": "revised", "summary": "initial",
                                     "reasoning": [], "changes": [], "questions": [],
                                     "prior_art_strategy": "", "answer": ""}),
        draft_agent.AgentRun(ok=True, session_id="session", model="draft-model",
                             result={"action": "revised", "summary": "repaired",
                                     "reasoning": [], "changes": [], "questions": [],
                                     "prior_art_strategy": "", "answer": ""}),
    ]
    workspace = Mock()
    workspace.snapshot.side_effect = [
        {"sections": {**GOOD, "cross_reference": ""}, "numerals": NUMERALS,
         "figures": FIGURES},
        {"sections": GOOD, "numerals": NUMERALS, "figures": FIGURES},
    ]
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=draft_qa, workspace=workspace)
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path, "project": {"user_id": 91, "agent_session_id": "",
                                             "latest_version_no": 0,
                                             "disclosure_text": "disclosure"},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": False,
        "previous_sections": {},
    })
    monkeypatch.setattr(runner, "_ensure_figures", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(runner, "evaluate", lambda *args, **kwargs: {
        "status": "complete", "verdict": "pass", "summary": "ready",
        "checks": [], "findings": [], "counts": {}, "cost_usd": 0,
        "duration_ms": 1, "model_name": "review"})

    runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                "turn_no": 1, "kind": "initial"})

    assert agent.run.call_count == 2
    assert repository.save_version.call_count == 1
    assert workspace._write_review.call_count >= 1
    assert "missing Cross-Reference" in workspace._write_review.call_args_list[0].args[1][
        "summary"]


def test_initial_turn_cannot_finish_as_an_answer_without_a_filing_candidate(monkeypatch, tmp_path):
    repository = Mock()
    repository.save_version.return_value = {"version_no": 1}
    repository.save_qa.return_value = {"id": 5, "verdict": "pass", "checks": [],
                                       "findings": [], "counts": {}}
    repository.complete_turn.return_value = {"status": "complete"}
    agent = Mock()
    agent.DRAFT_MODEL = "draft-model"
    agent.DRAFT_TIMEOUT = 60
    agent.new_session_id.return_value = "new-session"
    agent.strings.side_effect = lambda value, **_kwargs: list(value or [])
    agent.run.side_effect = [
        draft_agent.AgentRun(ok=True, session_id="session", model="draft-model",
                             result={"action": "answered", "summary": "I need more detail",
                                     "reasoning": [], "changes": [], "questions": [],
                                     "prior_art_strategy": "", "answer": "Please clarify."}),
        draft_agent.AgentRun(ok=True, session_id="session", model="draft-model",
                             result={"action": "revised", "summary": "complete application",
                                     "reasoning": [], "changes": [], "questions": [],
                                     "prior_art_strategy": "", "answer": ""}),
    ]
    workspace = Mock()
    workspace.snapshot.return_value = {
        "sections": GOOD, "numerals": NUMERALS, "figures": FIGURES}
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=draft_qa, workspace=workspace)
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path, "project": {"user_id": 91, "agent_session_id": "",
                                             "latest_version_no": 0,
                                             "disclosure_text": "disclosure"},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": False,
        "previous_sections": {},
    })
    monkeypatch.setattr(runner, "_ensure_figures", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(runner, "evaluate", lambda *args, **kwargs: {
        "status": "complete", "verdict": "pass", "summary": "ready",
        "checks": [], "findings": [], "counts": {}, "cost_usd": 0,
        "duration_ms": 1, "model_name": "review"})

    runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                "turn_no": 1, "kind": "initial"})

    assert agent.run.call_count == 2
    assert repository.save_version.call_count == 1
    first_report = workspace._write_review.call_args_list[0].args[1]
    assert "filing candidate" in first_report["summary"].lower()


def test_resumed_figure_plan_repair_is_source_locked_before_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_figures, "discard_project_figure_checkpoint",
                        lambda _turn_id: False)
    repository = Mock()
    repository.save_version.return_value = {"version_no": 2}
    repository.save_qa.return_value = {
        "id": 5, "verdict": "pass", "checks": [], "findings": [], "counts": {}}
    repository.complete_turn.return_value = {"status": "complete"}
    agent = Mock()
    agent.DRAFT_MODEL = "draft-model"
    agent.DRAFT_TIMEOUT = 60
    agent.new_session_id.return_value = "new-session"
    agent.strings.side_effect = lambda value, **_kwargs: list(value or [])
    revised_figures = [
        {**FIGURES[0], "numerals": ["10", "12"]},
        {**FIGURES[1], "numerals": ["14", "16", "18", "20"]},
    ]

    def mutate_sources(**_kwargs):
        draft_workspace.write_sections(tmp_path, {
            **GOOD,
            "drawing_descriptions": "FIG. 1 is a body view.\n\nFIG. 2 is a ring view.",
            "detailed_description": "Deleted part 20 to simplify the drawings.",
            "claims": "1. A different invention.",
        })
        draft_workspace.write_numerals(tmp_path, NUMERALS[:-1])
        draft_workspace.write_figures(tmp_path, revised_figures)
        return draft_agent.AgentRun(
            ok=True, session_id="session", model="draft-model",
            result={"action": "revised", "summary": "rebalanced figures",
                    "reasoning": [], "changes": [], "questions": [],
                    "prior_art_strategy": "", "answer": ""})

    agent.run.side_effect = mutate_sources
    draft_workspace.write_sections(tmp_path, GOOD)
    draft_workspace.write_numerals(tmp_path, NUMERALS)
    draft_workspace.write_figures(tmp_path, FIGURES)
    workspace = Mock()
    workspace.snapshot.side_effect = lambda _path: draft_workspace.snapshot(tmp_path)
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=draft_qa, workspace=workspace)
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path,
        "project": {"user_id": 91, "agent_session_id": "", "latest_version_no": 1,
                    "disclosure_text": "disclosure"},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": True, "resuming_candidate": True,
        "prepared_snapshot": {"sections": GOOD, "numerals": NUMERALS,
                              "figures": FIGURES},
        "prepared_qa": {"checks": [{
            "name": "Saved candidate passes the current filing preflight",
            "status": "fail", "category": "figures_and_numerals",
        }], "findings": []},
        "previous_sections": {},
    })
    monkeypatch.setattr(runner, "_ensure_figures", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(runner, "evaluate", lambda *args, **kwargs: {
        "status": "complete", "verdict": "pass", "summary": "ready",
        "checks": [], "findings": [], "counts": {}, "cost_usd": 0,
        "duration_ms": 1, "model_name": "review"})

    runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                "turn_no": 2, "kind": "revise"})

    saved = repository.save_version.call_args.kwargs
    assert saved["sections"]["detailed_description"] == GOOD["detailed_description"]
    assert saved["sections"]["claims"] == GOOD["claims"]
    assert saved["numerals"] == NUMERALS
    assert [{key: item[key] for key in ("label", "caption", "numerals")}
            for item in saved["figures"]] == revised_figures


def test_preflight_failure_retains_the_full_candidate_as_the_repair_source_lock(
        monkeypatch, tmp_path):
    monkeypatch.setattr(draft_figures, "discard_project_figure_checkpoint",
                        lambda _turn_id: False)
    repository = Mock()
    repository.save_version.return_value = {"version_no": 2}
    repository.save_qa.return_value = {
        "id": 5, "verdict": "pass", "checks": [], "findings": [], "counts": {}}
    repository.complete_turn.return_value = {"status": "complete"}
    agent = Mock()
    agent.DRAFT_MODEL = "draft-model"
    agent.DRAFT_TIMEOUT = 60
    agent.new_session_id.return_value = "new-session"
    agent.strings.side_effect = lambda value, **_kwargs: list(value or [])
    overlong_figures = [
        {**FIGURES[0], "caption": "plain rectangular body " * 160},
        FIGURES[1],
    ]
    repaired_figures = [
        {**FIGURES[0], "caption": "plain rectangular body"},
        FIGURES[1],
    ]

    def run_agent(**_kwargs):
        if agent.run.call_count == 1:
            return draft_agent.AgentRun(
                ok=True, session_id="session", model="draft-model",
                result={"action": "revised", "summary": "candidate",
                        "reasoning": [], "changes": [], "questions": [],
                        "prior_art_strategy": "", "answer": ""})
        draft_workspace.write_sections(tmp_path, {
            key: ("FIG. 1 is an assembly view. FIG. 2 is a ring view."
                  if key == "drawing_descriptions" else "")
            for key, _name, _heading in draft_workspace.SECTION_FILES
        })
        draft_workspace.write_numerals(tmp_path, [])
        draft_workspace.write_figures(tmp_path, repaired_figures)
        return draft_agent.AgentRun(
            ok=True, session_id="session", model="draft-model",
            result={"action": "revised", "summary": "shortened drawing brief",
                    "reasoning": [], "changes": [], "questions": [],
                    "prior_art_strategy": "", "answer": ""})

    agent.run.side_effect = run_agent
    draft_workspace.write_sections(tmp_path, GOOD)
    draft_workspace.write_numerals(tmp_path, NUMERALS)
    draft_workspace.write_figures(tmp_path, overlong_figures)
    workspace = Mock()
    workspace.snapshot.side_effect = lambda _path: draft_workspace.snapshot(tmp_path)
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=draft_qa, workspace=workspace)
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path,
        "project": {"user_id": 91, "agent_session_id": "", "latest_version_no": 1,
                    "disclosure_text": "disclosure"},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": True, "resuming_candidate": False,
        "previous_sections": {},
    })
    monkeypatch.setattr(runner, "_ensure_figures", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(runner, "evaluate", lambda *args, **kwargs: {
        "status": "complete", "verdict": "pass", "summary": "ready",
        "checks": [], "findings": [], "counts": {}, "cost_usd": 0,
        "duration_ms": 1, "model_name": "review"})

    runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                "turn_no": 2, "kind": "revise"})

    assert agent.run.call_count == 2
    first_report = workspace._write_review.call_args_list[0].args[1]
    assert first_report["checks"][0]["category"] == "figures_and_numerals"
    saved = repository.save_version.call_args.kwargs
    assert saved["sections"]["claims"] == GOOD["claims"]
    assert saved["sections"]["detailed_description"] == GOOD["detailed_description"]
    assert saved["numerals"] == NUMERALS
    assert [{key: item[key] for key in ("label", "caption", "numerals")}
            for item in saved["figures"]] == repaired_figures


def test_answering_a_question_does_not_discard_an_unpublished_candidate(monkeypatch, tmp_path):
    repository = Mock()
    repository.complete_turn.return_value = {"status": "complete"}
    agent = Mock()
    agent.DRAFT_MODEL = "draft-model"
    agent.DRAFT_TIMEOUT = 60
    agent.new_session_id.return_value = "new-session"
    agent.run.return_value = draft_agent.AgentRun(
        ok=True, session_id="session", model="draft-model",
        result={"action": "answered", "summary": "answered",
                "reasoning": [], "changes": [], "questions": [],
                "prior_art_strategy": "", "answer": "The answer."})
    runner = draft_studio.TurnRunner(
        repository, object(), agent=agent, qa=draft_qa, workspace=Mock())
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path, "project": {"user_id": 91, "agent_session_id": "prior",
                                             "latest_version_no": 1,
                                             "disclosure_text": "disclosure"},
        "references": [], "documents": [], "seeded": False, "had_version": True,
        "resuming_candidate": True, "previous_sections": GOOD,
    })

    runner.run({"id": 3, "lease_token": "lease", "project_id": 7,
                "turn_no": 2, "kind": "question"})

    assert repository.complete_turn.call_args.kwargs["discard_candidates"] is False


def test_a_drawing_prompt_gets_only_the_numerals_for_that_figure():
    import draft_studio_service
    version = {"numerals": NUMERALS, "figure_specs": FIGURES}
    assert draft_studio_service._expected_numerals(version, "Figure 1") == [
        "10 = vacuum lifting tool", "12 = body", "14 = pump"]
    assert "20 = passage" in draft_studio_service._expected_numerals(version, "FIG. 2")


def test_an_orphan_drawing_edit_explicitly_forbids_all_numerals(monkeypatch):
    """An unmatched photo must not inherit every numeral in the whole application."""
    import draft_figures
    import draft_studio_service

    class DraftingService:
        def get_project(self, _principal, project_id, include_versions=True):
            assert project_id == 7 and include_versions is True
            return {
                "id": 7, "user_id": 91, "latest_version_no": 1,
                "disclosure_text": GOOD["detailed_description"],
                "versions": [{"version_no": 1, "sections": GOOD,
                              "numerals": NUMERALS, "figure_specs": FIGURES}],
            }

    captured = {}

    def render(*_args, **kwargs):
        captured.update(kwargs)
        return {"figure_id": 9, "version_no": 2}

    monkeypatch.setattr(draft_figures, "render_figure", render)
    service = draft_studio_service.StudioService(DraftingService(), repository=object())
    service.draw_figure(object(), 7, label="FIG. 3", caption="photo-derived view",
                        instruction="simplify this area", figure_id=9,
                        region=[10, 10, 80, 80])
    assert captured["numerals"] == []


# =============================================================================================
# False positives, each measured on a real 20-claim draft before it was fixed
# =============================================================================================
def test_a_claims_own_number_is_not_a_reference_numeral():
    """Before this, every claim in a 20-claim set was reported as an undefined part."""
    check = checks_for()["Numerals in the claims are defined"] if \
        "Numerals in the claims are defined" in checks_for() else None
    assert check is None, "a clean claim set must raise nothing about numerals in the claims"


def test_a_numeral_genuinely_recited_in_a_claim_is_still_caught():
    broken = dict(GOOD)
    broken["claims"] += ("\n\n4. The vacuum lifting tool of claim 1, wherein the retaining "
                         "shoulder 44 is annular.")
    check = checks_for(broken)["Numerals in the claims are defined"]
    assert check["status"] == "warn" and "44" in check["items"]


def test_a_figure_caption_that_cross_references_another_figure_still_matches_its_sheet():
    """"FIG. 3 — Enlarged detail III of FIG. 2" is figure 3, not figure 32."""
    assert draft_qa.figure_number("FIG. 3 — Enlarged detail III of FIG. 2") == "3"
    assert draft_qa.figure_number("FIG. 1 — Perspective view") == "1"
    figures = [{"label": "FIG. 1 — Perspective view", "caption": "", "numerals": []},
               {"label": "FIG. 2 — Section on 2—2 of FIG. 1", "caption": "", "numerals": []}]
    checks = checks_for(figures=figures)
    assert "Each described figure has a drawing sheet" not in checks


def test_figure_specifications_must_have_unique_contiguous_numbers():
    figures = [
        {"label": "FIG. 1: perspective", "caption": "view", "numerals": ["10"]},
        {"label": "FIG. 1: duplicate", "caption": "other", "numerals": ["12"]},
        {"label": "FIG. 3", "caption": "gap", "numerals": ["14"]},
    ]
    check = checks_for(figures=figures)["Figure-sheet numbering is unique and contiguous"]
    assert check["status"] == "fail"
    assert any("duplicate" in item for item in check["items"])
    assert any("expected" in item for item in check["items"])


def test_a_participle_after_the_noun_does_not_break_antecedent_basis():
    """"an evacuable chamber" … "the evacuable chamber displaces" is the same chamber."""
    broken = dict(GOOD)
    broken["claims"] = ("1. A tool comprising a body, a pump, and an evacuable chamber bounded by "
                        "the body, wherein the evacuable chamber displaces a sealing ring, and "
                        "wherein the body carries the pump.")
    check = checks_for(broken)["Antecedent basis in the claims"]
    assert check["status"] == "pass", check["items"]


def test_antecedent_basis_still_catches_a_term_that_was_never_introduced():
    broken = dict(GOOD)
    broken["claims"] = "1. A tool comprising a body, wherein the retaining shoulder is annular."
    check = checks_for(broken)["Antecedent basis in the claims"]
    assert check["status"] == "warn" and any("retaining shoulder" in i for i in check["items"])


def test_morphological_variants_are_not_reported_as_unsupported_claim_terms():
    broken = dict(GOOD)
    broken["detailed_description"] += (" A controller energises the pump 14 and connects it to a "
                                       "battery, driving the diaphragm, and each ring is fitted "
                                       "and received in the groove 18 through a cycle.")
    broken["claims"] += ("\n\n4. The tool of claim 1, wherein a controller energising the pump "
                         "is connecting a battery, driving the diaphragm, the ring being fittable "
                         "and receivable, cycles being repeated.")
    check = checks_for(broken)["Claim terms appear in the description"]
    reported = " ".join(check.get("items") or [])
    for variant in ("energising", "connecting", "driving", "fittable", "receivable", "cycles"):
        assert variant not in reported, f"{variant} is the same word as one in the description"


def test_a_genuinely_different_word_is_still_reported():
    broken = dict(GOOD)
    broken["claims"] += "\n\n4. The tool of claim 1, wherein the housing is titanium."
    check = checks_for(broken)["Claim terms appear in the description"]
    reported = " ".join(check.get("items") or [])
    assert "housing" in reported and "titanium" in reported


def test_the_stemmer_is_consistent_across_the_forms_that_bit():
    pairs = [("energising", "energises"), ("driving", "drive"), ("connecting", "connect"),
             ("fittable", "fitted"), ("receivable", "received"), ("cycles", "cycle")]
    for left, right in pairs:
        assert draft_qa._stem(left) == draft_qa._stem(right), (left, right)
    assert draft_qa._stem("housing") != draft_qa._stem("body")


def test_the_first_version_names_the_project():
    """Until a draft exists the title is a placeholder — usually the first line of a paste."""
    assert draft_studio.project_title_from(1, GOOD) == GOOD["title"]
    assert draft_studio.project_title_from(1, {"title": "A Title\nand a stray line"}) == "A Title"
    assert draft_studio.project_title_from(1, {"title": "   "}) == ""


def test_a_later_version_never_renames_the_project():
    """A user who renamed the project must not have it overwritten by the next iteration."""
    assert draft_studio.project_title_from(2, GOOD) == ""
    assert draft_studio.project_title_from(7, GOOD) == ""


def test_recovery_leaves_a_turn_whose_worker_is_still_alive():
    """Expiring a live lease would put two agents on one workspace, not just two rows."""
    import os
    import draft_studio_service as service
    assert service._worker_is_alive(f"draft-turn-{os.getpid()}-140234") is True
    assert service._worker_is_alive("draft-turn-999999-1") is False
    assert service._worker_is_alive("") is False
    assert service._worker_is_alive(None) is False


def test_restart_recovery_does_not_spend_a_drafting_attempt(monkeypatch):
    import db
    import draft_studio_service as service

    queries = []

    class Cursor:
        def execute(self, query, params=()):
            queries.append((query, params))

        def fetchall(self):
            return [{"id": 33, "claimed_by": "draft-turn-999999-1"}]

    @contextmanager
    def cursor_factory(**_kwargs):
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor_factory)

    assert service.recover_interrupted_turns() == 1
    update = next(query for query, _params in queries
                  if "stage='resuming after a restart'" in query)
    assert "attempts=greatest(0,attempts-1)" in update


@pytest.mark.parametrize("failure", [
    drafting.DraftingValidationError("candidate failed"),
    drafting.DraftingConflict("turn superseded"),
])
def test_a_failed_or_superseded_turn_restores_the_published_drawing_set(monkeypatch, failure):
    import draft_studio_service as service

    class Repository:
        def claim_turn(self, worker_id):
            return {"id": 31, "lease_token": "lease", "project_id": 7}

        def fail_turn(self, turn_id, lease, error, retryable=True):
            return {"status": "failed"}

        def add_message(self, *args, **kwargs):
            pass

    class Runner:
        repository = Repository()

        def __init__(self):
            self.restored = []

        def run(self, claimed):
            raise failure

        def restore_figures(self, turn_id):
            self.restored.append(turn_id)
            return True

    runner = Runner()
    monkeypatch.setattr(service, "_RUNNER_FACTORY", lambda: runner)

    service.process_one()

    assert runner.restored == [31]


def test_a_publication_number_in_prose_is_not_three_reference_numerals():
    """Measured live: the turn that finally cited a reference properly FAILED the numeral check,
    because "US 9,108,319 B2" beside its token read as numerals 9, 108 and 319."""
    text = ("US 9,108,319 B2 [REF:US-9108319-B2] describes a suction cup assembly having a "
            "housing 12. The tool 10 lifts 60 kg with a 200 mm ring.")
    assert sorted(draft_qa.numerals_used(text), key=int) == ["10", "12"]


def test_a_cited_reference_does_not_fail_the_numeral_check():
    broken = dict(GOOD)
    broken["background"] += (" US 9,108,319 B2 [REF:US-11223344-B2] describes a suction cup "
                             "assembly.")
    check = checks_for(broken)["Every numeral in the text is defined"]
    assert check["status"] == "pass", check["items"]


def test_a_figure_written_as_prose_still_yields_its_numerals(tmp_path):
    """The agent writes a drawing brief, not a bullet list; the numerals must be found anyway."""
    directory = tmp_path / "figures"
    directory.mkdir()
    (directory / "fig-01.md").write_text(
        "# FIG. 1 — Perspective view\n\n**View type:** isometric.\n\n"
        "The body 12 carries the handle 14; the sealing ring 16 is visible at the underside.\n",
        encoding="utf-8")
    figure = draft_workspace.read_figures(tmp_path)[0]
    assert figure["label"].startswith("FIG. 1")
    assert set(figure["numerals"]) == {"12", "14", "16"}


def test_an_explicit_numeral_list_still_wins_over_the_prose_scan(tmp_path):
    directory = tmp_path / "figures"
    directory.mkdir()
    (directory / "fig-02.md").write_text(
        "# FIG. 2\n\nSection through the body 12.\n\n## Numerals shown on this figure\n\n"
        "- 12 body\n- 14 handle\n", encoding="utf-8")
    figure = draft_workspace.read_figures(tmp_path)[0]
    assert figure["numerals"] == ["12 body", "14 handle"]


def test_a_numbered_list_is_not_a_set_of_reference_numerals():
    """Measured: an ordered list inside a drawing brief reported 1, 2 and 3 as undefined parts."""
    text = ("**What is shown**\n\n1. The heel portion 38 is deformed.\n"
            "2. The lip portion 40 rolls.\n\nThe body 12 is unchanged.")
    assert sorted(draft_qa.numerals_used(text), key=int) == ["12", "38", "40"]


def test_a_numeral_at_the_end_of_a_sentence_still_counts():
    """The list-marker rule must not swallow a real numeral; one never OPENS a sentence."""
    assert sorted(draft_qa.numerals_used("The tool comprises a body 12."), key=int) == ["12"]
