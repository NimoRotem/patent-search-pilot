"""The counter: what it reads out of a transcript, and what it refuses to count twice.

A usage counter is only worth having if it is exact, so the tests that matter here are the ones
that feed it a transcript and check the arithmetic, and the one that appends to that transcript
and checks the second reading adds only the new part.
"""
from __future__ import annotations

import io
import json

import draft_usage


def _event(model="claude-opus-5", **usage):
    block = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0}
    block.update(usage)
    return json.dumps({"type": "assistant",
                       "message": {"model": model, "usage": block}}) + "\n"


def _transcript(*lines: str) -> io.BytesIO:
    return io.BytesIO("".join(lines).encode("utf-8"))


def test_the_usage_of_every_assistant_message_is_totalled_per_model():
    handle = _transcript(
        _event(input_tokens=10, output_tokens=5),
        _event(model="claude-sonnet-5", cache_read_input_tokens=1000),
        _event(input_tokens=1, cache_creation_input_tokens=200))
    summary = draft_usage._sum_usage(handle, 0)
    opus = summary["models"]["claude-opus-5"]
    assert opus["calls"] == 2
    assert opus["tokens_input"] == 11
    assert opus["tokens_output"] == 5
    assert opus["tokens_cache_write"] == 200
    assert summary["models"]["claude-sonnet-5"]["tokens_cache_read"] == 1000


def test_a_synthetic_message_is_not_a_model_call():
    """The CLI writes one of these for an interruption or an error. It carries a zero usage block
    and no model, and counting it inflates the call count on every cancelled turn."""
    handle = _transcript(_event(model="<synthetic>"), _event(input_tokens=7))
    summary = draft_usage._sum_usage(handle, 0)
    assert list(summary["models"]) == ["claude-opus-5"]
    assert summary["models"]["claude-opus-5"]["calls"] == 1


def test_a_line_still_being_written_is_left_for_the_next_read():
    """A session being written to right now ends mid-line. Counting the fragment loses it; not
    counting the bytes reads it twice. The offset stops at the last newline."""
    whole = _event(input_tokens=100)
    partial = whole[:-6]
    handle = _transcript(whole, partial)
    summary = draft_usage._sum_usage(handle, 0)
    assert summary["read"] == len(whole.encode("utf-8"))
    assert summary["models"]["claude-opus-5"]["tokens_input"] == 100

    #  The next read starts where the last one stopped and picks the line up whole.
    handle = _transcript(whole, whole)
    second = draft_usage._sum_usage(handle, summary["read"])
    assert second["models"]["claude-opus-5"]["tokens_input"] == 100


def test_rubbish_between_two_good_lines_is_skipped_rather_than_fatal():
    handle = _transcript(_event(input_tokens=3), "not json at all\n", _event(output_tokens=4))
    summary = draft_usage._sum_usage(handle, 0)
    assert summary["models"]["claude-opus-5"]["calls"] == 2


def test_a_message_with_no_usage_block_is_not_a_call():
    handle = io.BytesIO(json.dumps(
        {"type": "user", "message": {"content": "hello"}}).encode("utf-8") + b"\n")
    assert draft_usage._sum_usage(handle, 0)["models"] == {}


def test_the_project_folder_match_does_not_confuse_p1_with_p18(tmp_path):
    root = tmp_path / "projects"
    for name in ("-srv-patent-drafts-p1", "-srv-patent-drafts-p18",
                 "-srv-patent-drafts-filing-qa-p1", "-srv-something-else"):
        (root / name).mkdir(parents=True)
    found = {path.name for path in draft_usage._project_folders(root, 1)}
    assert found == {"-srv-patent-drafts-p1", "-srv-patent-drafts-filing-qa-p1"}
    assert {path.name for path in draft_usage._project_folders(root, 18)} == \
        {"-srv-patent-drafts-p18"}


def test_the_price_is_the_published_rate_for_the_model_named():
    cheap = draft_usage.price("claude-haiku-4-5", {"tokens_output": 1_000_000})
    dear = draft_usage.price("claude-opus-5", {"tokens_output": 1_000_000})
    assert 0 < cheap < dear
    #  A cache read is a fraction of a fresh input token, and hiding that is how a session that
    #  re-reads its whole context every round looks cheap.
    fresh = draft_usage.price("claude-opus-5", {"tokens_input": 1_000_000})
    cached = draft_usage.price("claude-opus-5", {"tokens_cache_read": 1_000_000})
    assert cached < fresh


def test_an_unknown_model_is_priced_high_rather_than_free():
    assert draft_usage.price("some-model-nobody-listed", {"tokens_output": 1_000_000}) > 0


def test_a_token_count_is_read_rather_than_audited():
    assert draft_usage.compact(0) == "0"
    assert draft_usage.compact(940) == "940"
    assert draft_usage.compact(1_240) == "1.2k"
    assert draft_usage.compact(24_000) == "24k"
    assert draft_usage.compact(470_510_446) == "471M"
    assert draft_usage.compact(1_254_120_535) == "1.3B"


def test_both_agent_layouts_are_found(tmp_path, monkeypatch):
    """The two agents are configured differently and the difference is invisible until you look.

    The headless runner points CLAUDE_CONFIG_DIR straight at a shared `.agent-home`. The
    interactive terminal is given a whole private HOME and its config directory is `.claude`
    INSIDE it, one level deeper. Reading only the first layout reports the agent that spends the
    most as having spent nothing at all.
    """
    import draft_workspace
    monkeypatch.setattr(draft_workspace, "root", lambda: tmp_path)
    (tmp_path / "p7" / ".agent-home" / ".claude" / "projects" / "-x-p7").mkdir(parents=True)
    (tmp_path / ".agent-home" / "projects" / "-x-p7").mkdir(parents=True)
    (tmp_path / "filing-qa" / ".agent-home" / "projects" / "-x-filing-qa-p7").mkdir(parents=True)
    found = {source for source, _path in draft_usage.transcript_roots(7)}
    assert found == {"terminal", "turn", "filing_qa"}


def test_a_directory_that_does_not_exist_is_not_reported(tmp_path, monkeypatch):
    import draft_workspace
    monkeypatch.setattr(draft_workspace, "root", lambda: tmp_path)
    assert draft_usage.transcript_roots(7) == []
