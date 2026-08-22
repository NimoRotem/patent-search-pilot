"""Small shared records used by discovery, fetching, and parsing."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Completeness:
    has_title: bool = False
    has_abstract: bool = False
    has_claims: bool = False
    has_complete_claims: bool = False
    has_description: bool = False
    has_complete_description: bool = False
    has_figures: bool = False
    has_citations: bool = False

    @property
    def full_text_complete(self) -> bool:
        return self.has_complete_claims and self.has_complete_description

    def missing_text_fields(self) -> frozenset[str]:
        missing = set()
        if not self.has_complete_claims:
            missing.add("claims")
        if not self.has_complete_description:
            missing.add("description")
        return frozenset(missing)


@dataclass(frozen=True)
class FetchRequest:
    publication_id: str
    publication_number: str
    authority: str
    missing_fields: frozenset[str]
    completeness: Completeness = field(default_factory=Completeness)
    kind_code: str = ""
    language: str = ""
    family_id: str = ""
    raw_object_uri: str = ""
    skip_providers: frozenset[str] = frozenset()
    require_artifact: bool = False
    local_only: bool = False


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    content: bytes
    media_type: str
    source_url: str
    http_status: int | None = None
    latency_ms: int = 0
    credits_used: int = 0
    completeness: Completeness = field(default_factory=Completeness)
    response_headers: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
