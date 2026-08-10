"""Deterministic, provenance-preserving patent figure compiler.

This is deliberately separate from :mod:`draft_figures`.  That module makes useful raster
concept sketches and supports image-to-image editing.  This module turns only disclosed,
approved semantic facts into inspectable SVG/PDF filing artifacts.  It never asks an image model
to invent geometry and it never lets a validator mutate the artifact it inspected.

The public functions are pure apart from loading a versioned ruleset and producing PDF bytes.
Persistence, account ownership, and approval audit trails live in ``figure_compiler_service``.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from html import escape
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    ValidationError,
    model_validator,
)

PIR_SCHEMA_VERSION = "pir-1"
MANIFEST_SCHEMA_VERSION = "figure-manifest-1"
DSL_SCHEMA_VERSION = "figure-dsl-1"
RENDERER_VERSION = "semantic-svg-1.0.0"
RULES_DIR = Path(__file__).resolve().parents[1] / "rules" / "figure_compiler"

WORKFLOW_STAGES = (
    "INGESTED", "PARSED", "DISCLOSURE_EXTRACTED", "MODEL_RECONCILED", "MODEL_APPROVED",
    "FIGURES_PLANNED", "MANIFEST_APPROVED", "FIGURE_SPECS_COMPILED", "RENDERED",
    "ANNOTATED", "COMPOSED", "VALIDATED", "FINAL_REVIEW", "APPROVED", "EXPORTED",
)

_NUMERAL_RE = re.compile(r"\b([A-Za-z]?\d{1,4}[A-Za-z]?)\b")
_CLAIM_RE = re.compile(r"(?ms)^\s*(\d{1,3})\s*[.)]\s*(.*?)(?=^\s*\d{1,3}\s*[.)]\s|\Z)")
_FIG_RE = re.compile(r"\bFIG(?:URE)?\.?\s*([0-9]+[A-Za-z]?)\b", re.IGNORECASE)
_NORMAL_RE = re.compile(r"[^a-z0-9]+")


class FigureCompilerError(ValueError):
    """A safe, user-displayable compiler failure."""


class ApprovalRequired(FigureCompilerError):
    """Raised when compilation or export crosses an explicit human gate."""


class CompilationBlocked(FigureCompilerError):
    """Raised when disclosed facts cannot currently produce a valid artifact."""


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SourceSpanContract(_Contract):
    id: str = Field(min_length=4, max_length=180)
    source: str = Field(min_length=1, max_length=80)
    locator: str = Field(min_length=1, max_length=300)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def end_is_after_start(self):
        if self.end < self.start:
            raise ValueError("source span ends before it starts")
        return self


class _EntityContract(_Contract):
    id: str
    reference: str = Field(pattern=r"^[A-Z]?\d{1,4}[A-Z]?$|^\d{1,4}[A-Z]$")
    name: str = Field(min_length=1, max_length=300)
    aliases: list[str]
    source_span_ids: list[str] = Field(min_length=1)
    conflicted: bool


class _RelationContract(_Contract):
    id: str
    from_entity_id: str
    to_entity_id: str
    predicate: str = Field(min_length=1, max_length=80)
    source_span_ids: list[str] = Field(min_length=1)


class _ClaimLimitationContract(_Contract):
    id: str
    claim_no: int = Field(gt=0)
    text: str = Field(min_length=1)
    entity_ids: list[str]
    drawable: bool
    source_span_ids: list[str] = Field(min_length=1)


class _ClaimCoverageContract(_Contract):
    limitation_id: str
    claim_no: int = Field(gt=0)
    drawable: bool
    entity_ids: list[str]
    figure_ids: list[str]
    status: Literal["covered", "uncovered"]


class _ReferenceConflictContract(_Contract):
    id: str
    kind: Literal["one_reference_multiple_entities", "one_entity_multiple_references"]
    candidates: list[str] = Field(min_length=2)
    material: bool
    status: Literal["unresolved", "resolved"]
    source_span_ids: list[str] = Field(min_length=1)
    reference: str | None = None
    entity: str | None = None
    resolution: dict[str, Any] | None = None


class _BlockerContract(_Contract):
    code: str
    message: str
    conflict_id: str | None = None
    limitation_id: str | None = None


class _PIRContract(_Contract):
    schema_version: Literal["pir-1"]
    source_spans: list[_SourceSpanContract]
    entities: list[_EntityContract]
    relations: list[_RelationContract]
    claim_limitations: list[_ClaimLimitationContract]
    claim_coverage: list[_ClaimCoverageContract]
    reference_conflicts: list[_ReferenceConflictContract]
    hard_blockers: list[_BlockerContract]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    approval: dict[str, Any] | None = None
    artifact_type: Literal["canonical_model"] | None = None


class _ManifestFigureContract(_Contract):
    id: str
    label: str
    caption: str
    view_type: str
    renderer: Literal["graph", "mechanical"]
    entity_ids: list[str] = Field(min_length=1)
    relation_ids: list[str]
    source_span_ids: list[str] = Field(min_length=1)


class _ManifestContract(_Contract):
    schema_version: Literal["figure-manifest-1"]
    artifact_type: Literal["figure_manifest"]
    artifact_version: int = Field(gt=0)
    approval: dict[str, Any] | None
    figures: list[_ManifestFigureContract]
    issues: list[dict[str, Any]]
    pir_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _NodeContract(_Contract):
    entity_id: str
    reference: str
    name: str
    source_span_ids: list[str] = Field(min_length=1)
    x: FiniteFloat
    y: FiniteFloat
    width: FiniteFloat = Field(gt=0)
    height: FiniteFloat = Field(gt=0)
    shape: Literal["ellipse", "box"]


class _LabelContract(_Contract):
    entity_id: str
    reference: str
    source_span_ids: list[str] = Field(min_length=1)
    x: FiniteFloat
    y: FiniteFloat
    target_x: FiniteFloat
    target_y: FiniteFloat


class _DSLFigureContract(_Contract):
    id: str
    label: str
    caption: str
    view_type: str
    renderer: Literal["graph", "mechanical"]
    source_span_ids: list[str] = Field(min_length=1)
    entities: list[_NodeContract] = Field(min_length=1)
    relations: list[_RelationContract]
    labels: list[_LabelContract] = Field(min_length=1)


class _SheetContract(_Contract):
    sheet_number: int = Field(gt=0)
    figure_id: str
    svg: str = Field(min_length=100)


class _PackageContract(_Contract):
    schema_version: Literal["figure-dsl-1"]
    artifact_type: Literal["compiled_figure_package"]
    artifact_version: int = Field(gt=0)
    approval: dict[str, Any] | None
    parent_sha256: str | None
    renderer_version: str
    ruleset: str
    pir_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    figures: list[_DSLFigureContract] = Field(min_length=1)
    patch: dict[str, Any] | None
    sheets: list[_SheetContract] = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _TypedPatchContract(_Contract):
    type: Literal["move_label", "move_entity", "delete_visible_entity", "reroute_leader"]
    figure_id: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    x: FiniteFloat | None = None
    y: FiniteFloat | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def coordinates_for_moves(self):
        if self.type != "delete_visible_entity" and (self.x is None or self.y is None):
            raise ValueError("a move/reroute patch requires x and y")
        return self


def validate_pir_contract(value: Mapping[str, Any]) -> None:
    try:
        _PIRContract.model_validate(value)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first.get("loc") or ()) or "root"
        raise FigureCompilerError(
            f"PIR contract failed at {location}: {first.get('msg')}") from exc


def _validate_contract(model: type[BaseModel], value: Mapping[str, Any], label: str) -> None:
    try:
        model.model_validate(value)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first.get("loc") or ()) or "root"
        raise FigureCompilerError(
            f"{label} contract failed at {location}: {first.get('msg')}") from exc


def validate_manifest_contract(value: Mapping[str, Any]) -> None:
    _validate_contract(_ManifestContract, value, "Figure manifest")


def validate_package_contract(value: Mapping[str, Any]) -> None:
    _validate_contract(_PackageContract, value, "Figure package")


def validate_patch_contract(value: Mapping[str, Any]) -> None:
    _validate_contract(_TypedPatchContract, value, "Typed patch")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean(value: Any, limit: int = 20_000) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()[:limit]


def _normal(value: Any) -> str:
    return _NORMAL_RE.sub(" ", _clean(value).lower()).strip()


def _canonical_reference(value: Any) -> str:
    match = _NUMERAL_RE.search(str(value or ""))
    return match.group(1).upper() if match else ""


def _sort_reference(value: str) -> tuple[int, str, str]:
    match = re.match(r"([A-Z]*)(\d+)([A-Z]*)", value or "")
    return (int(match.group(2)), match.group(1), match.group(3)) if match else (999999, value, "")


def _stable_id(prefix: str, *values: Any) -> str:
    raw = "\x1f".join(str(value) for value in values)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def _span(span_id: str, source: str, locator: str, text: str, start: int = 0) -> dict[str, Any]:
    return {
        "id": span_id, "source": source, "locator": locator, "start": start,
        "end": start + len(text), "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _section_spans(sections: Mapping[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for section, raw in sections.items():
        text = str(raw or "")
        cursor = 0
        blocks = [block for block in re.split(r"\n\s*\n|(?<=\.)\s+(?=[A-Z])", text)
                  if block.strip()]
        for index, block in enumerate(blocks, 1):
            clean = block.strip()
            start = text.find(clean, cursor)
            if start < 0:
                start = cursor
            cursor = start + len(clean)
            sid = f"span-{section}-{index:04d}-{hashlib.sha256(clean.encode()).hexdigest()[:8]}"
            out.append(_span(sid, "draft", f"{section}:{index}", clean, start))
    return out


def _figure_id(label: Any, fallback: int = 1) -> str:
    match = _FIG_RE.search(str(label or ""))
    suffix = match.group(1).lower() if match else str(fallback)
    return f"figure-{suffix}"


def _figure_label(label: Any, fallback: int = 1) -> str:
    match = _FIG_RE.search(str(label or ""))
    return f"FIG. {match.group(1).upper()}" if match else f"FIG. {fallback}"


def _source_ids_for(text: str, spans: Sequence[Mapping[str, Any]]) -> list[str]:
    lower = _normal(text)
    tokens = [token for token in lower.split() if len(token) > 2]
    matches = []
    for span in spans:
        source = _normal(span.get("text"))
        if lower and (lower in source or source in lower or (tokens and all(t in source for t in tokens[-2:]))):
            matches.append(str(span["id"]))
    return matches


def _split_claims(text: str) -> list[tuple[int, str]]:
    claims = [(int(match.group(1)), _clean(match.group(2), 100_000))
              for match in _CLAIM_RE.finditer(str(text or ""))]
    if claims:
        return claims
    clean = _clean(text, 100_000)
    return [(1, clean)] if clean else []


def _predicate(sentence: str) -> str | None:
    value = sentence.lower()
    predicates = (
        ("received_in", ("received in", "seated in", "fitted in")),
        ("carried_by", ("carried by", "carries", "mounted on", "supported by")),
        ("connected_to", ("connected to", "coupled to", "joined to", "attached to")),
        ("communicates_with", ("communicates with", "in fluid communication", "draws air")),
        ("passes_through", ("passes through", "extends through", "through a")),
        ("adjacent_to", ("adjacent to", "beside")),
        ("contains", ("contains", "includes", "comprises", "has a")),
    )
    return next((name for name, phrases in predicates if any(phrase in value for phrase in phrases)), None)


def build_pir(sections: Mapping[str, str], numerals: Sequence[Mapping[str, Any]],
              figure_specs: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Build the Patent Intermediate Representation from disclosed draft artifacts only."""
    section_map = {str(key): str(value or "") for key, value in (sections or {}).items()}
    spans = _section_spans(section_map)

    by_reference: dict[str, list[str]] = {}
    by_name: dict[str, set[str]] = {}
    registry_spans: dict[tuple[str, str], str] = {}
    for index, item in enumerate(numerals or (), 1):
        reference = _canonical_reference(item.get("numeral"))
        part = _clean(item.get("part"), 300)
        if not reference or not part:
            continue
        by_reference.setdefault(reference, [])
        if part not in by_reference[reference]:
            by_reference[reference].append(part)
        by_name.setdefault(_normal(part), set()).add(reference)
        row = f"{reference} = {part}"
        sid = f"span-reference-registry-{index:04d}-{hashlib.sha256(row.encode()).hexdigest()[:8]}"
        spans.append(_span(sid, "reference_registry", f"row:{index}", row))
        registry_spans[(reference, part)] = sid

    for index, spec in enumerate(figure_specs or (), 1):
        label = _figure_label(spec.get("label"), index)
        caption = _clean(spec.get("caption"), 1000)
        refs = ", ".join(_canonical_reference(v) for v in spec.get("numerals") or ()
                         if _canonical_reference(v))
        row = f"{label}: {caption}; references {refs}"
        sid = f"span-figure-spec-{index:04d}-{hashlib.sha256(row.encode()).hexdigest()[:8]}"
        spans.append(_span(sid, "figure_spec", f"figure:{index}", row))

    conflicts: list[dict[str, Any]] = []
    for reference, candidates in sorted(by_reference.items(), key=lambda item: _sort_reference(item[0])):
        if len({_normal(value) for value in candidates}) > 1:
            conflicts.append({
                "id": _stable_id("conflict", "reference", reference, *candidates),
                "kind": "one_reference_multiple_entities", "reference": reference,
                "candidates": candidates, "material": True, "status": "unresolved",
                "source_span_ids": [registry_spans[(reference, candidate)] for candidate in candidates],
            })
    for name, references in sorted(by_name.items()):
        if len(references) > 1:
            candidates = sorted(references, key=_sort_reference)
            conflicts.append({
                "id": _stable_id("conflict", "entity", name, *candidates),
                "kind": "one_entity_multiple_references", "entity": name,
                "candidates": candidates, "material": True, "status": "unresolved",
                "source_span_ids": [registry_spans[(ref, by_reference[ref][0])]
                                    for ref in candidates],
            })

    entities: list[dict[str, Any]] = []
    for reference, candidates in sorted(by_reference.items(), key=lambda item: _sort_reference(item[0])):
        name = candidates[0]
        source_ids = [registry_spans[(reference, candidate)] for candidate in candidates]
        for span in spans:
            body = str(span.get("text") or "")
            if re.search(rf"\b{re.escape(reference)}\b", body) and _normal(name) in _normal(body):
                source_ids.append(str(span["id"]))
        entities.append({
            "id": f"entity-{reference.lower()}", "reference": reference, "name": name,
            "aliases": candidates[1:], "source_span_ids": list(dict.fromkeys(source_ids)),
            "conflicted": any(c.get("reference") == reference for c in conflicts),
        })

    relations: list[dict[str, Any]] = []
    for span in spans:
        if span.get("source") != "draft":
            continue
        sentence = str(span.get("text") or "")
        predicate = _predicate(sentence)
        if not predicate:
            continue
        present = [entity for entity in entities
                   if (_normal(entity["name"]) in _normal(sentence) or
                       re.search(rf"\b{re.escape(entity['reference'])}\b", sentence))]
        present = list({entity["id"]: entity for entity in present}.values())
        if len(present) < 2:
            continue
        for left, right in pairwise(present):
            relations.append({
                "id": _stable_id("relation", left["id"], predicate, right["id"], span["id"]),
                "from_entity_id": left["id"], "to_entity_id": right["id"],
                "predicate": predicate, "source_span_ids": [span["id"]],
            })

    figure_entities: dict[str, set[str]] = {}
    for index, spec in enumerate(figure_specs or (), 1):
        fid = _figure_id(spec.get("label"), index)
        refs = {_canonical_reference(value) for value in spec.get("numerals") or ()}
        figure_entities[fid] = {entity["id"] for entity in entities
                                if entity["reference"] in refs}

    claim_limitations: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for claim_no, claim_text in _split_claims(section_map.get("claims", "")):
        clauses = [part.strip(" ,;") for part in re.split(
            r";|,\s+(?=(?:and\s+)?(?:a|an|the|wherein)\b)", claim_text, flags=re.IGNORECASE)
                   if part.strip(" ,;")]
        for index, clause in enumerate(clauses or [claim_text], 1):
            matched = [entity["id"] for entity in entities
                       if _normal(entity["name"]) in _normal(clause)]
            drawable = bool(matched)
            figures = sorted(fid for fid, eids in figure_entities.items()
                             if set(matched) & eids)
            span_ids = _source_ids_for(clause, spans) or [
                str(span["id"]) for span in spans
                if span.get("source") == "draft" and str(span.get("locator") or "").startswith("claims:")
            ][:1]
            limitation_id = f"claim-{claim_no}-limitation-{index}"
            claim_limitations.append({
                "id": limitation_id, "claim_no": claim_no, "text": clause,
                "entity_ids": matched, "drawable": drawable, "source_span_ids": span_ids,
            })
            coverage.append({
                "limitation_id": limitation_id, "claim_no": claim_no, "drawable": drawable,
                "entity_ids": matched, "figure_ids": figures,
                "status": "covered" if figures or not drawable else "uncovered",
            })

    blockers = [{
        "code": "unresolved_reference_conflict", "message":
            "A material reference-sign conflict must be resolved before figures are compiled.",
        "conflict_id": conflict["id"],
    } for conflict in conflicts if conflict["material"] and conflict["status"] == "unresolved"]
    blockers.extend({
        "code": "uncovered_drawable_claim", "message":
            f"Drawable limitation {item['limitation_id']} is absent from the proposed figures.",
        "limitation_id": item["limitation_id"],
    } for item in coverage if item["drawable"] and not item["figure_ids"])

    payload = {
        "schema_version": PIR_SCHEMA_VERSION,
        "source_spans": spans, "entities": entities, "relations": relations,
        "claim_limitations": claim_limitations, "claim_coverage": coverage,
        "reference_conflicts": conflicts, "hard_blockers": blockers,
        "input_sha256": hashlib.sha256(json.dumps(
            {"sections": section_map, "numerals": list(numerals or ()),
             "figure_specs": list(figure_specs or ())}, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
    }
    validate_pir_contract(payload)
    return payload


def resolve_reference_conflict(pir: Mapping[str, Any], *, conflict_id: str, choice: str,
                               resolved_by_user_id: int) -> dict[str, Any]:
    """Record an explicit human reconciliation in a new PIR value.

    The compiler can safely offer a candidate choice when one reference sign was assigned two
    names.  It cannot decide that choice itself.  More structural conflicts remain blocked until
    the draft/reference table is revised; guessing that two signs mean one object could delete
    disclosed subject matter.
    """
    if int(resolved_by_user_id) <= 0:
        raise FigureCompilerError("A named account must resolve the reference conflict.")
    out = copy.deepcopy(dict(pir))
    conflict = next((row for row in out.get("reference_conflicts") or ()
                     if row.get("id") == str(conflict_id)), None)
    if not conflict:
        raise FigureCompilerError("The reference conflict was not found.")
    if conflict.get("status") == "resolved":
        raise FigureCompilerError("That reference conflict is already resolved.")
    if conflict.get("kind") != "one_reference_multiple_entities":
        raise CompilationBlocked(
            "Revise the reference table to distinguish the conflicting reference signs.")
    candidates = list(conflict.get("candidates") or ())
    selected = next((candidate for candidate in candidates
                     if _normal(candidate) == _normal(choice)), None)
    if not selected:
        raise FigureCompilerError("Choose one of the disclosed names for this reference sign.")
    entity = next((row for row in out.get("entities") or ()
                   if row.get("reference") == conflict.get("reference")), None)
    if not entity:
        raise FigureCompilerError("The conflicted registry entity was not found.")
    entity["name"] = selected
    entity["aliases"] = [candidate for candidate in candidates if candidate != selected]
    entity["conflicted"] = False
    conflict["status"] = "resolved"
    conflict["resolution"] = {
        "choice": selected, "resolved_by_user_id": int(resolved_by_user_id),
        "resolved_at": _now(),
    }
    out["hard_blockers"] = [
        blocker for blocker in out.get("hard_blockers") or ()
        if blocker.get("conflict_id") != conflict["id"]
    ]
    out["reconciliation_sha256"] = content_hash({
        "input_sha256": out.get("input_sha256"),
        "reference_conflicts": out.get("reference_conflicts"),
    })
    out.pop("approval", None)
    validate_pir_contract(out)
    return out


def _view_type(caption: str) -> str:
    value = caption.lower()
    if "flow" in value or "process" in value:
        return "flow"
    if "network" in value or "system" in value or "block diagram" in value:
        return "network"
    if "exploded" in value:
        return "exploded"
    if "section" in value or "cross-section" in value:
        return "section"
    return "mechanical"


def plan_manifest(pir: Mapping[str, Any],
                  figure_specs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Create the smallest figure set supported by the supplied figure specifications."""
    entity_by_ref = {str(entity["reference"]): entity for entity in pir.get("entities") or ()}
    relation_rows = list(pir.get("relations") or ())
    figures = []
    issues = []
    for index, spec in enumerate(figure_specs or (), 1):
        refs = list(dict.fromkeys(filter(None, (
            _canonical_reference(item) for item in spec.get("numerals") or ()))))
        supported = [entity_by_ref[ref] for ref in refs if ref in entity_by_ref]
        unsupported = [ref for ref in refs if ref not in entity_by_ref]
        if unsupported:
            issues.append({"code": "unsupported_manifest_reference", "severity": "blocker",
                           "figure_id": _figure_id(spec.get("label"), index),
                           "references": unsupported})
        if not supported:
            continue
        ids = {entity["id"] for entity in supported}
        relations = [row["id"] for row in relation_rows
                     if row["from_entity_id"] in ids and row["to_entity_id"] in ids]
        caption = _clean(spec.get("caption"), 1000)
        figures.append({
            "id": _figure_id(spec.get("label"), index),
            "label": _figure_label(spec.get("label"), index),
            "caption": caption, "view_type": _view_type(caption),
            "renderer": "graph" if _view_type(caption) in {"flow", "network"} else "mechanical",
            "entity_ids": [entity["id"] for entity in supported],
            "relation_ids": relations,
            "source_span_ids": list(dict.fromkeys(
                source for entity in supported for source in entity["source_span_ids"])),
        })
    if not figures:
        issues.append({"code": "no_supported_figures", "severity": "blocker",
                       "message": "No disclosed figure specification can be compiled."})
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION, "artifact_type": "figure_manifest",
        "artifact_version": 1, "approval": None, "figures": figures, "issues": issues,
        "pir_input_sha256": pir.get("input_sha256"),
    }
    validate_manifest_contract(payload)
    return payload


def content_hash(value: Mapping[str, Any]) -> str:
    clean = copy.deepcopy(dict(value))
    clean.pop("content_sha256", None)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode()).hexdigest()


def approve_artifact(value: Mapping[str, Any], *, artifact_type: str, user_id: int,
                     approved_at: str | None = None) -> dict[str, Any]:
    out = copy.deepcopy(dict(value))
    if int(user_id) <= 0:
        raise FigureCompilerError("A named account must approve this artifact.")
    if out.get("artifact_type") not in (None, artifact_type):
        raise FigureCompilerError("The approval type does not match this artifact.")
    blockers = [item for item in out.get("issues") or () if item.get("severity") == "blocker"]
    if blockers:
        raise CompilationBlocked("Resolve the blocking manifest issues before approval.")
    out["artifact_type"] = artifact_type
    out["approval"] = {
        "artifact_type": artifact_type, "approved_by_user_id": int(user_id),
        "approved_at": approved_at or _now(), "approved_sha256": content_hash(out),
    }
    return out


def load_ruleset(name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9.-]{4,80}", str(name or "")):
        raise FigureCompilerError("Unknown drawing ruleset.")
    path = RULES_DIR / f"{name}.json"
    if not path.is_file():
        raise FigureCompilerError("Unknown drawing ruleset.")
    rules = json.loads(path.read_text(encoding="utf-8"))
    if rules.get("id") != name:
        raise FigureCompilerError("The drawing ruleset identifier is inconsistent.")
    return rules


def _layout_figure(manifest_figure: Mapping[str, Any], pir: Mapping[str, Any],
                   rules: Mapping[str, Any]) -> dict[str, Any]:
    entities_by_id = {row["id"]: row for row in pir.get("entities") or ()}
    relations_by_id = {row["id"]: row for row in pir.get("relations") or ()}
    selected = [entities_by_id[eid] for eid in manifest_figure.get("entity_ids") or ()
                if eid in entities_by_id]
    unit = float(rules["units_per_mm"])
    width, height = (float(v) * unit for v in rules["sheet_mm"])
    margins = {key: float(value) * unit for key, value in rules["margins_mm"].items()}
    x0, x1 = margins["left"], width - margins["right"]
    y0, y1 = margins["top"] + 130, height - margins["bottom"] - 160
    cols = max(1, min(3, math.ceil(math.sqrt(len(selected) or 1))))
    rows = max(1, math.ceil((len(selected) or 1) / cols))
    cell_w, cell_h = (x1 - x0) / cols, (y1 - y0) / rows
    node_w = min(390.0, max(210.0, cell_w * 0.42))
    node_h = min(260.0, max(130.0, cell_h * 0.28))
    nodes = []
    labels = []
    for index, entity in enumerate(selected):
        row, col = divmod(index, cols)
        cx = x0 + cell_w * (col + 0.5)
        cy = y0 + cell_h * (row + 0.5)
        shape = "ellipse" if index == 0 or manifest_figure.get("renderer") == "mechanical" else "box"
        node = {
            "entity_id": entity["id"], "reference": entity["reference"],
            "name": entity["name"], "source_span_ids": entity["source_span_ids"],
            "x": round(cx - node_w / 2, 2), "y": round(cy - node_h / 2, 2),
            "width": round(node_w, 2), "height": round(node_h, 2), "shape": shape,
        }
        nodes.append(node)
        right = index % 2 == 0
        lx = min(x1 - 45, cx + node_w / 2 + 105) if right else max(x0 + 45, cx - node_w / 2 - 105)
        ly = cy - node_h * 0.25
        labels.append({
            "entity_id": entity["id"], "reference": entity["reference"],
            "source_span_ids": entity["source_span_ids"], "x": round(lx, 2), "y": round(ly, 2),
            "target_x": round(cx + (node_w / 2 if right else -node_w / 2), 2),
            "target_y": round(cy - node_h * 0.1, 2),
        })
    node_ids = {node["entity_id"] for node in nodes}
    relations = [copy.deepcopy(relations_by_id[rid])
                 for rid in manifest_figure.get("relation_ids") or ()
                 if rid in relations_by_id and relations_by_id[rid]["from_entity_id"] in node_ids
                 and relations_by_id[rid]["to_entity_id"] in node_ids]
    return {
        "id": manifest_figure["id"], "label": manifest_figure["label"],
        "caption": manifest_figure.get("caption") or "", "view_type": manifest_figure["view_type"],
        "renderer": manifest_figure["renderer"], "source_span_ids": manifest_figure["source_span_ids"],
        "entities": nodes, "relations": relations, "labels": labels,
    }


def _svg_number(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def _render_svg(figure: Mapping[str, Any], rules: Mapping[str, Any], sheet: int,
                total: int) -> str:
    unit = float(rules["units_per_mm"])
    width, height = (float(v) * unit for v in rules["sheet_mm"])
    stroke = float(rules["stroke_mm"]) * unit
    thin = float(rules["thin_stroke_mm"]) * unit
    font = float(rules["minimum_character_height_mm"]) * unit
    margins = {key: float(value) * unit for key, value in rules["margins_mm"].items()}
    nodes = {node["entity_id"]: node for node in figure.get("entities") or ()}
    metadata = escape(json.dumps({
        "figure_id": figure["id"], "ruleset": rules["id"], "renderer": RENDERER_VERSION,
    }, sort_keys=True))
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{rules["sheet_mm"][0]}mm" '
            f'height="{rules["sheet_mm"][1]}mm" viewBox="0 0 {_svg_number(width)} {_svg_number(height)}" '
            f'data-ruleset="{escape(str(rules["id"]), quote=True)}" '
            f'data-renderer-version="{RENDERER_VERSION}">'
        ),
        f'<metadata>{metadata}</metadata>',
        f'<rect width="{_svg_number(width)}" height="{_svg_number(height)}" fill="#ffffff"/>',
        (
            f'<g id="{escape(str(figure["id"]), quote=True)}" fill="none" stroke="#000000" '
            f'stroke-width="{_svg_number(stroke)}" stroke-linecap="round" stroke-linejoin="round">'
        ),
    ]
    for relation in figure.get("relations") or ():
        left, right = nodes.get(relation["from_entity_id"]), nodes.get(relation["to_entity_id"])
        if not left or not right:
            continue
        x1 = left["x"] + left["width"] / 2
        y1 = left["y"] + left["height"] / 2
        x2 = right["x"] + right["width"] / 2
        y2 = right["y"] + right["height"] / 2
        parts.append(
            f'<line x1="{_svg_number(x1)}" y1="{_svg_number(y1)}" x2="{_svg_number(x2)}" '
            f'y2="{_svg_number(y2)}" data-relation-id="{escape(str(relation["id"]), quote=True)}" '
            f'data-source-spans="{escape(" ".join(relation.get("source_span_ids") or ()), quote=True)}"/>')
    for node in figure.get("entities") or ():
        attrs = (f'data-entity-id="{escape(str(node["entity_id"]), quote=True)}" '
                 f'data-reference="{escape(str(node["reference"]), quote=True)}" '
                 f'data-source-spans="{escape(" ".join(node.get("source_span_ids") or ()), quote=True)}"')
        if node.get("shape") == "ellipse":
            parts.append(
                f'<ellipse cx="{_svg_number(node["x"] + node["width"] / 2)}" '
                f'cy="{_svg_number(node["y"] + node["height"] / 2)}" '
                f'rx="{_svg_number(node["width"] / 2)}" ry="{_svg_number(node["height"] / 2)}" {attrs}/>')
        else:
            parts.append(
                f'<rect x="{_svg_number(node["x"])}" y="{_svg_number(node["y"])}" '
                f'width="{_svg_number(node["width"])}" height="{_svg_number(node["height"])}" '
                f'rx="18" {attrs}/>')
    parts.append("</g>")
    parts.append(f'<g fill="#000000" stroke="#000000" stroke-width="{_svg_number(thin)}" '
                 f'font-family="Arial, Helvetica, sans-serif" font-size="{_svg_number(font)}">')
    for label in figure.get("labels") or ():
        parts.append(
            f'<line x1="{_svg_number(label["x"])}" y1="{_svg_number(label["y"] - font * .25)}" '
            f'x2="{_svg_number(label["target_x"])}" y2="{_svg_number(label["target_y"])}" '
            f'fill="none" data-leader-for="{escape(str(label["entity_id"]), quote=True)}"/>')
        parts.append(
            f'<text x="{_svg_number(label["x"])}" y="{_svg_number(label["y"])}" stroke="none" '
            f'data-reference-label="{escape(str(label["reference"]), quote=True)}" '
            f'data-entity-id="{escape(str(label["entity_id"]), quote=True)}" '
            f'data-source-spans="{escape(" ".join(label.get("source_span_ids") or ()), quote=True)}">'
            f'{escape(str(label["reference"]))}</text>')
    sheet_text = str(rules.get("sheet_number_format") or "{sheet}/{total}").format(
        sheet=sheet, total=total)
    parts.extend([
        (
            f'<text x="{_svg_number(width / 2)}" y="{_svg_number(margins["top"] + font)}" '
            f'text-anchor="middle" stroke="none" data-sheet-number="true">{escape(sheet_text)}</text>'
        ),
        (
            f'<text x="{_svg_number(width / 2)}" y="{_svg_number(height - margins["bottom"] - font)}" '
            f'text-anchor="middle" stroke="none" data-figure-label="true">{escape(str(figure["label"]))}</text>'
        ),
        "</g>", "</svg>",
    ])
    return "".join(parts)


def _compose(package: dict[str, Any], rules: Mapping[str, Any]) -> dict[str, Any]:
    total = len(package.get("figures") or ())
    package["sheets"] = [{
        "sheet_number": index, "figure_id": figure["id"],
        "svg": _render_svg(figure, rules, index, total),
    } for index, figure in enumerate(package.get("figures") or (), 1)]
    package["content_sha256"] = content_hash(package)
    return package


def compile_package(pir: Mapping[str, Any], manifest: Mapping[str, Any],
                    ruleset_name: str = "uspto-letter-2026.1") -> dict[str, Any]:
    validate_pir_contract(pir)
    validate_manifest_contract(manifest)
    pir_approval = pir.get("approval") or {}
    if pir_approval.get("artifact_type") != "canonical_model":
        raise ApprovalRequired("Approve the canonical model before compilation.")
    approval = manifest.get("approval") or {}
    if approval.get("artifact_type") != "figure_manifest":
        raise ApprovalRequired("Approve the figure manifest before compilation.")
    if pir.get("hard_blockers"):
        raise CompilationBlocked("Resolve the canonical-model blockers before compilation.")
    if manifest.get("issues"):
        blockers = [item for item in manifest["issues"] if item.get("severity") == "blocker"]
        if blockers:
            raise CompilationBlocked("Resolve the blocking manifest issues before compilation.")
    rules = load_ruleset(ruleset_name)
    package = {
        "schema_version": DSL_SCHEMA_VERSION, "artifact_type": "compiled_figure_package",
        "artifact_version": 1, "approval": None, "parent_sha256": None,
        "renderer_version": RENDERER_VERSION, "ruleset": ruleset_name,
        "pir_input_sha256": pir.get("input_sha256"),
        "manifest_approval_sha256": approval.get("approved_sha256"),
        "figures": [_layout_figure(figure, pir, rules) for figure in manifest.get("figures") or ()],
        "patch": None,
    }
    package = _compose(package, rules)
    validate_package_contract(package)
    return package


def validate_package(pir: Mapping[str, Any], manifest: Mapping[str, Any],
                     package: Mapping[str, Any], ruleset_name: str | None = None) -> dict[str, Any]:
    """Inspect without mutation; every issue declares the repair route that owns it."""
    validate_pir_contract(pir)
    validate_manifest_contract(manifest)
    validate_package_contract(package)
    rules = load_ruleset(ruleset_name or str(package.get("ruleset") or ""))
    issues: list[dict[str, Any]] = []
    package_for_hash = copy.deepcopy(dict(package))
    package_for_hash["approval"] = None
    if str(package.get("content_sha256") or "") != content_hash(package_for_hash):
        issues.append({
            "code": "package_content_hash_mismatch", "severity": "blocker",
            "category": "semantic",
            "message": "The compiled package changed after deterministic composition.",
            "repair_action": "recompile_package",
        })
    manifest_approval = manifest.get("approval") or {}
    manifest_for_hash = copy.deepcopy(dict(manifest))
    manifest_for_hash["approval"] = None
    approved_manifest_hash = str(manifest_approval.get("approved_sha256") or "")
    if (approved_manifest_hash != content_hash(manifest_for_hash) or
            package.get("manifest_approval_sha256") != approved_manifest_hash):
        issues.append({
            "code": "manifest_approval_chain_mismatch", "severity": "blocker",
            "category": "semantic",
            "message": "The package is not pinned to the approved figure manifest.",
            "repair_action": "recompile_package",
        })
    if package.get("pir_input_sha256") != pir.get("input_sha256"):
        issues.append({
            "code": "pir_input_chain_mismatch", "severity": "blocker",
            "category": "semantic",
            "message": "The package is not pinned to this canonical model input.",
            "repair_action": "recompile_package",
        })
    registry = {entity["id"]: entity for entity in pir.get("entities") or ()}
    text_refs = {entity["reference"] for entity in registry.values()}
    drawing_refs: list[str] = []
    unit = float(rules["units_per_mm"])
    sheet_width, sheet_height = [float(value) * unit for value in rules["sheet_mm"]]
    usable = {
        "left": float(rules["margins_mm"]["left"]) * unit,
        "top": float(rules["margins_mm"]["top"]) * unit,
        "right": sheet_width - float(rules["margins_mm"]["right"]) * unit,
        "bottom": sheet_height - float(rules["margins_mm"]["bottom"]) * unit,
    }
    for figure in package.get("figures") or ():
        visible_by_id = {str(entity.get("entity_id") or ""): entity
                         for entity in figure.get("entities") or ()}
        for entity in figure.get("entities") or ():
            drawing_refs.append(str(entity.get("reference") or ""))
            canonical = registry.get(entity.get("entity_id"))
            if not canonical or canonical.get("reference") != entity.get("reference"):
                issues.append({
                    "code": "unsupported_visible_entity", "severity": "blocker",
                    "category": "disclosure", "figure_id": figure.get("id"),
                    "entity_id": entity.get("entity_id"),
                    "message": "A visible object is not supported by the approved registry.",
                    "repair_action": "delete_visible_entity",
                })
            elif not entity.get("source_span_ids"):
                issues.append({
                    "code": "missing_entity_provenance", "severity": "blocker",
                    "category": "disclosure", "figure_id": figure.get("id"),
                    "entity_id": entity.get("entity_id"),
                    "message": "A visible object has no source-span provenance.",
                    "repair_action": "reconcile_model",
                })
            if (float(entity.get("x") or 0) < usable["left"] or
                    float(entity.get("y") or 0) < usable["top"] or
                    float(entity.get("x") or 0) + float(entity.get("width") or 0) > usable["right"] or
                    float(entity.get("y") or 0) + float(entity.get("height") or 0) > usable["bottom"]):
                issues.append({
                    "code": "content_outside_usable_area", "severity": "blocker",
                    "category": "formal", "figure_id": figure.get("id"),
                    "entity_id": entity.get("entity_id"),
                    "message": "Visible geometry crosses a required blank margin.",
                    "repair_action": "move_entity",
                })
        label_counts: dict[tuple[str, str], int] = {}
        for label in figure.get("labels") or ():
            entity_id = str(label.get("entity_id") or "")
            reference = str(label.get("reference") or "")
            key = (entity_id, reference)
            label_counts[key] = label_counts.get(key, 0) + 1
            canonical = registry.get(entity_id)
            visible = visible_by_id.get(entity_id)
            if (not canonical or canonical.get("reference") != reference or not visible or
                    visible.get("reference") != reference):
                issues.append({
                    "code": "label_registry_mismatch", "severity": "blocker",
                    "category": "semantic", "figure_id": figure.get("id"),
                    "entity_id": entity_id, "reference": reference,
                    "message": "A reference label is not bound to its visible registry object.",
                    "repair_action": "reconcile_label",
                })
            if (float(label.get("x") or 0) < usable["left"] or
                    float(label.get("x") or 0) > usable["right"] or
                    float(label.get("y") or 0) < usable["top"] or
                    float(label.get("y") or 0) > usable["bottom"]):
                issues.append({
                    "code": "content_outside_usable_area", "severity": "blocker",
                    "category": "formal", "figure_id": figure.get("id"),
                    "reference": label.get("reference"),
                    "message": "A reference label crosses a required blank margin.",
                    "repair_action": "move_label",
                })
        for entity in figure.get("entities") or ():
            entity_id = str(entity.get("entity_id") or "")
            reference = str(entity.get("reference") or "")
            count = label_counts.get((entity_id, reference), 0)
            if count != 1:
                issues.append({
                    "code": "reference_label_count", "severity": "blocker",
                    "category": "semantic", "figure_id": figure.get("id"),
                    "entity_id": entity_id, "reference": reference,
                    "message": (
                        f"Reference {reference} is printed {count} times for this visible object; "
                        "it must appear once in the figure."
                    ),
                    "repair_action": "deduplicate_label" if count > 1 else "add_label",
                })
    drawing_set = set(filter(None, drawing_refs))
    for reference in sorted(text_refs - drawing_set, key=_sort_reference):
        issues.append({
            "code": "text_reference_missing_from_drawings", "severity": "blocker",
            "category": "cross_document", "reference": reference,
            "message": f"Reference {reference} is in the draft but absent from every drawing.",
            "repair_action": "add_supported_entity",
        })
    for reference in sorted(drawing_set - text_refs, key=_sort_reference):
        issues.append({
            "code": "drawing_reference_missing_from_text", "severity": "blocker",
            "category": "cross_document", "reference": reference,
            "message": f"Reference {reference} is visible but absent from the draft registry.",
            "repair_action": "delete_visible_entity",
        })
    for coverage in pir.get("claim_coverage") or ():
        if coverage.get("drawable") and not coverage.get("figure_ids"):
            issues.append({
                "code": "uncovered_drawable_claim", "severity": "blocker",
                "category": "claim_coverage", "limitation_id": coverage.get("limitation_id"),
                "message": "A drawable claim limitation is absent from the figure set.",
                "repair_action": "revise_manifest",
            })
    expected_w, expected_h = sheet_width, sheet_height
    package_sheets = list(package.get("sheets") or ())
    package_figures = list(package.get("figures") or ())
    if len(package_sheets) != len(package_figures):
        issues.append({
            "code": "sheet_figure_count_mismatch", "severity": "blocker", "category": "formal",
            "message": "The number of composed sheets does not match the compiled figures.",
            "repair_action": "recompose_sheet",
        })
    for index, sheet in enumerate(package_sheets, 1):
        svg = str(sheet.get("svg") or "")
        if f'viewBox="0 0 {_svg_number(expected_w)} {_svg_number(expected_h)}"' not in svg:
            issues.append({
                "code": "wrong_sheet_size", "severity": "blocker", "category": "formal",
                "figure_id": sheet.get("figure_id"), "message": "Sheet size does not match ruleset.",
                "repair_action": "recompose_sheet",
            })
        if "<image" in svg or "rgb(" in svg or re.search(r"#[0-9A-Fa-f]{6}", svg.replace(
                "#000000", "").replace("#ffffff", "")):
            issues.append({
                "code": "non_monochrome_or_raster_content", "severity": "blocker",
                "category": "formal", "figure_id": sheet.get("figure_id"),
                "message": "Filing SVG must contain semantic black-and-white vector content only.",
                "repair_action": "rerender_figure",
            })
        figure = next((row for row in package_figures
                       if row.get("id") == sheet.get("figure_id")), None)
        if figure and svg != _render_svg(figure, rules, index, len(package_figures)):
            issues.append({
                "code": "rendered_semantics_mismatch", "severity": "blocker",
                "category": "semantic", "figure_id": sheet.get("figure_id"),
                "message": "Rendered SVG does not match the semantic figure specification.",
                "repair_action": "rerender_figure",
            })
    blockers = sum(1 for issue in issues if issue["severity"] == "blocker")
    validator_counts = {category: sum(1 for issue in issues if issue.get("category") == category)
                        for category in ("formal", "semantic", "disclosure", "cross_document",
                                         "claim_coverage")}
    return {
        "validator_version": "figure-validator-1.0.0", "ruleset": rules["id"],
        "issues": issues, "hard_blockers": blockers, "warnings":
            sum(1 for issue in issues if issue["severity"] == "warning"),
        "validator_counts": validator_counts, "approved_for_export": blockers == 0,
    }


def apply_typed_patch(package: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a narrowly typed edit to a new version; the inspected input stays byte-for-byte."""
    validate_package_contract(package)
    validate_patch_contract(patch)
    if package.get("approval"):
        raise CompilationBlocked("Approved artifacts are immutable; create a new compiler run.")
    kind = str(patch.get("type") or "")
    if kind not in {"move_label", "move_entity", "delete_visible_entity", "reroute_leader"}:
        raise FigureCompilerError("Unknown or unsafe figure patch type.")
    out = copy.deepcopy(dict(package))
    figure_id = str(patch.get("figure_id") or "")
    figure = next((item for item in out.get("figures") or () if item.get("id") == figure_id), None)
    if figure is None:
        raise FigureCompilerError("The requested figure does not exist.")
    reference = _canonical_reference(patch.get("reference"))
    if not reference:
        raise FigureCompilerError("Choose a reference sign to patch.")
    if kind == "move_label":
        target = next((item for item in figure.get("labels") or ()
                       if item.get("reference") == reference), None)
        if target is None:
            raise FigureCompilerError("The requested label does not exist.")
        target["x"], target["y"] = float(patch["x"]), float(patch["y"])
    elif kind == "move_entity":
        target = next((item for item in figure.get("entities") or ()
                       if item.get("reference") == reference), None)
        if target is None:
            raise FigureCompilerError("The requested object does not exist.")
        dx, dy = float(patch["x"]) - target["x"], float(patch["y"]) - target["y"]
        target["x"], target["y"] = float(patch["x"]), float(patch["y"])
        label = next((item for item in figure.get("labels") or ()
                      if item.get("reference") == reference), None)
        if label:
            label["target_x"] += dx
            label["target_y"] += dy
    elif kind == "reroute_leader":
        target = next((item for item in figure.get("labels") or ()
                       if item.get("reference") == reference), None)
        if target is None:
            raise FigureCompilerError("The requested leader does not exist.")
        target["target_x"], target["target_y"] = float(patch["x"]), float(patch["y"])
    else:
        ids = {item["entity_id"] for item in figure.get("entities") or ()
               if item.get("reference") == reference}
        if not ids:
            raise FigureCompilerError("The requested object does not exist.")
        figure["entities"] = [item for item in figure.get("entities") or ()
                              if item["entity_id"] not in ids]
        figure["labels"] = [item for item in figure.get("labels") or ()
                            if item["entity_id"] not in ids]
        figure["relations"] = [item for item in figure.get("relations") or ()
                               if item["from_entity_id"] not in ids and item["to_entity_id"] not in ids]
    out["parent_sha256"] = content_hash(package)
    out["artifact_version"] = int(package.get("artifact_version") or 1) + 1
    out["patch"] = {key: value for key, value in patch.items()
                    if key in {"type", "figure_id", "reference", "x", "y", "reason"}}
    out["approval"] = None
    out.pop("content_sha256", None)
    out = _compose(out, load_ruleset(str(out["ruleset"])))
    validate_package_contract(out)
    return out


def render_pdf(package: Mapping[str, Any], ruleset_name: str | None = None) -> bytes:
    """Render the semantic DSL directly to a deterministic vector PDF."""
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    rules = load_ruleset(ruleset_name or str(package.get("ruleset") or ""))
    page_size = (float(rules["sheet_mm"][0]) * mm, float(rules["sheet_mm"][1]) * mm)
    unit_to_pt = mm / float(rules["units_per_mm"])
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=page_size, pageCompression=1, invariant=1)
    pdf.setTitle("Patent drawing package")
    pdf.setAuthor("Rotem Patent Figure Compiler")
    total = len(package.get("figures") or ())
    font_pt = float(rules["minimum_character_height_mm"]) * mm
    for index, figure in enumerate(package.get("figures") or (), 1):
        pdf.setStrokeColorRGB(0, 0, 0)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.setLineWidth(float(rules["stroke_mm"]) * mm)
        nodes = {node["entity_id"]: node for node in figure.get("entities") or ()}
        def py(value: float) -> float:
            return page_size[1] - float(value) * unit_to_pt
        for relation in figure.get("relations") or ():
            left, right = nodes.get(relation["from_entity_id"]), nodes.get(relation["to_entity_id"])
            if left and right:
                pdf.line((left["x"] + left["width"] / 2) * unit_to_pt,
                         py(left["y"] + left["height"] / 2),
                         (right["x"] + right["width"] / 2) * unit_to_pt,
                         py(right["y"] + right["height"] / 2))
        for node in figure.get("entities") or ():
            x, w, h = node["x"] * unit_to_pt, node["width"] * unit_to_pt, node["height"] * unit_to_pt
            y = py(node["y"] + node["height"])
            if node.get("shape") == "ellipse":
                pdf.ellipse(x, y, x + w, y + h, stroke=1, fill=0)
            else:
                pdf.roundRect(x, y, w, h, 1.8 * mm, stroke=1, fill=0)
        pdf.setLineWidth(float(rules["thin_stroke_mm"]) * mm)
        pdf.setFont("Helvetica", font_pt)
        for label in figure.get("labels") or ():
            pdf.line(label["x"] * unit_to_pt, py(label["y"]),
                     label["target_x"] * unit_to_pt, py(label["target_y"]))
            pdf.drawString(label["x"] * unit_to_pt, py(label["y"]), str(label["reference"]))
        margins = rules["margins_mm"]
        pdf.drawCentredString(page_size[0] / 2, page_size[1] - (float(margins["top"]) + 3.2) * mm,
                              str(rules.get("sheet_number_format") or "{sheet}/{total}").format(
                                  sheet=index, total=total))
        pdf.drawCentredString(page_size[0] / 2, (float(margins["bottom"]) + 3.2) * mm,
                              str(figure["label"]))
        pdf.showPage()
    pdf.save()
    return stream.getvalue()
