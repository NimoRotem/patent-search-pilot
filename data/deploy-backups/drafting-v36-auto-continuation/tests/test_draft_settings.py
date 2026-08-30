"""Advanced settings: every knob is real, and a bad value is refused rather than corrected."""
import pytest

import draft_agent
import draft_settings


def test_a_project_that_saved_nothing_gets_todays_defaults():
    out = draft_settings.resolve(None)
    assert out["finalization_rounds"] == 6
    assert out["research_depth"] == "deep"
    assert out["draft_model"] == ""


def test_a_project_saved_before_a_field_existed_still_gets_that_fields_default():
    """The whole reason these live in one jsonb column rather than a column each."""
    out = draft_settings.resolve({"draft_model": "sonnet"})
    assert out["draft_model"] == "sonnet"
    assert out["research_references"] == draft_settings.BY_KEY["research_references"]["default"]


def test_an_unknown_setting_is_refused_rather_than_ignored():
    with pytest.raises(ValueError, match="not a setting"):
        draft_settings.clean({"temperature": 0.7})


def test_a_model_this_server_does_not_run_is_refused():
    with pytest.raises(ValueError, match="not a model"):
        draft_settings.clean({"draft_model": "gpt-5"})
    assert draft_settings.clean({"draft_model": "Haiku"})["draft_model"] == "haiku"


def test_a_number_outside_its_range_is_refused_not_clamped():
    """Silently clamping is how a settings page starts lying: the field shows what you typed and
    the system uses something else."""
    with pytest.raises(ValueError, match="between 2 and 6"):
        draft_settings.clean({"finalization_rounds": 99})
    with pytest.raises(ValueError, match="whole number"):
        draft_settings.clean({"finalization_rounds": "lots"})
    assert draft_settings.clean({"finalization_rounds": 3})["finalization_rounds"] == 3


def test_a_choice_that_is_not_offered_is_refused():
    with pytest.raises(ValueError, match="not one of the choices"):
        draft_settings.clean({"research_depth": "exhaustive"})


def test_free_text_is_length_checked_and_trimmed():
    with pytest.raises(ValueError, match="longer than"):
        draft_settings.clean({"style_notes": "x" * 5000})
    assert draft_settings.clean({"style_notes": "  no em dashes  "})["style_notes"] == \
        "no em dashes"


def test_saving_one_setting_leaves_the_others_where_they_were():
    stored = {"draft_model": "opus", "finalization_rounds": 3}
    out = draft_settings.clean({"style_notes": "British spelling"}, stored)
    assert out["draft_model"] == "opus"
    assert out["finalization_rounds"] == 3
    assert out["style_notes"] == "British spelling"


def test_the_house_instructions_never_read_as_permission_to_override_the_disclosure():
    text = draft_settings.prompt_additions({"style_notes": "Two independent claims maximum."})
    assert "Two independent claims maximum." in text
    assert "never override the inventor's disclosure" in text
    assert "—" not in text


def test_no_house_instructions_adds_nothing_at_all():
    assert draft_settings.prompt_additions({}) == ""
    assert draft_settings.prompt_additions(None) == ""


def test_every_field_explains_itself_and_offers_only_real_choices():
    shown = draft_settings.public({})
    keys = {field["key"] for field in shown["fields"]}
    assert keys == set(draft_settings.BY_KEY)
    for field in shown["fields"]:
        assert field["help"] and field["label"]
        assert "—" not in field["help"]
        if field["kind"] == "model":
            offered = {choice["id"] for choice in field["choices"]} - {""}
            assert offered <= draft_agent.MODEL_IDS, "a model the server cannot run was offered"
