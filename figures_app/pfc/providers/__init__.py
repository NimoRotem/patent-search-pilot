"""Provider selection. Business logic asks for a role, never for a vendor."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from .base import (CallLog, CallRecord, ModelUnavailable, StructuredOutputError,
                   TextReasoner, VisionVerifier, coerce, extract_json, input_hash,
                   with_retries)

TEXT_PROVIDER = os.environ.get("PFC_TEXT_PROVIDER", "vertex")
VISION_PROVIDER = os.environ.get("PFC_VISION_PROVIDER", "vertex")


def text_reasoner(log: Optional[CallLog] = None, model: str = "") -> TextReasoner:
    if TEXT_PROVIDER != "vertex":
        raise ModelUnavailable(f"unknown text provider {TEXT_PROVIDER!r}")
    from .gemini import DEFAULT_TEXT_MODEL, GeminiTextReasoner
    return GeminiTextReasoner(model=model or DEFAULT_TEXT_MODEL, log=log)


def vision_verifier(log: Optional[CallLog] = None, model: str = "") -> VisionVerifier:
    if VISION_PROVIDER != "vertex":
        raise ModelUnavailable(f"unknown vision provider {VISION_PROVIDER!r}")
    from .gemini import DEFAULT_VISION_MODEL, GeminiVisionVerifier
    return GeminiVisionVerifier(model=model or DEFAULT_VISION_MODEL, log=log)


def second_verifier(log: Optional[CallLog] = None) -> VisionVerifier:
    """The independent second reader used in strict mode and on disagreement."""
    from .gemini import DEFAULT_VERIFIER_B_MODEL, GeminiVisionVerifier
    return GeminiVisionVerifier(model=DEFAULT_VERIFIER_B_MODEL, log=log)


def config_hash() -> str:
    """A stable fingerprint of the model configuration, recorded on every artifact."""
    from .gemini import (DEFAULT_LOCATION, DEFAULT_TEXT_MODEL, DEFAULT_VERIFIER_B_MODEL,
                         DEFAULT_VISION_MODEL)
    payload = {
        "text_provider": TEXT_PROVIDER, "text_model": DEFAULT_TEXT_MODEL,
        "vision_provider": VISION_PROVIDER, "vision_model": DEFAULT_VISION_MODEL,
        "verifier_b_model": DEFAULT_VERIFIER_B_MODEL, "location": DEFAULT_LOCATION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


__all__ = [
    "CallLog", "CallRecord", "ModelUnavailable", "StructuredOutputError", "TextReasoner",
    "VisionVerifier", "coerce", "config_hash", "extract_json", "input_hash",
    "second_verifier", "text_reasoner", "vision_verifier", "with_retries",
]
