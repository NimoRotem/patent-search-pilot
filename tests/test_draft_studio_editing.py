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
        "edits": [], "replacement": "The tool — a gripper — lifts."})
    assert "—" not in out


# =============================================================================================
# The prompt that carries the scope
# =============================================================================================
def test_the_section_prompt_names_the_one_file_and_quotes_its_current_text():
    prompt, patched = draft_studio.build_section_edit_prompt("field", GOOD)
    assert not patched, "there was no workspace to take material from"
    assert "Field of the Disclosure" in prompt
    assert "draft/04-field.md" in prompt
    assert GOOD["field"] in prompt


def test_an_empty_section_is_described_rather_than_quoted_as_nothing():
    prompt, _ = draft_studio.build_section_edit_prompt("field", {"field": "   "})
    assert "(this section is empty)" in prompt


def test_a_section_key_that_is_not_a_section_is_refused():
    with pytest.raises(draft_studio.SectionEditError):
        draft_studio.build_section_edit_prompt("claims_appendix", GOOD)


def test_every_section_of_the_application_can_be_edited_on_its_own():
    for key, filename, heading in draft_workspace.SECTION_FILES:
        prompt, _ = draft_studio.build_section_edit_prompt(key, GOOD)
        assert filename in prompt and heading in prompt


def test_the_section_agent_is_told_it_may_not_widen_the_request():
    assert "THE SCOPE IS ABSOLUTE" in draft_studio.SECTION_EDIT_SYSTEM
    assert "exactly one section" in draft_studio.SECTION_EDIT_SYSTEM
    assert "—" not in draft_studio.SECTION_EDIT_SYSTEM


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
                 "background": "Handheld lifters — the older sort — are known.  ",
                 "detailed_description": GOOD["detailed_description"] + "—"}
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


# =================================================================================================
# The repair round returns a patch, and a patch that misses is refused whole
# =================================================================================================
def _repair_workspace(tmp_path):
    (tmp_path / "draft").mkdir()
    (tmp_path / "figures").mkdir()
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "disclosure.md").write_text("The inventor disclosed a gripper.")
    (tmp_path / "draft" / "09-claims.md").write_text(
        "1. A device comprising a body and a seal.\n\n2. The device of claim 1, wherein the "
        "seal is elastomeric.\n")
    (tmp_path / "draft" / "numerals.md").write_text("| 10 | body |\n| 12 | seal |\n")
    return tmp_path


def test_a_repair_patch_changes_only_what_it_names(tmp_path):
    ws = _repair_workspace(tmp_path)
    before = (ws / "draft" / "numerals.md").read_text()

    touched = draft_studio.apply_repair_patches(ws, {
        "patches": [{"path": "draft/09-claims.md", "find": "a body and a seal",
                     "replace": "a body 10 and a seal 12", "why": "numerals"}]})

    assert touched == ["draft/09-claims.md"]
    assert "a body 10 and a seal 12" in (ws / "draft" / "09-claims.md").read_text()
    assert "wherein the seal is elastomeric" in (ws / "draft" / "09-claims.md").read_text()
    assert (ws / "draft" / "numerals.md").read_text() == before, "it touched a file it did not name"


def test_one_patch_that_misses_refuses_the_whole_set(tmp_path):
    """Applying the ones that fit would publish a draft nobody wrote and nobody read back, and the
    miss would surface at the next gate looking like a fresh defect."""
    ws = _repair_workspace(tmp_path)
    before = (ws / "draft" / "09-claims.md").read_text()

    with pytest.raises(draft_studio.RepairPatchError, match="not in draft/09-claims.md"):
        draft_studio.apply_repair_patches(ws, {"patches": [
            {"path": "draft/09-claims.md", "find": "a body and a seal",
             "replace": "a body 10 and a seal 12", "why": "numerals"},
            {"path": "draft/09-claims.md", "find": "a hydraulic actuator",
             "replace": "an actuator", "why": "not in this draft at all"}]})

    assert (ws / "draft" / "09-claims.md").read_text() == before


def test_an_ambiguous_patch_is_refused_rather_than_applied_to_the_first_match(tmp_path):
    ws = _repair_workspace(tmp_path)
    with pytest.raises(draft_studio.RepairPatchError, match="appears 2 times"):
        draft_studio.apply_repair_patches(ws, {"patches": [
            {"path": "draft/09-claims.md", "find": "device", "replace": "apparatus",
             "why": "x"}]})


def test_a_wrapped_quote_still_applies_when_it_is_unambiguous(tmp_path):
    """The usual near miss: a paragraph quoted back with its line breaks collapsed."""
    ws = _repair_workspace(tmp_path)
    (ws / "draft" / "05-background.md").write_text("A known gripper\nloses suction on a rough\nface.")
    draft_studio.apply_repair_patches(ws, {"patches": [
        {"path": "draft/05-background.md",
         "find": "A known gripper loses suction on a rough face.",
         "replace": "A known gripper loses vacuum on a rough face.", "why": "wording"}]})
    assert "loses vacuum" in (ws / "draft" / "05-background.md").read_text()


def test_a_repair_may_not_write_outside_the_draft_and_the_figures(tmp_path):
    ws = _repair_workspace(tmp_path)
    for path in ("input/disclosure.md", "../../etc/passwd", "prior_art/US-1.md"):
        with pytest.raises(draft_studio.RepairPatchError):
            draft_studio.apply_repair_patches(ws, {"rewrites": [
                {"path": path, "text": "anything", "why": "x"}]})
    assert (ws / "input" / "disclosure.md").read_text() == "The inventor disclosed a gripper."


def test_a_rewrite_may_not_empty_a_file(tmp_path):
    ws = _repair_workspace(tmp_path)
    with pytest.raises(draft_studio.RepairPatchError, match="would empty"):
        draft_studio.apply_repair_patches(ws, {"rewrites": [
            {"path": "draft/09-claims.md", "text": "   ", "why": "x"}]})


def test_a_repair_that_returns_nothing_is_a_failure_not_a_no_op(tmp_path):
    ws = _repair_workspace(tmp_path)
    with pytest.raises(draft_studio.RepairPatchError, match="no change to apply"):
        draft_studio.apply_repair_patches(ws, {"patches": [], "rewrites": []})


def test_the_repair_prompt_carries_the_material_and_the_report(tmp_path):
    ws = _repair_workspace(tmp_path)
    prompt, patched = draft_studio.build_repair_prompt(ws, {
        "checks": [{"name": "Numerals", "status": "fail", "detail": "10 undefined",
                    "items": ["10"]},
                   {"name": "Claims", "status": "pass", "detail": "fine", "items": []}],
        "findings": [{"title": "Claim 2 lacks antecedent basis", "where": "draft/09-claims.md",
                      "detail": "the seal", "evidence": "claim 2", "fix": "add it"}]})

    assert patched
    assert "wherein the seal is elastomeric" in prompt, "the draft did not travel with the prompt"
    assert "The inventor disclosed a gripper." in prompt
    assert "10 undefined" in prompt and "Claim 2 lacks antecedent basis" in prompt
    assert "Claims: fine" not in prompt, "it spent tokens on the checks that passed"


def test_a_workspace_too_large_to_hand_over_keeps_the_old_repair(tmp_path, monkeypatch):
    ws = _repair_workspace(tmp_path)
    monkeypatch.setattr(draft_workspace, "MAX_MATERIAL_CHARS", 10)
    prompt, patched = draft_studio.build_repair_prompt(ws, {})
    assert not patched and prompt == ""


# =================================================================================================
# A revision turn is a patch too
# =================================================================================================
def test_a_revision_is_handed_the_application_and_the_request(tmp_path):
    ws = _repair_workspace(tmp_path)
    (ws / "input" / "request.md").write_text("Make claim 1 broader.")

    prompt, patched = draft_studio.build_revision_prompt(ws)

    assert patched
    assert "Make claim 1 broader." in prompt
    assert "wherein the seal is elastomeric" in prompt, "the draft did not travel with the prompt"
    assert "The inventor disclosed a gripper." in prompt
    assert "the last review of this draft" not in prompt, "it invented a review that never ran"


def test_a_revision_carries_the_previous_review_when_there_was_one(tmp_path):
    ws = _repair_workspace(tmp_path)
    (ws / "input" / "request.md").write_text("Shorten the background.")
    prompt, patched = draft_studio.build_revision_prompt(ws, {
        "checks": [{"name": "Numerals", "status": "fail", "detail": "10 undefined",
                    "items": ["10"]},
                   {"name": "Claims", "status": "pass", "detail": "fine", "items": []}]})
    assert patched and "10 undefined" in prompt
    assert "Claims: fine" not in prompt, "it paid to be told what already passes"


def test_a_revision_that_answers_a_question_returns_no_patch(tmp_path):
    """A question about the draft is not a change to it, and must not touch a file."""
    ws = _repair_workspace(tmp_path)
    before = (ws / "draft" / "09-claims.md").read_text()
    result = {"action": "answered", "answer": "Claim 1 covers the body and the seal.",
              "patches": [], "rewrites": []}
    assert result["action"] == "answered"
    with pytest.raises(draft_studio.RepairPatchError):
        draft_studio.apply_repair_patches(ws, result)
    assert (ws / "draft" / "09-claims.md").read_text() == before


def test_the_revision_schema_keeps_the_prior_art_strategy_and_drops_the_prose(tmp_path):
    required = set(draft_studio.REVISION_SCHEMA["required"])
    assert "prior_art_strategy" in required and "answer" in required
    assert "reasoning" not in required, "it is paying for prose the per-change why already gives"


def test_a_workspace_too_large_to_hand_over_keeps_the_old_revision(tmp_path, monkeypatch):
    ws = _repair_workspace(tmp_path)
    monkeypatch.setattr(draft_workspace, "MAX_MATERIAL_CHARS", 10)
    prompt, patched = draft_studio.build_revision_prompt(ws)
    assert not patched and prompt == ""


# =================================================================================================
# The material is ordered so a prompt cache can hit
# =================================================================================================
def test_the_draft_comes_last_so_the_unchanging_prefix_is_longest(tmp_path):
    """A prompt cache is a PREFIX match: the first differing byte discards everything after it.
    Within a turn the draft is rewritten up to six times and nothing else moves."""
    ws = _repair_workspace(tmp_path)
    (ws / "input" / "request.md").write_text("do a thing")
    (ws / "figures" / "FIG-1.md").write_text("a sheet")

    order = list(draft_workspace.text_materials(ws))

    assert order[0] == "input/disclosure.md", "the one file that never changes was not first"
    assert order.index("input/request.md") < order.index("figures/FIG-1.md")
    assert all(name.startswith("draft/") for name in order[-2:])
    assert order.index("figures/FIG-1.md") < order.index("draft/09-claims.md")


def test_a_section_edit_with_a_workspace_is_handed_the_whole_application(tmp_path):
    """It used to open with a reading list: the rest of draft/, the disclosure, the conversation,
    the prior-art index. Eight or nine tool calls before a word is written."""
    ws = _repair_workspace(tmp_path)
    (ws / "input" / "request.md").write_text("Say suction cup, not vacuum cup.")
    (ws / "draft" / "04-field.md").write_text("This relates to lifting devices.")

    prompt, patched = draft_studio.build_section_edit_prompt(
        "field", {"field": "This relates to lifting devices."}, ws)

    assert patched
    assert "Say suction cup, not vacuum cup." in prompt
    assert "wherein the seal is elastomeric" in prompt, "it cannot see the claims it must not break"
    assert "The inventor disclosed a gripper." in prompt
    assert "Read before you write anything" not in prompt, "still sending it to fetch its own files"
    assert "patent_lookup.py" in prompt, "the one thing not in the prompt was not mentioned"


def test_a_workspace_too_large_to_hand_over_keeps_the_reading_list(tmp_path, monkeypatch):
    ws = _repair_workspace(tmp_path)
    monkeypatch.setattr(draft_workspace, "MAX_MATERIAL_CHARS", 10)
    prompt, patched = draft_studio.build_section_edit_prompt("field", GOOD, ws)
    assert not patched and "Read before you write anything" in prompt


# =================================================================================================
# An advisory is reported, not enforced
# =================================================================================================
def test_an_advisory_check_does_not_block_a_turn():
    """Three checks are advisory on purpose because each is a heuristic that false-positives on
    correct drafting. Blocking on them made the repair agent rewrite good claim language to
    satisfy a regex, up to six rounds deep with two reviews between each."""
    report = {"status": "complete", "checks": [
        {"name": "Claim terms appear in the description", "status": "warn",
         "severity": "advisory", "detail": "2 claim words do not appear", "items": ["a", "b"]}]}
    assert draft_studio.text_blockers(report) == []
    assert draft_studio.filing_blockers(report) == []


def test_a_real_defect_still_blocks():
    report = {"status": "complete", "checks": [
        {"name": "Claims are numbered", "status": "fail", "severity": "error",
         "detail": "claim 3 is missing", "items": []}]}
    assert draft_studio.text_blockers(report)
    assert draft_studio.filing_blockers(report)


def test_a_check_with_no_severity_still_blocks():
    """Absent means unknown, and unknown is not permission to publish."""
    report = {"status": "complete", "checks": [
        {"name": "Something", "status": "fail", "detail": "x", "items": []}]}
    assert draft_studio.text_blockers(report)


def test_the_three_advisory_checks_are_all_still_advisory():
    """If one of these stops being advisory it starts blocking again, silently."""
    import draft_qa
    advisory = {"Numerals are introduced with their part name",
                "Antecedent basis in the claims",
                "Claim terms appear in the description"}
    checks = draft_qa.run_checks(sections=GOOD, numerals=NUMERALS, figures=FIGURES,
                                 allowed_references=ALLOWED)
    seen = {c["name"] for c in checks if str(c.get("severity") or "") == "advisory"}
    assert advisory <= seen, f"a check stopped being advisory: {advisory - seen}"


def test_an_advisory_is_still_reported_so_a_person_can_judge_it():
    report = {"status": "complete", "checks": [
        {"name": "Antecedent basis in the claims", "status": "warn", "severity": "advisory",
         "detail": "claim 4: the flange", "items": ["claim 4"]}]}
    assert draft_studio.text_blockers(report) == []
    assert report["checks"][0]["detail"], "the advisory must survive into the report"
