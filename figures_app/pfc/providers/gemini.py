"""Vertex AI adapter for both model roles.

Vertex is used rather than a keyed API because every builder VM already authenticates to it as
its own service account, so there is no credential to rotate into this app and no shared key to
leak. The text role and the vision role are separate objects with separate models, which is
what lets the verifier be a genuinely different reader from the extractor.

Structured output is requested with ``response_mime_type=application/json`` and, where the
schema survives translation, a ``response_schema``. The reply is still validated against the
Pydantic model afterwards. A provider-side schema reduces malformed replies; it is not a
substitute for checking.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional, TypeVar

from pydantic import BaseModel

from .base import CallLog, ModelUnavailable, input_hash, with_retries

T = TypeVar("T", bound=BaseModel)

DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "nimo-gpt")
DEFAULT_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
DEFAULT_TEXT_MODEL = os.environ.get("PFC_TEXT_MODEL", "gemini-2.5-flash")
DEFAULT_VISION_MODEL = os.environ.get("PFC_VISION_MODEL", "gemini-2.5-pro")
DEFAULT_VERIFIER_B_MODEL = os.environ.get("PFC_VERIFIER_B_MODEL", "gemini-2.5-flash")

_local = threading.local()


def _client():
    if not hasattr(_local, "client"):
        try:
            from google import genai
        except Exception as exc:  # pragma: no cover - deployment dependent
            raise ModelUnavailable(f"google-genai is not installed: {exc}") from exc
        _local.client = genai.Client(vertexai=True, project=DEFAULT_PROJECT,
                                     location=DEFAULT_LOCATION)
    return _local.client


def _usage(response) -> dict[str, int]:
    meta = getattr(response, "usage_metadata", None)
    return {
        "prompt_tokens": int(getattr(meta, "prompt_token_count", 0) or 0) if meta else 0,
        "completion_tokens": int(getattr(meta, "candidates_token_count", 0) or 0) if meta else 0,
    }


def _response_schema(schema: type[BaseModel]) -> Optional[dict[str, Any]]:
    """A JSON schema Vertex will accept, or None to fall back to free JSON.

    Vertex rejects ``$ref``/``$defs`` and a few other JSON-Schema constructs. Rather than
    maintaining a translator that silently mangles a nested model, anything containing a
    reference is simply not sent; the prompt still states the shape and the reply is still
    validated locally.
    """
    try:
        raw = schema.model_json_schema()
    except Exception:
        return None
    if "$defs" in json.dumps(raw) or "$ref" in json.dumps(raw):
        return None
    raw.pop("title", None)
    return raw


class GeminiTextReasoner:
    name = "vertex"

    def __init__(self, model: str = DEFAULT_TEXT_MODEL, log: Optional[CallLog] = None,
                 temperature: float = 0.0):
        self.model = model
        self.log = log
        self.temperature = temperature

    def generate_structured(self, task: str, schema: type[T], system: str, context: str,
                            *, prompt_version: str = "", max_tokens: int = 8000) -> T:
        from google.genai.types import GenerateContentConfig, ThinkingConfig

        key = input_hash(task, self.model, prompt_version, system, context)
        response_schema = _response_schema(schema)

        def call(feedback: str):
            prompt = context if not feedback else f"{context}\n\n{feedback}"
            config = GenerateContentConfig(
                system_instruction=system, temperature=self.temperature,
                max_output_tokens=max_tokens, response_mime_type="application/json",
                thinking_config=ThinkingConfig(thinking_budget=0))
            if response_schema is not None:
                config.response_schema = response_schema
            response = _client().models.generate_content(
                model=self.model, contents=prompt, config=config)
            return (response.text or ""), _usage(response)

        return with_retries(call, schema, task=task, provider=self.name, model=self.model,
                            prompt_version=prompt_version, log=self.log, input_key=key)


class GeminiVisionVerifier:
    name = "vertex"

    def __init__(self, model: str = DEFAULT_VISION_MODEL, log: Optional[CallLog] = None):
        self.model = model
        self.log = log

    def inspect(self, image_png: bytes, system: str, instruction: str, schema: type[T],
                *, prompt_version: str = "", max_tokens: int = 4000) -> T:
        from google.genai.types import GenerateContentConfig, Part, ThinkingConfig

        key = input_hash("vision", self.model, prompt_version, instruction,
                         len(image_png or b""))
        response_schema = _response_schema(schema)

        def call(feedback: str):
            text = instruction if not feedback else f"{instruction}\n\n{feedback}"
            config = GenerateContentConfig(
                system_instruction=system, temperature=0.0, max_output_tokens=max_tokens,
                response_mime_type="application/json",
                thinking_config=ThinkingConfig(thinking_budget=0))
            if response_schema is not None:
                config.response_schema = response_schema
            response = _client().models.generate_content(
                model=self.model,
                contents=[Part.from_bytes(data=image_png, mime_type="image/png"), text],
                config=config)
            return (response.text or ""), _usage(response)

        return with_retries(call, schema, task="vision_verify", provider=self.name,
                            model=self.model, prompt_version=prompt_version, log=self.log,
                            input_key=key)


def read_figure_labels(images: list[bytes], model: str = DEFAULT_VISION_MODEL) -> list[list[str]]:
    """Read the ``FIG. n`` captions printed on the ORIGINAL patent sheets.

    This is the one place a model looks at the applicant's own drawings, and it does one thing:
    read the caption that is printed on the sheet, so a generated FIG. 3 can be shown beside the
    filed FIG. 3 rather than beside whichever sheet happens to be third in the file. Nothing
    read here reaches the semantic graph or any generated figure.
    """
    from google.genai.types import GenerateContentConfig, Part, ThinkingConfig

    out: list[list[str]] = []
    for blob in images:
        if not blob:
            out.append([])
            continue
        try:
            response = _client().models.generate_content(
                model=model,
                contents=[Part.from_bytes(data=blob, mime_type="image/png"),
                          "List every figure caption printed on this patent drawing sheet, "
                          "exactly as printed (for example \"FIG. 1\", \"FIG. 2A\"). Return "
                          "ONLY JSON: {\"labels\":[\"FIG. 1\"]}. Return an empty list if the "
                          "sheet carries no figure caption."],
                config=GenerateContentConfig(
                    temperature=0.0, max_output_tokens=400,
                    response_mime_type="application/json",
                    thinking_config=ThinkingConfig(thinking_budget=0)))
            payload = json.loads(response.text or "{}")
            labels = [str(x)[:16] for x in (payload.get("labels") or [])][:12]
        except Exception:
            labels = []
        out.append(labels)
    return out
