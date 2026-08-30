"""The three ways a draft changes that are not a full revision.

A full revision runs the agent with write tools, redraws and re-inspects every sheet, and has an
independent reviewer read the result. That is right when the invention has changed and ruinous when
a clause has. These tests pin the three cheaper paths and, more importantly, pin what they must
NOT do: a section edit must never reach the figure pipeline, and a hand edit must never be silently
rewritten on its way to storage.
"""
from unittest.mock import Mock

import pytest

import draft_agent
import draft_studio
import draft_workspace
import drafting

from test_draft_studio import ALLOWED, FIGURES, GOOD, NUMERALS, no_corpus  # noqa: F401


# =============================================================================================
# Applying a patch this process was handed
# =============================================================================================
def test_a_find_and_replace_pair_changes_only_what_it_names():
    out = draft_studio.apply_section_edits(GOOD["field"], {
        "edits": [{"find": "portable vacuum lifting tools",
                   "replace": "portable and self-propelled vacuum lifting tools",
                   "why": "the disclosure covers both"}],
        "replacement": ""})
    assert out == "The disclosure relates to portable and self-propelled vacuum lifting tools."


def test_several_pairs_apply_in_order():
    out = draft_studio.apply_section_edits("A cat sat. A dog ran.", {
        "edits": [{"find": "cat", "replace": "gripper", "why": ""},
                  {"find": "dog", "replace": "pump", "why": ""}],
        "replacement": ""})
    assert out == "A gripper sat. A pump ran."


def test_an_empty_replacement_deletes_the_text_it_found():
    out = draft_studio.apply_section_edits("Keep this. Drop this.", {
        "edits": [{"find": " Drop this.", "replace": "", "why": "unsupported"}],
        "replacement": ""})
    assert out == "Keep this."


def test_text_the_agent_retyped_with_different_line_breaks_still_applies():
    """The one near miss worth forgiving: a wrapped paragraph quoted back on one line."""
    current = "The sealing ring is received\nin a groove in the body."
    out = draft_studio.apply_section_edits(current, {
        "edits": [{"find": "The sealing ring is received in a groove in the body.",
                   "replace": "The sealing ring seats in a groove in the body.", "why": ""}],
        "replacement": ""})
    assert out == "The sealing ring seats in a groove in the body."


def test_a_find_that_is_not_in_the_section_fails_the_whole_patch():
    with pytest.raises(draft_studio.SectionEditError) as caught:
        draft_studio.apply_section_edits(GOOD["field"], {
            "edits": [{"find": "the seal is annular", "replace": "x", "why": ""}],
            "replacement": ""})
    assert "not in this section" in str(caught.value)


def test_an_ambiguous_find_is_refused_rather_than_applied_to_the_first_one():
    """Guessing which occurrence was meant edits a sentence nobody chose."""
    with pytest.raises(draft_studio.SectionEditError) as caught:
        draft_studio.apply_section_edits("the body 12 and the body 12 again", {
            "edits": [{"find": "the body 12", "replace": "the housing 12", "why": ""}],
            "replacement": ""})
    assert "appears 2 times" in str(caught.value)


def test_a_patch_is_all_or_nothing():
    """Half a patch publishes a section the agent never wrote and never read back."""
    with pytest.raises(draft_studio.SectionEditError):
        draft_studio.apply_section_edits("A cat sat.", {
            "edits": [{"find": "cat", "replace": "gripper", "why": ""},
                      {"find": "elephant", "replace": "pump", "why": ""}],
            "replacement": ""})


def test_a_whole_section_replacement_is_accepted_on_its_own():
    out = draft_studio.apply_section_edits(GOOD["field"], {
        "edits": [], "replacement": "  The disclosure relates to lifting tools.  "})
    assert out == "The disclosure relates to lifting tools."


def test_returning_both_a_replacement_and_pairs_is_an_error_not_a_preference():
    with pytest.raises(draft_studio.SectionEditError):
        draft_studio.apply_section_edits("A cat sat.", {
            "edits": [{"find": "cat", "replace": "gripper", "why": ""}],
            "replacement": "Something else entirely."})


def test_an_answer_with_no_edits_is_refused():
    with pytest.raises(draft_studio.SectionEditError):
        draft_studio.apply_section_edits("A cat sat.", {"edits": [], "replacement": ""})


def test_an_em_dash_never_survives_a_patch():
    out = draft_studio.apply_section_edits("The tool - a gripper - lifts.", {
        "edits": [], "replacement": "The tool \u2014 a gripper \u2014 lifts."})
    assert "\u2014" not in out


# =============================================================================================
# The prompt that carries the scope
# =============================================================================================
def test_the_section_prompt_names_the_one_file_and_quotes_its_current_text():
    prompt = draft_studio.build_section_edit_prompt("field", GOOD)
    assert "Field of the Disclosure" in prompt
    assert "draft/04-field.md" in prompt
    assert GOOD["field"] in prompt


def test_an_empty_section_is_described_rather_than_quoted_as_nothing():
    prompt = draft_studio.build_section_edit_prompt("field", {"field": "   "})
    assert "(this section is empty)" in prompt


def test_a_section_key_that_is_not_a_section_is_refused():
    with pytest.raises(draft_studio.SectionEditError):
        draft_studio.build_section_edit_prompt("claims_appendix", GOOD)


def test_every_section_of_the_application_can_be_edited_on_its_own():
    for key, filename, heading in draft_workspace.SECTION_FILES:
        prompt = draft_studio.build_section_edit_prompt(key, GOOD)
        assert filename in prompt and heading in prompt


def test_the_section_agent_is_told_it_may_not_widen_the_request():
    assert "THE SCOPE IS ABSOLUTE" in draft_studio.SECTION_EDIT_SYSTEM
    assert "exactly one section" in draft_studio.SECTION_EDIT_SYSTEM
    assert "\u2014" not in draft_studio.SECTION_EDIT_SYSTEM


def test_the_patch_schema_forbids_anything_it_did_not_ask_for():
    assert draft_studio.SECTION_EDIT_SCHEMA["additionalProperties"] is False
    item = draft_studio.SECTION_EDIT_SCHEMA["properties"]["edits"]["items"]
    assert item["required"] == ["find", "replace", "why"]
    assert item["additionalProperties"] is False


# =============================================================================================
# The draft never talks about itself
# =============================================================================================
def test_the_drafting_agent_is_forbidden_from_writing_version_numbers_into_the_draft():
    assert "THE DRAFT IS THE ONLY DRAFT" in draft_studio.DRAFT_SYSTEM
    for banned in ("version or draft\nnumber", "change log", "editorial aside"):
        assert banned.replace("\n", " ") in " ".join(draft_studio.DRAFT_SYSTEM.split())


def test_the_section_agent_carries_the_same_rule():
    joined = " ".join(draft_studio.SECTION_EDIT_SYSTEM.split())
    assert "No version or draft number" in joined
    assert "no change log" in joined


# =============================================================================================
# A model per project
# =============================================================================================
def test_only_a_known_tier_can_reach_the_command_line():
    assert draft_agent.normalize_model("opus") == "opus"
    assert draft_agent.normalize_model("Fable") == "fable"
    assert draft_agent.normalize_model("gpt-5") == ""
    assert draft_agent.normalize_model("opus; rm -rf /") == ""
    assert draft_agent.normalize_model(None) == ""


def test_an_unset_model_means_the_server_default_rather_than_a_named_one():
    assert draft_agent.normalize_model("") == ""
    assert draft_agent.model_label("") == ""


def test_every_offered_model_is_one_the_normaliser_accepts():
    for choice in draft_agent.MODEL_CHOICES:
        assert draft_agent.normalize_model(choice["id"]) == choice["id"]
        assert choice["label"] and choice["detail"]


# =============================================================================================
# What History says about a version somebody typed
# =============================================================================================
def test_a_hand_edited_version_names_the_sections_that_were_touched():
    assert draft_studio._manual_change_note(["field"]) == \
        "Edited by hand: Field of the Disclosure."
    note = draft_studio._manual_change_note(["field", "claims", "abstract"])
    assert note == "Edited by hand: Field of the Disclosure, Claims and Abstract."


def test_an_unknown_key_does_not_produce_a_meaningless_note():
    assert draft_studio._manual_change_note(["not_a_section"]) == "Edited by hand."


# =============================================================================================
# The lane itself
# =============================================================================================
def _section_edit_runner(monkeypatch, tmp_path, result, *, model=""):
    repository = Mock()
    repository.save_version.return_value = {"version_no": 3}
    repository.save_qa.return_value = {
        "id": 9, "verdict": "pass", "checks": [], "findings": [], "counts": {}}
    repository.complete_turn.return_value = {"status": "complete"}
    agent = Mock()
    agent.DRAFT_MODEL = "opus"
    agent.DRAFT_TIMEOUT = 60
    agent.new_session_id.return_value = "scratch-session"
    agent.strings.side_effect = lambda value, **_kwargs: [str(v) for v in (value or []) if v]
    agent.run.return_value = draft_agent.AgentRun(
        ok=True, session_id="scratch-session", model=model or "opus",
        cost_usd=0.02, duration_ms=4000, result=result)
    workspace = Mock()
    runner = draft_studio.TurnRunner(repository, object(), agent=agent, workspace=workspace)
    monkeypatch.setattr(runner, "_load", lambda _pid: {
        "project": {"user_id": 91}, "references": [{"publication_number": ALLOWED[0]}],
        "sections": dict(GOOD), "numerals": list(NUMERALS), "figures": list(FIGURES)})
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path,
        "project": {"user_id": 91, "agent_session_id": "project-session",
                    "latest_version_no": 2, "disclosure_text": "disclosure",
                    "draft_model": model},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": True, "resuming_candidate": False,
        "previous_sections": dict(GOOD),
    })
    monkeypatch.setattr(runner, "_ensure_figures", lambda **_k: pytest.fail(
        "a section edit reached the drawing pipeline"))
    return runner, repository, agent, workspace


REVISED = {"action": "revised", "summary": "Widened the field to cover a self-propelled tool.",
           "edits": [{"find": "portable vacuum lifting tools",
                      "replace": "portable and self-propelled vacuum lifting tools",
                      "why": "the disclosure describes both"}],
           "replacement": "", "consequences": [], "answer": ""}


def test_a_section_edit_publishes_a_version_without_touching_a_drawing(monkeypatch, tmp_path):
    runner, repository, _agent, _workspace = _section_edit_runner(
        monkeypatch, tmp_path, REVISED)

    out = runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                      "kind": "section_edit", "section_key": "field", "attempts": 1})

    assert out["version"] == {"version_no": 3}
    saved = repository.save_version.call_args.kwargs
    assert saved["sections"]["field"] == \
        "The disclosure relates to portable and self-propelled vacuum lifting tools."
    #  Everything else is byte-identical, and so are the numerals and the figure specs: a sheet
    #  that could not have changed is never redrawn and can never go stale.
    assert {k: v for k, v in saved["sections"].items() if k != "field"} == \
        {k: v for k, v in GOOD.items() if k != "field"}
    assert saved["numerals"] == NUMERALS
    assert saved["figures"] == FIGURES


def test_the_section_agent_is_given_no_way_to_write_to_the_workspace(monkeypatch, tmp_path):
    """The tool set is what makes the output a patch instead of a rewrite."""
    runner, _repository, agent, _workspace = _section_edit_runner(monkeypatch, tmp_path, REVISED)

    runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                "kind": "section_edit", "section_key": "field", "attempts": 1})

    tools = agent.run.call_args.kwargs["tools"]
    assert "Write" not in tools and "Edit" not in tools
    assert agent.run.call_args.kwargs["schema"] is draft_studio.SECTION_EDIT_SCHEMA


def test_a_section_edit_reads_the_published_version_not_a_leftover_repair_candidate(
        monkeypatch, tmp_path):
    """`prepare` prefers a saved candidate so a blocked revision can resume. Here that would base
    the user's edit on text they have never seen, so the published version is re-read."""
    runner, _repository, agent, workspace = _section_edit_runner(monkeypatch, tmp_path, REVISED)
    written = []
    workspace.write_sections.side_effect = lambda _ws, sections: written.append(dict(sections))
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path,
        "project": {"user_id": 91, "agent_session_id": "", "latest_version_no": 2,
                    "disclosure_text": "d", "draft_model": ""},
        "references": [{"publication_number": ALLOWED[0]}], "documents": [],
        "seeded": False, "had_version": True, "resuming_candidate": True,
        "prepared_snapshot": {"sections": {**GOOD, "field": "Unpublished candidate text."},
                              "numerals": [], "figures": []},
        "previous_sections": dict(GOOD),
    })

    runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                "kind": "section_edit", "section_key": "field", "attempts": 1})

    #  What the workspace was set to, and what the agent was actually shown, are both the
    #  published text rather than the candidate nobody has seen.
    assert written and written[0]["field"] == GOOD["field"]
    assert GOOD["field"] in agent.run.call_args.kwargs["prompt"]
    assert "Unpublished candidate text." not in agent.run.call_args.kwargs["prompt"]


def test_the_chosen_model_reaches_the_run(monkeypatch, tmp_path):
    runner, _repository, agent, _workspace = _section_edit_runner(
        monkeypatch, tmp_path, REVISED, model="haiku")

    runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                "kind": "section_edit", "section_key": "field", "attempts": 1})

    assert agent.run.call_args.kwargs["model"] == "haiku"


def test_a_question_about_one_section_answers_without_publishing(monkeypatch, tmp_path):
    runner, repository, _agent, _workspace = _section_edit_runner(monkeypatch, tmp_path, {
        "action": "answered", "summary": "", "edits": [], "replacement": "",
        "consequences": [], "answer": "It is there to carry the pump."})

    out = runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                      "kind": "section_edit", "section_key": "field", "attempts": 1})

    assert out["version"] is None
    repository.save_version.assert_not_called()


def test_an_edit_that_changes_nothing_is_reported_rather_than_stored(monkeypatch, tmp_path):
    runner, repository, _agent, _workspace = _section_edit_runner(monkeypatch, tmp_path, {
        **REVISED, "edits": [], "replacement": GOOD["field"]})

    with pytest.raises(draft_studio.SectionEditError):
        runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                    "kind": "section_edit", "section_key": "field", "attempts": 1})
    repository.save_version.assert_not_called()


def test_a_scoped_edit_that_would_break_the_application_is_still_refused(monkeypatch, tmp_path):
    """The whole draft is validated, not only the edited section: a citation to a document this
    project was never given is exactly as unfilable however small the change was."""
    runner, repository, _agent, _workspace = _section_edit_runner(monkeypatch, tmp_path, {
        **REVISED, "edits": [], "replacement": "The disclosure relates to lifting "
                                               "tools [REF:US-9999999-B2]."})

    with pytest.raises(Exception):
        runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                    "kind": "section_edit", "section_key": "field", "attempts": 1})
    repository.save_version.assert_not_called()


def test_a_first_draft_is_never_diverted_into_the_section_lane(monkeypatch, tmp_path):
    """There is nothing to patch before a version exists, so the request must run the full path."""
    runner, _repository, _agent, _workspace = _section_edit_runner(
        monkeypatch, tmp_path, REVISED)
    monkeypatch.setattr(runner, "prepare", lambda _turn: {
        "workspace": tmp_path,
        "project": {"user_id": 91, "agent_session_id": "", "latest_version_no": 0,
                    "disclosure_text": "d", "draft_model": ""},
        "references": [], "documents": [], "seeded": False, "had_version": False,
        "resuming_candidate": False, "previous_sections": {},
    })
    monkeypatch.setattr(runner, "_run_section_edit", lambda *_a, **_k: pytest.fail(
        "a project with no version was sent to the section lane"))
    monkeypatch.setattr(runner, "_run_agent", lambda **_k: (_ for _ in ()).throw(
        RuntimeError("full path")))

    with pytest.raises(RuntimeError, match="full path"):
        runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 1,
                    "kind": "section_edit", "section_key": "field", "attempts": 1})


# =============================================================================================
# The database must accept every kind the code writes
# =============================================================================================
def test_the_kind_constraint_allows_every_kind_the_code_enqueues():
    """It did not, and both offenders failed at INSERT with nothing readable saying why."""
    import pathlib
    import re

    import draft_studio_service

    sql = (pathlib.Path(draft_studio.__file__).resolve().parents[1] /
           "sql" / "019_draft_turn_kinds.sql").read_text(encoding="utf-8")
    allowed = set(re.findall(r"'([a-z_]+)'::text", sql))

    source = pathlib.Path(draft_studio_service.__file__).read_text(encoding="utf-8")
    #  The list start_turn will accept from a request, read from the code rather than restated.
    accepted = re.search(r'if kind not in \(([^)]*)\)', source).group(1)
    enqueued = set(re.findall(r'"([a-z_]+)"', accepted))
    #  Plus the kind the automatic filing-repair continuation enqueues on the worker's own behalf.
    enqueued |= {"gate_resume"}

    assert enqueued, "no turn kinds were found in the service"
    assert enqueued <= allowed, f"kinds the constraint would reject: {sorted(enqueued - allowed)}"
    assert {"section_edit", "gate_resume"} <= allowed


# =============================================================================================
# Refuse what the edit broke, carry what it inherited
# =============================================================================================
def test_a_clean_application_has_no_problems_to_report():
    assert draft_studio.section_problems(GOOD, ALLOWED) == []


def test_every_defect_class_is_reported_rather_than_only_the_first():
    broken = {**GOOD, "government_support": "",
              "cross_reference": "[DRAFTING NOTE: confirm priority]",
              "abstract": "A tool that is patentable over the art."}
    problems = draft_studio.section_problems(broken, ALLOWED)
    joined = " | ".join(problems)
    assert "Statement Regarding Federally Sponsored Research or Development is empty." in problems
    assert "placeholder" in joined
    assert "legal conclusion" in joined
    assert len(problems) >= 3


def test_a_citation_this_project_was_never_given_is_a_problem():
    problems = draft_studio.section_problems(
        {**GOOD, "background": "Known [REF:US-9999999-B2]."}, ALLOWED)
    assert any("US-9999999-B2" in item for item in problems)


def test_a_defect_the_edit_inherited_does_not_block_it(monkeypatch, tmp_path):
    """A placeholder somebody left in the Cross-Reference two months ago must not make every other
    section uneditable for ever. That is exactly what it did, to a request that only wanted a
    phrase removed from the Field of the Disclosure."""
    older = {**GOOD, "government_support": "",
             "cross_reference": "[DRAFTING NOTE: Priority status is not confirmed.]"}
    runner, repository, _agent, _workspace = _section_edit_runner(monkeypatch, tmp_path, REVISED)
    monkeypatch.setattr(runner, "_load", lambda _pid: {
        "project": {"user_id": 91}, "references": [{"publication_number": ALLOWED[0]}],
        "sections": older, "numerals": list(NUMERALS), "figures": list(FIGURES)})

    runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                "kind": "section_edit", "section_key": "field", "attempts": 1})

    saved = repository.save_version.call_args.kwargs
    assert "self-propelled" in saved["sections"]["field"]
    assert saved["sections"]["cross_reference"] == older["cross_reference"]
    #  And the user is told, rather than the inherited defects quietly disappearing.
    report = repository.save_qa.call_args.kwargs["report"]
    inherited = next(c for c in report["checks"]
                     if c["name"] == "Defects this change inherited rather than caused")
    assert any("placeholder" in item for item in inherited["items"])
    assert inherited["status"] == "fail"


def test_a_defect_the_edit_introduces_is_still_refused(monkeypatch, tmp_path):
    older = {**GOOD, "cross_reference": "[DRAFTING NOTE: confirm priority]"}
    runner, repository, _agent, _workspace = _section_edit_runner(monkeypatch, tmp_path, {
        **REVISED, "edits": [],
        "replacement": "The disclosure relates to a tool that is patentable over the art."})
    monkeypatch.setattr(runner, "_load", lambda _pid: {
        "project": {"user_id": 91}, "references": [{"publication_number": ALLOWED[0]}],
        "sections": older, "numerals": list(NUMERALS), "figures": list(FIGURES)})

    with pytest.raises(drafting.DraftingValidationError) as caught:
        runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                    "kind": "section_edit", "section_key": "field", "attempts": 1})
    assert "legal conclusion" in str(caught.value)
    repository.save_version.assert_not_called()


def test_emptying_the_edited_section_is_refused_even_on_a_broken_draft(monkeypatch, tmp_path):
    older = {**GOOD, "government_support": ""}
    runner, repository, _agent, _workspace = _section_edit_runner(monkeypatch, tmp_path, {
        **REVISED, "edits": [{"find": GOOD["field"], "replace": "", "why": ""}],
        "replacement": ""})
    monkeypatch.setattr(runner, "_load", lambda _pid: {
        "project": {"user_id": 91}, "references": [{"publication_number": ALLOWED[0]}],
        "sections": older, "numerals": list(NUMERALS), "figures": list(FIGURES)})

    with pytest.raises(Exception):
        runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                    "kind": "section_edit", "section_key": "field", "attempts": 1})
    repository.save_version.assert_not_called()


def test_a_scoped_edit_leaves_every_other_section_byte_for_byte(monkeypatch, tmp_path):
    """The first live run turned an em dash into a hyphen in two sections nobody had named. A house
    rule doing the right thing in the wrong place is still a rewrite the user did not ask for."""
    untouched = {**GOOD,
                 "background": "Handheld lifters \u2014 the older sort \u2014 are known.  ",
                 "detailed_description": GOOD["detailed_description"] + "\u2014"}
    runner, repository, _agent, _workspace = _section_edit_runner(monkeypatch, tmp_path, REVISED)
    monkeypatch.setattr(runner, "_load", lambda _pid: {
        "project": {"user_id": 91}, "references": [{"publication_number": ALLOWED[0]}],
        "sections": untouched, "numerals": list(NUMERALS), "figures": list(FIGURES)})

    runner.run({"id": 12, "lease_token": "lease", "project_id": 7, "turn_no": 5,
                "kind": "section_edit", "section_key": "field", "attempts": 1})

    saved = repository.save_version.call_args.kwargs["sections"]
    changed = [key for key in untouched if untouched[key] != saved.get(key)]
    assert changed == ["field"]
    assert saved["background"] == untouched["background"]
    assert saved["detailed_description"] == untouched["detailed_description"]
