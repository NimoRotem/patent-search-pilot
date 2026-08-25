"""Structured model calls, over Vertex AI.

Vertex rather than a keyed API because every builder VM already authenticates to it as its own
service account: nothing to rotate into this app, nothing to leak out of it. Two roles, because
they want different models. ``fast`` reads a draft and pulls out numerals; ``deep`` decides what
the figures are and what is in them, and is worth letting think.

A retry here only ever fixes a malformed reply. It never asks the same question again hoping for
a different meaning: a pipeline that reruns a prompt because it disliked the answer is one that
argues itself into a conclusion. When the meaning is wrong, the validator says so and the failure
is reported against the stage that caused it.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

PROJECT = os.environ.get("GCP_PROJECT", "nimo-gpt")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
FAST_MODEL = os.environ.get("FM_FAST_MODEL", "gemini-2.5-flash")
DEEP_MODEL = os.environ.get("FM_DEEP_MODEL", "gemini-2.5-pro")
MAX_ATTEMPTS = int(os.environ.get("FM_MAX_ATTEMPTS", "3"))
SCENE_MODEL = os.environ.get("FM_SCENE_MODEL", DEEP_MODEL)
# Thinking budgets in tokens. 0 means unbounded, which is what these were and why a figure set
# took ten minutes.
PLAN_THINKING = int(os.environ.get("FM_PLAN_THINKING", "6144"))
SCENE_THINKING = int(os.environ.get("FM_SCENE_THINKING", "3072"))
REVISE_THINKING = int(os.environ.get("FM_REVISE_THINKING", "2048"))

_local = threading.local()

# Some Gemini releases answer ``thinking_budget=0`` with a 400. Which ones changes with each
# release, so it is learned at runtime: the first rejection records the model and every later
# call simply lets it think.
_MUST_THINK: set[str] = set()
_MUST_THINK_LOCK = threading.Lock()


class ModelUnavailable(RuntimeError):
    """No model could answer. The caller degrades or reports; it does not invent."""


class StructuredOutputError(ValueError):
    """The model answered, but never in the shape that was asked for."""


# ---------------------------------------------------------------------------------- telemetry


@dataclass
class CallRecord:
    task: str
    model: str
    input_sha256: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    ok: bool = True
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["error"] = self.error[:300]
        return data


@dataclass
class CallLog:
    """One job's model telemetry. Hashes and counts, never draft text."""
    path: Optional[Path] = None
    records: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: CallRecord) -> None:
        with self._lock:
            self.records.append(record)
            if self.path is None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record.as_dict(), sort_keys=True) + "\n")
            except OSError:
                pass

    @property
    def totals(self) -> dict[str, int]:
        return {"calls": len(self.records),
                "prompt_tokens": sum(r.prompt_tokens for r in self.records),
                "completion_tokens": sum(r.completion_tokens for r in self.records),
                "failures": sum(1 for r in self.records if not r.ok)}


def input_hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------- parsing


def extract_json(text: str) -> Any:
    raw = (text or "").strip()
    if not raw:
        raise StructuredOutputError("the reply was empty")
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except ValueError:
            pass
    raise StructuredOutputError("the reply contained no parsable JSON object")


def coerce(schema: type[T], payload: Any) -> T:
    if isinstance(payload, str):
        payload = extract_json(payload)
    if not isinstance(payload, dict):
        # Naming the keys that were wanted is the difference between a retry that fixes it and
        # three identical attempts. A model that returned a bare array knows what to do with
        # "wrap it under these keys"; it does not know what to do with "that was wrong".
        keys = ", ".join(f'"{k}"' for k in (schema.model_json_schema().get("properties") or {}))
        raise StructuredOutputError(
            f"the reply was a JSON {type(payload).__name__}, not an object. Wrap it in an "
            f"object with these top-level keys: {keys}")
    return schema.model_validate(payload)


# -------------------------------------------------------------------------------------- vertex


def _client():
    if not hasattr(_local, "client"):
        try:
            from google import genai
        except Exception as exc:  # pragma: no cover - deployment dependent
            raise ModelUnavailable(f"google-genai is not installed: {exc}") from exc
        _local.client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _local.client


def _thinking(model: str, allow: bool, budget: int = 0):
    """The thinking configuration for one call.

    Left unbounded, a thinking model will spend as long as it likes, and on a scene call that was
    ninety seconds where twenty would have done. A budget is not a quality setting so much as a
    latency one: the work these prompts ask for is filling in a schema from a passage of text,
    and it converges long before the model stops looking for a better answer.
    """
    from google.genai.types import ThinkingConfig

    if allow:
        return ThinkingConfig(thinking_budget=budget) if budget > 0 else None
    with _MUST_THINK_LOCK:
        if model in _MUST_THINK:
            return None
    return ThinkingConfig(thinking_budget=0)


def _note_thinking_rejected(model: str, error: Exception) -> None:
    if "thinking_budget" in str(error):
        with _MUST_THINK_LOCK:
            _MUST_THINK.add(model)


# The subset of JSON Schema that Vertex accepts. Anything else has to go, and a schema that
# cannot be expressed without it is not sent at all.
_VERTEX_KEYS = {"type", "format", "description", "nullable", "enum", "items", "properties",
                "required", "minItems", "maxItems", "anyOf", "minimum", "maximum"}
_DROP_KEYS = {"$defs", "title", "default", "additionalProperties", "const", "discriminator",
              "examples", "exclusiveMinimum", "exclusiveMaximum", "$schema", "allOf"}


class _Unrepresentable(Exception):
    """The schema needs something Vertex has no way to say."""


def _inline(node: Any, defs: dict[str, Any], depth: int, seen: tuple[str, ...]) -> Any:
    """Resolve ``$ref`` into the schema itself, dropping what Vertex will not take."""
    if depth > 14:
        raise _Unrepresentable("nested too deeply")
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        if name in seen:
            # A model that contains itself, such as a UI tree. There is no way to write that in
            # Vertex's schema dialect, so the whole schema is dropped and the prompt carries the
            # shape instead.
            raise _Unrepresentable(f"{name} is recursive")
        return _inline(defs.get(name, {}), defs, depth + 1, seen + (name,))

    if "anyOf" in node:
        # Pydantic writes Optional[X] as anyOf[X, null]; Vertex says the same thing with
        # "nullable" on X itself.
        variants = [v for v in node["anyOf"] if not (isinstance(v, dict)
                                                     and v.get("type") == "null")]
        nullable = len(variants) != len(node["anyOf"])
        if len(variants) == 1:
            merged = _inline(variants[0], defs, depth + 1, seen)
            if isinstance(merged, dict):
                if nullable:
                    merged["nullable"] = True
                if node.get("description"):
                    merged.setdefault("description", node["description"])
            return merged
        raise _Unrepresentable("a union of more than one type")

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS or key not in _VERTEX_KEYS:
            continue
        if key == "properties":
            out[key] = {name: _inline(sub, defs, depth + 1, seen)
                        for name, sub in (value or {}).items()}
        elif key == "items":
            out[key] = _inline(value, defs, depth + 1, seen)
        else:
            out[key] = value
    if out.get("type") == "object" and not out.get("properties"):
        raise _Unrepresentable("an object with no stated properties")
    return out


def _response_schema(schema: type[BaseModel]) -> Optional[dict[str, Any]]:
    """A schema Vertex will take, or None to fall back to the shape in the prompt.

    Sending one matters more than it looks. Without it the model chooses its own envelope, and
    the failure it chooses is not a malformed object but a perfectly well-formed different one:
    a bare array of figures rather than {"figures": [...]}. That parses, and then fails
    validation, and the retry asks the same question again.
    """
    try:
        raw = schema.model_json_schema()
        defs = raw.get("$defs") or {}
        return _inline(raw, defs, 0, ())
    except (_Unrepresentable, Exception):
        return None


# ------------------------------------------------------------------------------ shape hints


def shape_hint(schema: type[BaseModel], depth: int = 0, max_depth: int = 5) -> str:
    """The required JSON shape, written out compactly, generated from the schema itself.

    Generated rather than hand-written so it cannot drift from the model it describes. It goes
    into every system prompt, because a schema Vertex refused to take is exactly the case where
    the model most needs to be told what the envelope is.
    """
    try:
        raw = schema.model_json_schema()
    except Exception:
        return ""
    return _render(raw, raw.get("$defs") or {}, depth, max_depth, ())


def _render(node: Any, defs: dict, depth: int, max_depth: int, seen: tuple[str, ...]) -> str:
    if not isinstance(node, dict):
        return "value"
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        if name in seen or depth >= max_depth:
            return f"<{name}, same shape as above>"
        return _render(defs.get(name, {}), defs, depth, max_depth, seen + (name,))
    if "anyOf" in node:
        variants = [v for v in node["anyOf"]
                    if not (isinstance(v, dict) and v.get("type") == "null")]
        if variants:
            return _render(variants[0], defs, depth, max_depth, seen) + " or null"
        return "null"
    if node.get("enum"):
        return " | ".join(json.dumps(v) for v in node["enum"][:12])
    kind = node.get("type")
    if kind == "array":
        return "[ " + _render(node.get("items") or {}, defs, depth + 1, max_depth, seen) + ", ... ]"
    if kind == "object" or node.get("properties"):
        properties = node.get("properties") or {}
        if not properties:
            extra = node.get("additionalProperties")
            if isinstance(extra, dict):
                return ('{ "name": ' + _render(extra, defs, depth + 1, max_depth, seen)
                        + ", ... }")
            return "{ ... }"
        pad = "  " * (depth + 1)
        required = set(node.get("required") or [])
        lines = []
        for name, sub in list(properties.items())[:24]:
            mark = "" if name in required else "   (optional)"
            lines.append(f'{pad}"{name}": '
                         + _render(sub, defs, depth + 1, max_depth, seen) + f",{mark}")
        return "{\n" + "\n".join(lines) + "\n" + "  " * depth + "}"
    return {"string": "string", "integer": "integer", "number": "number",
            "boolean": "true | false"}.get(str(kind), "value")


def _usage(response) -> dict[str, int]:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return {"prompt_tokens": 0, "completion_tokens": 0}
    return {"prompt_tokens": int(getattr(meta, "prompt_token_count", 0) or 0),
            "completion_tokens": int(getattr(meta, "candidates_token_count", 0) or 0)}


def _check_complete(response) -> None:
    """Say when a reply was cut off, so the retry can ask for a shorter one.

    A truncated JSON object fails to parse, and a generic "return valid JSON" retry then asks for
    the same too-long answer and fails identically. Naming the real problem gets a shorter list,
    which is a request a model can actually satisfy.
    """
    candidates = getattr(response, "candidates", None) or []
    reason = str(getattr(candidates[0], "finish_reason", "") if candidates else "")
    if "MAX_TOKENS" in reason.upper():
        raise StructuredOutputError(
            "the reply hit the output limit and its JSON is incomplete. Return fewer items, "
            "keeping the best-supported ones")


class Reasoner:
    """One model role."""

    def __init__(self, model: str, log: Optional[CallLog] = None, *, think: bool = False,
                 temperature: float = 0.0, thinking_budget: int = 0):
        self.model = model
        self.log = log
        self.think = think
        self.temperature = temperature
        self.thinking_budget = thinking_budget

    def structured(self, task: str, schema: type[T], system: str, context: str, *,
                   max_tokens: int = 16000) -> T:
        from google.genai.types import GenerateContentConfig

        key = input_hash(task, self.model, system, context)
        response_schema = _response_schema(schema)
        instruction = system + (
            "\n\nReturn ONE JSON object and nothing else, with exactly these top-level keys, in "
            "exactly this shape:\n" + shape_hint(schema))

        def call(feedback: str):
            prompt = context if not feedback else f"{context}\n\n{feedback}"
            config = GenerateContentConfig(
                system_instruction=instruction, temperature=self.temperature,
                max_output_tokens=max_tokens, response_mime_type="application/json",
                thinking_config=_thinking(self.model, self.think, self.thinking_budget))
            if response_schema is not None:
                config.response_schema = response_schema
            try:
                response = _client().models.generate_content(
                    model=self.model, contents=prompt, config=config)
            except Exception as exc:
                _note_thinking_rejected(self.model, exc)
                raise
            _check_complete(response)
            return (response.text or ""), _usage(response)

        return _with_retries(call, schema, task=task, model=self.model, log=self.log,
                             input_key=key)


def _with_retries(call: Callable[[str], tuple[str, dict]], schema: type[T], *, task: str,
                  model: str, log: Optional[CallLog], input_key: str,
                  attempts: int = MAX_ATTEMPTS) -> T:
    started = time.monotonic()
    feedback = ""
    last_error = ""
    for attempt in range(attempts):
        try:
            raw, usage = call(feedback)
            value = coerce(schema, raw)
        except ValidationError as exc:
            last_error = str(exc)[:900]
            feedback = ("Your previous reply did not match the required schema. Fix exactly "
                        f"these problems and return the corrected JSON only:\n{last_error}")
            continue
        except StructuredOutputError as exc:
            last_error = str(exc)[:900]
            feedback = (f"Your previous reply could not be used: {last_error}. Return one JSON "
                        "object and nothing else.")
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:900]
            feedback = ""
            time.sleep(1.0 + attempt)
            continue
        if log is not None:
            log.add(CallRecord(task=task, model=model, input_sha256=input_key,
                               latency_ms=int((time.monotonic() - started) * 1000),
                               prompt_tokens=usage.get("prompt_tokens", 0),
                               completion_tokens=usage.get("completion_tokens", 0),
                               retries=attempt, ok=True))
        return value
    if log is not None:
        log.add(CallRecord(task=task, model=model, input_sha256=input_key,
                           latency_ms=int((time.monotonic() - started) * 1000),
                           retries=attempts, ok=False, error=last_error))
    raise StructuredOutputError(f"{task}: no schema-valid reply after {attempts} attempts "
                                f"({last_error})")


def fast(log: Optional[CallLog] = None) -> Reasoner:
    return Reasoner(FAST_MODEL, log, think=False)


def deep(log: Optional[CallLog] = None, *, budget: int = PLAN_THINKING) -> Reasoner:
    """The role that decides what a figure contains. It thinks, but not without limit."""
    return Reasoner(DEEP_MODEL, log, think=True, thinking_budget=budget)


def scene(log: Optional[CallLog] = None) -> Reasoner:
    """The role that fills in one figure's scene. The same model, on a shorter leash.

    A scene call is a schema-filling task over a passage that is already selected for it, and it
    converges early. Unbounded, the same call took ninety seconds where twenty was enough, and
    with one call per figure that is the whole difference between a job you wait for and a job
    you come back to.
    """
    return Reasoner(SCENE_MODEL, log, think=True, thinking_budget=SCENE_THINKING)


def available() -> tuple[bool, str]:
    """Whether a model can be reached at all, for the health endpoint."""
    try:
        from google import genai  # noqa: F401
    except Exception as exc:
        return False, f"google-genai missing: {exc}"
    try:
        _client()
    except Exception as exc:
        return False, str(exc)[:200]
    return True, f"vertex {PROJECT}/{LOCATION} fast={FAST_MODEL} deep={DEEP_MODEL}"
