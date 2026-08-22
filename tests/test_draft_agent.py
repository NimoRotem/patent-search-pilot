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
