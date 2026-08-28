"""What the drafting agent is allowed to put through a model, and why it is this small.

MEASURED, before any of this: one turn read 192,131,508 tokens to produce 162,384 - 1,183 read for
every token written - because every run RESUMED the project's Claude Code session. Turn 55 of that
project began with 546,614 tokens of inherited transcript before it opened a file, and ended at
771,916. The whole workspace is 45,000 tokens.

These tests pin the three things that made it so, so none of them can come back quietly.
"""
import inspect

import draft_studio
import draft_workspace


def test_a_run_never_resumes_another_turns_session():
    """The session was resumed from the PROJECT, so one conversation accumulated across every turn
    the project had ever had, and could only grow."""
    source = inspect.getsource(draft_studio.TurnRunner.run)
    assert 'prior_session = ""' in source, (
        "the drafting run is resuming a session again; the context will grow without bound")
    assert 'project.get("agent_session_id")' not in source, (
        "the project's session id is being read back into a run")


def test_a_repair_round_starts_fresh_too():
    """Six rounds each inheriting the last one's tool calls is where the growth compounded. The
    review report on disk is the designed hand-off and carries what the next round needs."""
    source = inspect.getsource(draft_studio.TurnRunner.run)
    assert "prompt=FINALIZE_PROMPT, session_id=self.agent.new_session_id()" in source
    assert "resume=True" not in source, "a round is resuming the previous round"


def test_the_conversation_given_to_the_agent_is_bounded():
    """It reached 38,000 characters on a real project and was re-read on every run of every round."""
    messages = [{"role": "user", "body": f"message {i} " + "x" * 3000} for i in range(200)]
    out = draft_workspace._conversation(messages)
    assert len(out) <= draft_workspace.MAX_CONVERSATION_CHARS + 400
    #  And it says so, rather than looking like the whole conversation.
    assert "not reproduced" in out


def test_a_short_conversation_is_passed_through_whole():
    messages = [{"role": "user", "body": "narrow claim 1"},
                {"role": "agent", "body": "done"}]
    out = draft_workspace._conversation(messages)
    assert "narrow claim 1" in out and "done" in out
    assert "not reproduced" not in out


def test_the_most_recent_exchange_survives_truncation():
    """Keep the end. What was most recently asked is what the turn is about."""
    messages = [{"role": "user", "body": f"old {i} " + "x" * 3000} for i in range(100)]
    messages.append({"role": "user", "body": "THE LATEST REQUEST"})
    out = draft_workspace._conversation(messages)
    assert "THE LATEST REQUEST" in out


def test_the_agent_is_told_to_read_what_it_needs_rather_than_the_tree():
    assert "READ WHAT THE REQUEST NEEDS, NOT THE WHOLE WORKSPACE" in draft_studio.DRAFT_SYSTEM
    assert "READ ONLY WHAT YOU NEED" in draft_studio.FINALIZE_PROMPT
    for text in (draft_studio.DRAFT_SYSTEM, draft_studio.FINALIZE_PROMPT):
        assert "—" not in text
