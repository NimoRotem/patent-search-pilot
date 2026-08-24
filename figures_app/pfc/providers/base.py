"""Provider-neutral model interfaces.

Two capabilities, defined once, so no business-logic module ever names a vendor:

``TextReasoner``    text plus a Pydantic schema in, a validated instance of that schema out.
``VisionVerifier``  an image plus the references it should contain in, an ``ObservedFigure`` out.

A structured call retries only for the reasons a retry can fix — malformed output, a schema
mismatch, a transport failure — and never to shop for a different semantic answer. Repeating a
prompt because you disliked the meaning of the reply is how a system talks itself into a
conclusion, and this pipeline resolves semantic ambiguity by blocking a figure instead.

Every call is recorded to the job's own directory: task, provider, model, prompt version, input
hash, latency, tokens, retries. The record holds hashes and counts, not patent text, so a job's
telemetry can be read without exposing a confidential draft.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

MAX_ATTEMPTS = 3


class ModelUnavailable(RuntimeError):
    """No provider could answer. The caller must degrade, not pretend."""


class StructuredOutputError(ValueError):
    """The provider answered but never produced output matching the schema."""


@dataclass
class CallRecord:
    task: str
    provider: str
    model: str
    prompt_version: str
    input_sha256: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    ok: bool = True
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task, "provider": self.provider, "model": self.model,
            "prompt_version": self.prompt_version, "input_sha256": self.input_sha256,
            "latency_ms": self.latency_ms, "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens, "retries": self.retries,
            "ok": self.ok, "error": self.error[:300],
        }


@dataclass
class CallLog:
    """Model-call telemetry for one job, written where the job's artifacts live."""

    path: Optional[Path] = None
    records: list[CallRecord] = field(default_factory=list)

    def add(self, record: CallRecord) -> None:
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
        return {
            "calls": len(self.records),
            "prompt_tokens": sum(r.prompt_tokens for r in self.records),
            "completion_tokens": sum(r.completion_tokens for r in self.records),
            "failures": sum(1 for r in self.records if not r.ok),
        }


class TextReasoner(Protocol):
    name: str
    model: str

    def generate_structured(self, task: str, schema: type[T], system: str,
                            context: str, *, prompt_version: str = "",
                            max_tokens: int = 8000) -> T:
        ...


class VisionVerifier(Protocol):
    name: str
    model: str

    def inspect(self, image_png: bytes, system: str, instruction: str,
                schema: type[T], *, prompt_version: str = "",
                max_tokens: int = 4000) -> T:
        ...


def input_hash(*parts: Any) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def coerce(schema: type[T], payload: Any) -> T:
    """Validate a provider's JSON against the schema, or raise."""
    if isinstance(payload, str):
        payload = extract_json(payload)
    if not isinstance(payload, dict):
        raise StructuredOutputError("provider did not return a JSON object")
    return schema.model_validate(payload)


def extract_json(text: str) -> Any:
    """The first JSON object in a reply.

    Kept deliberately small. A provider that has been told to return JSON and returns prose is
    a provider that misunderstood the task, and the answer is a retry with the validation error,
    not an ever-more-elaborate salvage routine that eventually parses something wrong.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except ValueError:
            pass
    raise StructuredOutputError("provider reply contained no parsable JSON object")


def with_retries(call, schema: type[T], *, task: str, provider: str, model: str,
                 prompt_version: str, log: Optional[CallLog], input_key: str,
                 attempts: int = MAX_ATTEMPTS) -> T:
    """Run ``call(feedback)`` until it yields a schema-valid object.

    ``feedback`` is the previous validation error, handed back verbatim so the provider is
    correcting a stated defect rather than guessing again from scratch.
    """
    started = time.monotonic()
    feedback = ""
    last_error = ""
    for attempt in range(attempts):
        try:
            raw, usage = call(feedback)
            value = coerce(schema, raw)
        except ValidationError as exc:
            last_error = str(exc)[:800]
            feedback = ("Your previous reply did not match the required schema. Fix exactly "
                        f"these problems and return the corrected JSON only:\n{last_error}")
            continue
        except StructuredOutputError as exc:
            last_error = str(exc)[:800]
            # The message matters here: "you ran out of room" and "you returned prose" need
            # different corrections, and a generic scolding gets the same failure again.
            feedback = (f"Your previous reply could not be used: {last_error}. Return one JSON "
                        "object and nothing else.")
            continue
        except Exception as exc:  # transport, quota, timeout
            last_error = f"{type(exc).__name__}: {exc}"[:800]
            feedback = ""
            continue
        if log is not None:
            log.add(CallRecord(
                task=task, provider=provider, model=model, prompt_version=prompt_version,
                input_sha256=input_key, latency_ms=int((time.monotonic() - started) * 1000),
                prompt_tokens=int((usage or {}).get("prompt_tokens") or 0),
                completion_tokens=int((usage or {}).get("completion_tokens") or 0),
                retries=attempt, ok=True))
        return value
    if log is not None:
        log.add(CallRecord(
            task=task, provider=provider, model=model, prompt_version=prompt_version,
            input_sha256=input_key, latency_ms=int((time.monotonic() - started) * 1000),
            retries=attempts, ok=False, error=last_error))
    raise StructuredOutputError(f"{task}: no schema-valid reply after {attempts} attempts "
                                f"({last_error})")
