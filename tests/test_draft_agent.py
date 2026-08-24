from pathlib import Path

import draft_agent


def test_api_auth_environment_does_not_inject_the_subscription_token(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-token")
    monkeypatch.setattr(draft_agent, "_oauth_token", lambda: "subscription-token")

    environment = draft_agent._environment(tmp_path, auth_mode="api")

    assert environment["ANTHROPIC_API_KEY"] == "api-key"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environment


def test_auto_auth_continues_a_resumed_run_through_api_after_subscription_limit(
        monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "auto")
    monkeypatch.setattr(draft_agent, "_SUBSCRIPTION_UNAVAILABLE", False)
    monkeypatch.setattr(draft_agent, "_oauth_token", lambda: "subscription-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    calls = []

    def run_once(**values):
        calls.append(values)
        if values["auth_mode"] == "subscription":
            return draft_agent.AgentRun(
                session_id="existing-session", model="opus",
                error="You've hit your weekly limit, resets soon", duration_ms=25)
        return draft_agent.AgentRun(
            ok=True, session_id=values["session_id"], model="opus",
            result={"action": "revised"}, duration_ms=75)

    monkeypatch.setattr(draft_agent, "_run_once", run_once)

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="repair", system_prompt="system", schema={},
        session_id="existing-session", resume=True)

    assert result.ok is True
    assert [(call["auth_mode"], call["resume"], call["session_id"]) for call in calls] == [
        ("subscription", True, "existing-session"),
        ("api", True, "existing-session"),
    ]
    assert result.duration_ms == 100
    assert draft_agent._SUBSCRIPTION_UNAVAILABLE is True


def test_auto_auth_skips_known_limited_subscription_on_later_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "auto")
    monkeypatch.setattr(draft_agent, "_SUBSCRIPTION_UNAVAILABLE", True)
    monkeypatch.setattr(draft_agent, "_oauth_token", lambda: "subscription-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    calls = []

    def run_once(**values):
        calls.append(values)
        return draft_agent.AgentRun(ok=True, session_id=values["session_id"], model="opus")

    monkeypatch.setattr(draft_agent, "_run_once", run_once)

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="repair", system_prompt="system", schema={},
        session_id="existing-session", resume=True)

    assert result.ok is True
    assert [call["auth_mode"] for call in calls] == ["api"]


def test_api_rate_limit_retries_a_fresh_review_in_a_new_session(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "api")
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
    assert [(call["auth_mode"], call["resume"], call["session_id"]) for call in calls] == [
        ("api", False, "review-session"),
        ("api", False, "retry-session"),
    ]
    assert result.duration_ms == 100
    assert any("rate limit" in step["text"].lower() for step in result.steps)


def test_api_overload_retries_a_fresh_review_in_a_new_session(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "api")
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


def test_api_rate_limit_retry_keeps_a_resumed_drafting_session(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "api")
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
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "subscription")
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
