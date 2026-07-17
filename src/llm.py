"""Thin LLM helper for the agent's language tasks (spec §7): query generation, terminology/
synonyms, CPC suggestions, cross-lingual translation. Deterministic code (dates/dedup/budget/
scoring/stopping) lives elsewhere — NOT here.

Provider: Vertex AI `gemini-2.5-flash` via the GCE service account (OpenAI account is quota-blocked)."""
from __future__ import annotations
import json, threading
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

AGENT_MODEL = "gemini-2.5-flash"
_local = threading.local()
def _client():
    if not hasattr(_local, "c"):
        from google import genai
        _local.c = genai.Client(vertexai=True, project="nimo-gpt", location="us-central1")
    return _local.c

_usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
def usage():
    return dict(_usage)


@retry(wait=wait_exponential(min=1, max=20), stop=stop_after_attempt(5),
       retry=retry_if_exception_type(Exception))
def _call(system, user, max_tokens):
    from google.genai.types import GenerateContentConfig, ThinkingConfig
    # gemini-2.5-flash is a thinking model; thinking tokens eat the output budget and truncate
    # the JSON -> disable thinking for these short structured tasks.
    resp = _client().models.generate_content(
        model=AGENT_MODEL, contents=user,
        config=GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                     temperature=0.2, max_output_tokens=max_tokens,
                                     thinking_config=ThinkingConfig(thinking_budget=0)))
    return resp


def chat_json(system, user, max_tokens=1200):
    try:
        resp = _call(system, user, max_tokens)
    except Exception:
        return {}
    _usage["calls"] += 1
    um = getattr(resp, "usage_metadata", None)
    if um:
        _usage["prompt_tokens"] += getattr(um, "prompt_token_count", 0) or 0
        _usage["completion_tokens"] += getattr(um, "candidates_token_count", 0) or 0
    try:
        return json.loads(resp.text)
    except Exception:
        return {}
