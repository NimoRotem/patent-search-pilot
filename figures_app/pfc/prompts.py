"""Prompt loading and versioning.

Prompts are files, not string literals scattered through the modules that call them. A prompt
is the specification of a model's task, and a change to one changes the system's behaviour as
surely as a code change does; keeping them on disk means a change is a reviewable diff, and
hashing the file gives every artifact a record of exactly which wording produced it.
"""
from __future__ import annotations

import functools
import hashlib
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


class PromptMissing(FileNotFoundError):
    pass


@functools.lru_cache(maxsize=32)
def load(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptMissing(f"prompt {name!r} not found in {PROMPT_DIR}")
    return path.read_text(encoding="utf-8").strip()


@functools.lru_cache(maxsize=32)
def version(name: str) -> str:
    """``<name>@<content hash>`` — the identity recorded on artifacts and call logs."""
    digest = hashlib.sha256(load(name).encode("utf-8")).hexdigest()[:10]
    return f"{name}@{digest}"


def versions(*names: str) -> dict[str, str]:
    return {name: version(name) for name in names}
