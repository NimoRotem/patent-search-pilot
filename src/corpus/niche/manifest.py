"""Canonical, idempotent niche publication records."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, replace

from .identifiers import (
    authority_of,
    kind_code_of,
    normalize_classification,
    normalize_family_id,
    normalize_publication_number,
)

_NULLABLE_TEXT_FIELDS = (
    "publication_id",
    "family_id",
    "authority",
    "kind_code",
    "title",
    "abstract",
    "language",
    "publication_date",
    "filing_date",
    "earliest_priority_date",
    "preferred_source",
    "raw_object_uri",
    "parsed_object_uri",
    "last_provider",
    "last_error",
    "updated_at",
)


@dataclass(frozen=True)
class PublicationRecord:
    publication_number: str
    publication_id: str = ""
    family_id: str = ""
    authority: str = ""
    kind_code: str = ""
    title: str = ""
    abstract: str = ""
    language: str = ""
    cpc_codes: tuple[str, ...] = ()
    ipc_codes: tuple[str, ...] = ()
    publication_date: str = ""
    filing_date: str = ""
    earliest_priority_date: str = ""
    has_title: bool = False
    has_abstract: bool = False
    has_claims: bool = False
    has_complete_claims: bool = False
    has_description: bool = False
    has_complete_description: bool = False
    has_figures: bool = False
    has_citations: bool = False
    preferred_source: str = ""
    raw_object_uri: str = ""
    parsed_object_uri: str = ""
    fetch_status: str = "pending"
    fetch_attempts: int = 0
    last_provider: str = ""
    last_error: str = ""
    priority: int = 4
    discovery_signals: tuple[str, ...] = ()
    updated_at: str = ""

    def __post_init__(self):
        for name in _NULLABLE_TEXT_FIELDS:
            object.__setattr__(self, name, str(getattr(self, name) or ""))
        publication_number = normalize_publication_number(self.publication_number)
        object.__setattr__(self, "publication_number", publication_number)
        object.__setattr__(self, "publication_id", self.publication_id or publication_number)
        object.__setattr__(self, "authority", self.authority or authority_of(publication_number))
        object.__setattr__(self, "kind_code", self.kind_code or kind_code_of(publication_number))
        object.__setattr__(self, "cpc_codes", _clean_codes(self.cpc_codes))
        object.__setattr__(self, "ipc_codes", _clean_codes(self.ipc_codes))
        object.__setattr__(
            self, "discovery_signals",
            tuple(sorted({str(value) for value in self.discovery_signals if str(value)})),
        )
        if self.title and not self.has_title:
            object.__setattr__(self, "has_title", True)
        if self.abstract and not self.has_abstract:
            object.__setattr__(self, "has_abstract", True)
        if (
            self.fetch_status == "pending"
            and self.has_complete_claims
            and self.has_complete_description
        ):
            object.__setattr__(self, "fetch_status", "completed")


def _clean_codes(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        code for code in (normalize_classification(value) for value in (values or ())) if code
    ))


def family_key(family_id: str, publication_number: str) -> str:
    return normalize_family_id(family_id, publication_number)


_BOOL_FIELDS = tuple(field.name for field in fields(PublicationRecord)
                     if field.name.startswith("has_"))
_TEXT_FIELDS = (
    "title", "abstract", "language", "publication_date", "filing_date",
    "earliest_priority_date", "preferred_source", "raw_object_uri",
    "parsed_object_uri", "last_provider", "last_error", "updated_at",
)


def _merge_pair(old: PublicationRecord, new: PublicationRecord) -> PublicationRecord:
    values = {}
    for name in _TEXT_FIELDS:
        before, after = getattr(old, name), getattr(new, name)
        if name in ("title", "abstract"):
            values[name] = after if len(after or "") > len(before or "") else before
        else:
            values[name] = after or before
    for name in _BOOL_FIELDS:
        values[name] = bool(getattr(old, name) or getattr(new, name))
    values.update(
        family_id=new.family_id or old.family_id,
        authority=new.authority or old.authority,
        kind_code=new.kind_code or old.kind_code,
        cpc_codes=_clean_codes((*old.cpc_codes, *new.cpc_codes)),
        ipc_codes=_clean_codes((*old.ipc_codes, *new.ipc_codes)),
        fetch_attempts=max(old.fetch_attempts, new.fetch_attempts),
        fetch_status=(
            "completed"
            if "completed" in (old.fetch_status, new.fetch_status)
            else new.fetch_status
            if new.fetch_status and new.fetch_status != "pending"
            else old.fetch_status or "pending"
        ),
        priority=min(old.priority, new.priority),
        discovery_signals=tuple(sorted(set(old.discovery_signals) | set(new.discovery_signals))),
    )
    return replace(old, **values)


def merge_publications(records: Iterable[PublicationRecord]) -> dict[str, PublicationRecord]:
    merged: dict[str, PublicationRecord] = {}
    for record in records:
        if not isinstance(record, PublicationRecord):
            record = PublicationRecord(**dict(record))
        key = record.publication_id
        merged[key] = _merge_pair(merged[key], record) if key in merged else record
    return dict(sorted(merged.items()))


def _preference(record: PublicationRecord) -> tuple:
    return (
        int(record.has_complete_claims and record.has_complete_description),
        int(record.has_complete_claims),
        int(record.has_complete_description),
        int(record.language.lower().startswith("en")),
        int(record.has_claims) + int(record.has_description),
        int(record.has_abstract),
        record.publication_date or "",
        record.publication_number,
    )


def choose_family_fetch_targets(records: Iterable[PublicationRecord]) -> list[PublicationRecord]:
    by_family: dict[str, list[PublicationRecord]] = {}
    for record in merge_publications(records).values():
        by_family.setdefault(family_key(record.family_id, record.publication_number), []).append(record)
    selected = [max(members, key=_preference) for members in by_family.values()]
    return sorted(selected, key=lambda record: (record.priority, record.publication_number))


class MemoryManifest:
    """Small manifest implementation for dry runs and unit tests."""

    def __init__(self):
        self.rows: dict[str, PublicationRecord] = {}
        self.watermarks: dict[str, int] = {}

    def load_watermarks(self) -> Mapping[str, int]:
        return dict(self.watermarks)

    def upsert_publications(self, records: Iterable[PublicationRecord]) -> None:
        self.rows = merge_publications((*self.rows.values(), *records))

    def save_watermarks(self, watermarks: Mapping[str, int]) -> None:
        self.watermarks.update({str(key): int(value) for key, value in watermarks.items()})
