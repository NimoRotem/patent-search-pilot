"""Bound the context supplied to each autonomous drafting pass."""

import inspect

import draft_studio
import draft_workspace


def test_a_run_never_resumes_another_turns_session():
    source = inspect.getsource(draft_studio.TurnRunner.run)

    assert 'prior_session = ""' in source
    assert 'project.get("agent_session_id")' not in source


def test_a_repair_round_starts_fresh_too():
    source = inspect.getsource(draft_studio.TurnRunner.run)

    assert "prompt=FINALIZE_PROMPT, session_id=self.agent.new_session_id()" in source
    assert "resume=True" not in source


def test_the_conversation_given_to_the_agent_is_bounded():
    messages = [
        {"role": "user", "body": f"message {index} " + "x" * 3000}
        for index in range(200)
    ]

    out = draft_workspace._conversation(messages)

    assert len(out) <= draft_workspace.MAX_CONVERSATION_CHARS + 400
    assert "not reproduced" in out


def test_a_short_conversation_is_passed_through_whole():
    messages = [
        {"role": "user", "body": "narrow claim 1"},
        {"role": "agent", "body": "done"},
    ]

    out = draft_workspace._conversation(messages)

    assert "narrow claim 1" in out and "done" in out
    assert "not reproduced" not in out


def test_the_most_recent_exchange_survives_truncation():
    messages = [
        {"role": "user", "body": f"old {index} " + "x" * 3000}
        for index in range(100)
    ]
    messages.append({"role": "user", "body": "THE LATEST REQUEST"})

    assert "THE LATEST REQUEST" in draft_workspace._conversation(messages)


def test_the_agent_is_told_to_read_only_relevant_files():
    assert "READ WHAT THE REQUEST NEEDS, NOT THE WHOLE WORKSPACE" in draft_studio.DRAFT_SYSTEM
    assert "READ ONLY WHAT YOU NEED" in draft_studio.FINALIZE_PROMPT
    forbidden = chr(0x2014)
    for text in (draft_studio.DRAFT_SYSTEM, draft_studio.FINALIZE_PROMPT):
        assert forbidden not in text
