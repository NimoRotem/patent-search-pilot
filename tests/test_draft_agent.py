from pathlib import Path

import pytest

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


def test_connection_loss_mid_response_retries_from_the_saved_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "api")
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


def test_provider_quota_detector_matches_the_live_plural_usage_limit_error():
    assert draft_agent._provider_quota_error(
        "API Error: 400 You have reached your specified API usage limits. "
        "You will regain access on 2026-09-01 at 00:00 UTC."
    ) is True


def test_vertex_agent_takes_over_after_revoked_subscription_and_api_quota(
        monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "auto")
    monkeypatch.setattr(draft_agent, "_SUBSCRIPTION_UNAVAILABLE", False)
    monkeypatch.setattr(draft_agent, "_oauth_token", lambda: "revoked-subscription-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    calls = []

    def run_once(**values):
        calls.append(values["auth_mode"])
        if values["auth_mode"] == "subscription":
            return draft_agent.AgentRun(
                model="opus",
                error=("Failed to authenticate. API Error: 401 OAuth access token has been "
                       "revoked."))
        return draft_agent.AgentRun(
            model="opus", error="You have reached your specified API usage limits.")

    monkeypatch.setattr(draft_agent, "_run_once", run_once)
    monkeypatch.setattr(
        draft_agent, "_run_vertex_once",
        lambda **_values: draft_agent.AgentRun(
            ok=True, model="vertex/gemini-2.5-pro", result={"summary": "reviewed"}))

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="review", system_prompt="system", schema={})

    assert result.ok is True
    assert result.model == "vertex/gemini-2.5-pro"
    assert calls == ["subscription", "api"]
    assert draft_agent._SUBSCRIPTION_UNAVAILABLE is True


def test_vertex_agent_takes_over_after_subscription_and_api_quota_exhaustion(
        monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "auto")
    monkeypatch.setattr(draft_agent, "_SUBSCRIPTION_UNAVAILABLE", False)
    monkeypatch.setattr(draft_agent, "_oauth_token", lambda: "subscription-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    calls = []

    def run_once(**values):
        calls.append(values["auth_mode"])
        return draft_agent.AgentRun(
            session_id="claude-session", model="opus", duration_ms=20,
            error=("API Error: 400 You have reached your specified API usage limits. "
                   "You will regain access on 2026-09-01 at 00:00 UTC."))

    def run_vertex(**values):
        assert values["workspace"] == Path(tmp_path)
        return draft_agent.AgentRun(
            ok=True, session_id="vertex-session", model="vertex/gemini-2.5-pro",
            result={"action": "revised"}, duration_ms=60,
            tokens={"input": 100, "output": 20, "cache_read": 0, "cache_write": 0})

    monkeypatch.setattr(draft_agent, "_run_once", run_once)
    monkeypatch.setattr(draft_agent, "_run_vertex_once", run_vertex)

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="repair", system_prompt="system", schema={},
        session_id="claude-session", resume=True)

    assert result.ok is True
    assert calls == ["subscription", "api"]
    assert result.model == "vertex/gemini-2.5-pro"
    assert result.duration_ms == 100
    assert result.tokens["input"] == 100
    assert any("vertex" in step["text"].lower() for step in result.steps)


def test_vertex_agent_takes_over_when_explicit_api_route_exhausts_quota(
        monkeypatch, tmp_path):
    monkeypatch.setattr(draft_agent, "AUTH_MODE", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setattr(
        draft_agent, "_run_once",
        lambda **_values: draft_agent.AgentRun(
            model="opus", duration_ms=15,
            error="You have reached your specified API usage limits."))
    monkeypatch.setattr(
        draft_agent, "_run_vertex_once",
        lambda **_values: draft_agent.AgentRun(
            ok=True, model="vertex/gemini-2.5-pro", result={"summary": "reviewed"},
            duration_ms=35))

    result = draft_agent.run(
        workspace=Path(tmp_path), prompt="review", system_prompt="system", schema={})

    assert result.ok is True
    assert result.duration_ms == 50


def test_vertex_workspace_tools_are_confined_and_only_edit_filing_files(tmp_path):
    (tmp_path / "draft").mkdir()
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "disclosure.md").write_text("authority", encoding="utf-8")

    written, attachments = draft_agent._vertex_tool(
        tmp_path, "write_file", {"path": "draft/01-title.md", "content": "A title"},
        writable=True)

    assert written["ok"] is True
    assert attachments == []
    assert (tmp_path / "draft" / "01-title.md").read_text(encoding="utf-8") == "A title"
    with pytest.raises(ValueError, match="Only draft/ and figures/"):
        draft_agent._vertex_tool(
            tmp_path, "write_file",
            {"path": "input/disclosure.md", "content": "changed"}, writable=True)
    with pytest.raises(ValueError, match="leaves the drafting workspace"):
        draft_agent._vertex_tool(
            tmp_path, "write_file", {"path": "draft/../../outside", "content": "x"},
            writable=True)
    with pytest.raises(ValueError, match="canonical application files"):
        draft_agent._vertex_tool(
            tmp_path, "write_file", {"path": "draft/08-claims.md", "content": "wrong"},
            writable=True)
    assert (tmp_path / "input" / "disclosure.md").read_text(encoding="utf-8") == "authority"


def test_vertex_image_read_attaches_pixels_without_exposing_raw_bytes_in_json(tmp_path):
    (tmp_path / "figures").mkdir()
    image = b"\x89PNG\r\n\x1a\nnot-a-real-image"
    (tmp_path / "figures" / "rendered-FIG-1.png").write_bytes(image)

    result, attachments = draft_agent._vertex_tool(
        tmp_path, "read_file", {"path": "figures/rendered-FIG-1.png"}, writable=False)

    assert result["pixels_attached"] is True
    assert result["bytes"] == len(image)
    assert attachments == [(image, "image/png")]
    assert image not in str(result).encode()


def test_vertex_structured_result_validator_rejects_missing_and_extra_fields():
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }

    assert draft_agent._schema_problem({"summary": "done"}, schema) == ""
    assert "required" in draft_agent._schema_problem({}, schema)
    assert "unexpected" in draft_agent._schema_problem(
        {"summary": "done", "notes": "no"}, schema)


def test_vertex_agent_does_not_replay_an_invalid_or_empty_model_role(monkeypatch, tmp_path):
    """A blank Vertex candidate once poisoned the next request with an invalid history role."""
    from google.genai import types

    calls = []

    def generate(_client, *, model, contents, config, deadline, cancel):
        del model, config, deadline, cancel
        calls.append(list(contents))
        if len(calls) == 1:
            return type("Response", (), {
                "candidates": [type("Candidate", (), {
                    "content": types.Content(role="assistant", parts=[]),
                })()],
                "usage_metadata": None,
            })()
        roles = [str(getattr(item, "role", "") or "") for item in contents]
        if any(role not in {"user", "model"} for role in roles):
            raise RuntimeError("Please use a valid role: user, model.")
        return type("Response", (), {
            "candidates": [type("Candidate", (), {
                "content": types.Content(
                    role="model", parts=[types.Part.from_text(text='{"action":"ready"}')]),
            })()],
            "usage_metadata": None,
        })()

    monkeypatch.setattr(draft_agent, "_vertex_client", lambda: object())
    monkeypatch.setattr(draft_agent, "_vertex_generate", generate)
    result = draft_agent._run_vertex_once(
        workspace=tmp_path, prompt="finish", system_prompt="system",
        schema={
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
            "additionalProperties": False,
        }, timeout=30)

    assert result.ok is True and result.result == {"action": "ready"}
    assert len(calls) == 2
    assert [item.role for item in calls[1]] == ["user", "user"]


def test_vertex_agent_forces_submit_result_after_repeated_prose_only_finishes(
        monkeypatch, tmp_path):
    from google.genai import types

    calls = []

    def generate(_client, *, model, contents, config, deadline, cancel):
        del model, contents, deadline, cancel
        calls.append(config)
        if len(calls) <= 3:
            parts = [types.Part.from_text(text="The requested edits are complete.")]
        else:
            function_config = config.tool_config.function_calling_config
            assert function_config.mode == types.FunctionCallingConfigMode.ANY
            assert function_config.allowed_function_names == ["submit_result"]
            parts = [types.Part.from_function_call(
                name="submit_result", args={"action": "ready"})]
        return type("Response", (), {
            "candidates": [type("Candidate", (), {
                "content": types.Content(role="model", parts=parts),
            })()],
            "usage_metadata": None,
        })()

    monkeypatch.setattr(draft_agent, "_vertex_client", lambda: object())
    monkeypatch.setattr(draft_agent, "_vertex_generate", generate)
    result = draft_agent._run_vertex_once(
        workspace=tmp_path, prompt="finish", system_prompt="system",
        schema={
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
            "additionalProperties": False,
        }, timeout=30)

    assert result.ok is True and result.result == {"action": "ready"}
    assert len(calls) == 4
