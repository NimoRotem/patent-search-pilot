from pathlib import Path

import pytest

import draft_agent


@pytest.fixture(autouse=True)
def one_known_subscription(monkeypatch):
    """No test reaches this host's real credential, and none of them calls Vertex for real.

    Without this a failure case falls through to `_with_vertex_fallback`, which is a live
    Gemini call: the suite took 104 seconds and billed a project before this fixture existed.
    """
    monkeypatch.setattr(draft_agent, "_CACHED_TOKEN", None)
    monkeypatch.setattr(draft_agent, "VERTEX_FALLBACK", False)
    monkeypatch.delenv("DRAFT_AGENT_TOKEN_FILES", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "plan-token")
    monkeypatch.setattr(draft_agent, "token_files", list)
    yield
    draft_agent._CACHED_TOKEN = None


def test_api_rate_limit_retries_a_fresh_review_in_a_new_session(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "RATE_LIMIT_RETRIES", 1)
    monkeypatch.setattr(draft_agent, "RATE_LIMIT_RETRY_SECONDS", 65)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setattr(draft_agent, "new_session_id", lambda: "retry-session")
    waits = []
    monkeypatch.setattr(
        draft_agent, "_wait_for_rate_limit_retry",
        lambda seconds, cancel=None: waits.append(seconds) or True)
    calls = []

    def run_once(**values):
        calls.append(values)
        if len(calls) == 1:
            return draft_agent.AgentRun(
                session_id=values["session_id"], model="opus", duration_ms=25,
                error="API Error: Request rejected (429), input tokens per minute rate limit")
        return draft_agent.AgentRun(
            ok=True, session_id=values["session_id"], model="opus",
            result={"summary": "reviewed"}, duration_ms=75)

    monkeypatch.setattr(draft_agent, "_run_once", run_once)

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="review", system_prompt="system", schema={},
        session_id="review-session", resume=False)

    assert result.ok is True
    assert waits == [65]
    assert [(call["token"], call["resume"], call["session_id"]) for call in calls] == [
        ("plan-token", False, "review-session"),
        ("plan-token", False, "retry-session"),
    ]
    assert result.duration_ms == 100
    assert any("rate limit" in step["text"].lower() for step in result.steps)


def test_api_overload_retries_a_fresh_review_in_a_new_session(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "RATE_LIMIT_RETRIES", 1)
    monkeypatch.setattr(draft_agent, "RATE_LIMIT_RETRY_SECONDS", 65)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setattr(draft_agent, "new_session_id", lambda: "retry-session")
    waits = []
    monkeypatch.setattr(
        draft_agent, "_wait_for_rate_limit_retry",
        lambda seconds, cancel=None: waits.append(seconds) or True)
    calls = []

    def run_once(**values):
        calls.append(values)
        if len(calls) == 1:
            return draft_agent.AgentRun(
                session_id=values["session_id"], model="opus", duration_ms=25,
                error="API Error: 529 Overloaded. This is a server-side issue.")
        return draft_agent.AgentRun(
            ok=True, session_id=values["session_id"], model="opus",
            result={"summary": "reviewed"}, duration_ms=75)

    monkeypatch.setattr(draft_agent, "_run_once", run_once)

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="review", system_prompt="system", schema={},
        session_id="review-session", resume=False)

    assert result.ok is True
    assert waits == [65]
    assert [(call["resume"], call["session_id"]) for call in calls] == [
        (False, "review-session"),
        (False, "retry-session"),
    ]
    assert any("provider" in step["text"].lower() for step in result.steps)


def test_connection_loss_mid_response_retries_from_the_saved_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "RATE_LIMIT_RETRIES", 1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setattr(draft_agent, "new_session_id", lambda: "retry-session")
    monkeypatch.setattr(
        draft_agent, "_wait_for_rate_limit_retry", lambda _seconds, cancel=None: True)
    calls = []

    def run_once(**values):
        calls.append(values)
        if len(calls) == 1:
            return draft_agent.AgentRun(
                session_id=values["session_id"], model="opus",
                error="API Error: Connection lost mid-response. The response may be incomplete.")
        return draft_agent.AgentRun(
            ok=True, session_id=values["session_id"], model="opus",
            result={"action": "revised"})

    monkeypatch.setattr(draft_agent, "_run_once", run_once)

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="repair", system_prompt="system", schema={},
        session_id="first-session", resume=False)

    assert result.ok is True
    assert [(call["resume"], call["session_id"]) for call in calls] == [
        (False, "first-session"),
        (False, "retry-session"),
    ]


def test_api_rate_limit_retry_keeps_a_resumed_drafting_session(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "RATE_LIMIT_RETRIES", 1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setattr(
        draft_agent, "_wait_for_rate_limit_retry", lambda _seconds, cancel=None: True)
    calls = []

    def run_once(**values):
        calls.append(values)
        if len(calls) == 1:
            return draft_agent.AgentRun(
                session_id=values["session_id"], model="opus",
                error="Request rejected with status 429")
        return draft_agent.AgentRun(
            ok=True, session_id=values["session_id"], model="opus",
            result={"action": "revised"})

    monkeypatch.setattr(draft_agent, "_run_once", run_once)

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="repair", system_prompt="system", schema={},
        session_id="draft-session", resume=True)

    assert result.ok is True
    assert [(call["resume"], call["session_id"]) for call in calls] == [
        (True, "draft-session"),
        (True, "draft-session"),
    ]


def test_resumed_run_restarts_from_workspace_when_conversation_is_missing(
        monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "_oauth_token", lambda: "subscription-token")
    monkeypatch.setattr(draft_agent, "new_session_id", lambda: "replacement-session")
    calls = []

    def run_once(**values):
        calls.append(values)
        if len(calls) == 1:
            return draft_agent.AgentRun(
                session_id=values["session_id"], model="opus",
                error="No conversation found with session ID: missing-session",
                duration_ms=10)
        return draft_agent.AgentRun(
            ok=True, session_id=values["session_id"], model="opus",
            result={"action": "revised"}, duration_ms=30)

    monkeypatch.setattr(draft_agent, "_run_once", run_once)

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="repair", system_prompt="system", schema={},
        session_id="missing-session", resume=True)

    assert result.ok is True
    assert [(call["resume"], call["session_id"]) for call in calls] == [
        (True, "missing-session"),
        (False, "replacement-session"),
    ]
    assert result.duration_ms == 40
    assert any("fresh session" in step["text"].lower() for step in result.steps)


def test_structured_cli_error_details_are_not_discarded():
    assert draft_agent._final_error({
        "subtype": "error_during_execution",
        "result": "",
        "errors": ["No conversation found with session ID: missing-session"],
    }) == "No conversation found with session ID: missing-session"


# =============================================================================================
# Subscriptions only, in order, and never the billed API
# =============================================================================================
def test_a_capped_subscription_moves_to_the_next_one_not_to_the_api(monkeypatch, tmp_path):
    """On 2026-08-26 at 20:33 UTC the subscription reported its weekly limit and every run after
    that continued through the billed API key, silently, carrying 600,000 tokens each."""
    monkeypatch.setattr(draft_agent, "subscription_tokens", lambda: ["first", "second"])
    seen = []

    def fake(common, *, token=""):
        seen.append(("subscription", token))
        if token == "first":
            return draft_agent.AgentRun(
                ok=False, error="You've hit your weekly limit \u00b7 resets Aug 31, 6am (UTC)")
        return draft_agent.AgentRun(ok=True, result={"action": "revised"})

    monkeypatch.setattr(draft_agent, "_run_with_rate_limit_retries", fake)
    out = draft_agent.run(workspace=tmp_path, prompt="p", system_prompt="s", schema={})

    assert out.ok
    assert [mode for mode, _ in seen] == ["subscription", "subscription"], (
        "a capped subscription reached for metered billing")
    assert [tok for _, tok in seen] == ["first", "second"]


def test_when_every_subscription_is_capped_the_run_stops_rather_than_spending(
        monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "subscription_tokens", lambda: ["a", "b"])
    monkeypatch.setattr(
        draft_agent, "_run_with_rate_limit_retries",
        lambda common, *, token="": draft_agent.AgentRun(
            ok=False, error="You've hit your weekly limit"))

    out = draft_agent.run(workspace=tmp_path, prompt="p", system_prompt="s", schema={})

    assert not out.ok
    assert any("not a fallback" in str(step.get("text") or "") for step in out.steps), (
        "the run did not say why it stopped instead of spending")


def test_a_host_with_no_subscription_refuses_rather_than_finding_a_key(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "subscription_tokens", lambda: [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-be-used")
    with pytest.raises(draft_agent.AgentUnavailable, match="deliberately not a fallback"):
        draft_agent.run(workspace=tmp_path, prompt="p", system_prompt="s", schema={})


def test_a_failure_that_is_not_a_quota_limit_does_not_burn_the_other_subscription(
        monkeypatch, tmp_path):
    """A malformed answer is not a reason to spend a second account's quota on the same work."""
    monkeypatch.setattr(draft_agent, "subscription_tokens", lambda: ["first", "second"])
    calls = []

    def fake(common, *, token=""):
        calls.append(token)
        return draft_agent.AgentRun(ok=False, error="did not return valid structured output")

    monkeypatch.setattr(draft_agent, "_run_with_rate_limit_retries", fake)
    draft_agent.run(workspace=tmp_path, prompt="p", system_prompt="s", schema={})
    assert calls == ["first"]


def test_the_token_list_is_read_in_priority_order(monkeypatch, tmp_path):
    first = tmp_path / "primary"
    second = tmp_path / "fallback"
    first.write_text("sk-ant-oat01-primary")
    second.write_text("sk-ant-oat01-fallback")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(draft_agent, "token_files",
                        lambda: [Path(item) for item in
                                 f"{first}:{second}".split(":")])
    assert draft_agent.subscription_tokens() == [
        "sk-ant-oat01-primary", "sk-ant-oat01-fallback"]


def test_a_missing_fallback_file_is_not_an_error(monkeypatch):
    """A host with only one subscription is the normal case, not a misconfiguration."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(draft_agent, "token_files",
                        lambda: [Path("/nonexistent/a"), Path("/nonexistent/b")])
    assert draft_agent.subscription_tokens() == []


def test_adding_a_fallback_credential_does_not_wait_for_a_restart(monkeypatch, tmp_path):
    """The 300-second cache is keyed on what was asked for, not on nothing."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    first = tmp_path / "primary"
    first.write_text("sk-ant-oat01-primary")
    monkeypatch.setattr(draft_agent, "token_files", lambda: [first])
    assert draft_agent.subscription_tokens() == ["sk-ant-oat01-primary"]
    second = tmp_path / "fallback"
    second.write_text("sk-ant-oat01-fallback")
    monkeypatch.setattr(draft_agent, "token_files", lambda: [first, second])
    assert draft_agent.subscription_tokens() == [
        "sk-ant-oat01-primary", "sk-ant-oat01-fallback"]


def test_a_dead_credential_moves_on_instead_of_ending_the_turn(monkeypatch, tmp_path):
    """A mirrored rotating credential is revoked the moment its own host refreshes it. Measured
    live on 2026-08-27: the fallback answered 401 and the turn died with a working rung left."""
    monkeypatch.setattr(draft_agent, "subscription_tokens", lambda: ["dead", "good"])
    seen = []

    def fake(common, *, token=""):
        seen.append(token)
        if token == "dead":
            return draft_agent.AgentRun(
                ok=False,
                error="Failed to authenticate. API Error: 401 OAuth access token has been revoked.")
        return draft_agent.AgentRun(ok=True, result={"action": "revised"})

    monkeypatch.setattr(draft_agent, "_run_with_rate_limit_retries", fake)
    out = draft_agent.run(workspace=tmp_path, prompt="p", system_prompt="s", schema={})

    assert out.ok and seen == ["dead", "good"]
    assert any("dead or revoked" in str(step.get("text") or "") for step in out.steps), \
        "it reported a revoked credential as an exhausted quota"


def test_a_dead_credential_is_not_reported_as_an_exhausted_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "subscription_tokens", lambda: ["dead"])
    monkeypatch.setattr(
        draft_agent, "_run_with_rate_limit_retries",
        lambda common, *, token="": draft_agent.AgentRun(
            ok=False, error="API Error: 401 OAuth access token has been revoked."))
    out = draft_agent.run(workspace=tmp_path, prompt="p", system_prompt="s", schema={})
    text = " ".join(str(step.get("text") or "") for step in out.steps)
    assert "could not be presented" in text and "out of quota" not in text


def test_when_every_subscription_is_dead_vertex_takes_over(monkeypatch, tmp_path):
    class AllowedSpend:
        degraded = False
        allowed = True

    monkeypatch.setattr(draft_agent, "VERTEX_FALLBACK", True)
    monkeypatch.setattr(draft_agent, "subscription_tokens", lambda: ["dead"])
    monkeypatch.setattr(
        draft_agent, "_run_with_rate_limit_retries",
        lambda common, *, token="": draft_agent.AgentRun(
            ok=False,
            error="Failed to authenticate. API Error: 401 OAuth access token has been revoked."))
    monkeypatch.setattr(draft_agent._SPEND, "status", lambda: AllowedSpend())
    monkeypatch.setattr(draft_agent._SPEND, "record", lambda **_values: None)
    monkeypatch.setattr(
        draft_agent, "_run_vertex_once",
        lambda **_values: draft_agent.AgentRun(
            ok=True, model="vertex/gemini-2.5-pro", result={"summary": "reviewed"}))

    out = draft_agent.run(workspace=tmp_path, prompt="p", system_prompt="s", schema={})

    assert out.ok is True
    assert out.model == "vertex/gemini-2.5-pro"
