"""Normalized patent chunks ready for an external embedding stage."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from pathlib import Path

ALLOWED_CHUNK_KINDS = frozenset({
    "abstract", "claim_own", "claim_resolved", "description", "figure_caption"
})
CHUNK_COLUMNS = (
    "chunk_id",
    "publication_id",
    "family_id",
    "chunk_kind",
    "claim_number",
    "language",
    "text",
    "source_location",
    "content_hash",
)
MAX_EMBED_CHUNK_BYTES = 6000


def _segments(text: str, max_bytes: int = MAX_EMBED_CHUNK_BYTES) -> list[str]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return []
    if len(cleaned.encode("utf-8")) <= max_bytes:
        return [cleaned]
    segments = []
    current = []
    current_bytes = 0
    for word in cleaned.split():
        word_bytes = len(word.encode("utf-8"))
        separator = 1 if current else 0
        if current and current_bytes + separator + word_bytes > max_bytes:
            segments.append(" ".join(current))
            current, current_bytes = [], 0
        if word_bytes <= max_bytes:
            current.append(word)
            current_bytes += (1 if current_bytes else 0) + word_bytes
            continue
        piece = ""
        piece_bytes = 0
        for character in word:
            size = len(character.encode("utf-8"))
            if piece and piece_bytes + size > max_bytes:
                segments.append(piece)
                piece, piece_bytes = "", 0
            piece += character
            piece_bytes += size
        if piece:
            current, current_bytes = [piece], piece_bytes
    if current:
        segments.append(" ".join(current))
    return segments


def _append_segments(
    chunks: list[dict],
    parsed: dict,
    kind: str,
    text: str,
    source_location: str,
    *,
    claim_number=None,
    language: str = "",
) -> None:
    segments = _segments(text)
    for index, segment in enumerate(segments, 1):
        location = (
            f"{source_location};part:{index}" if len(segments) > 1 else source_location
        )
        chunks.append(_chunk(
            parsed,
            kind,
            segment,
            location,
            claim_number=claim_number,
            language=language,
        ))


def _chunk(
    parsed: dict,
    kind: str,
    text: str,
    source_location: str,
    *,
    claim_number=None,
    language: str = "",
) -> dict:
    if kind not in ALLOWED_CHUNK_KINDS:
        raise ValueError(f"unsupported chunk kind: {kind}")
    cleaned = str(text or "").strip()
    content_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    publication_id = str(parsed.get("publication_id") or parsed.get("publication_number") or "")
    identity = "\x1f".join((publication_id, kind, str(source_location), content_hash))
    chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "chunk_id": chunk_id,
        "publication_id": publication_id,
        "family_id": str(parsed.get("family_id") or publication_id),
        "chunk_kind": kind,
        "claim_number": claim_number,
        "language": str(language or parsed.get("language") or ""),
        "text": cleaned,
        "source_location": source_location,
        "content_hash": content_hash,
    }


def build_chunks(parsed: dict) -> list[dict]:
    chunks = []
    abstract = str(parsed.get("abstract") or "").strip()
    if abstract:
        _append_segments(chunks, parsed, "abstract", abstract, "abstract")
    for claim in parsed.get("claims") or []:
        number = claim.get("number")
        language = claim.get("language") or parsed.get("language") or ""
        own = str(claim.get("raw_text") or claim.get("text") or "").strip()
        if own:
            _append_segments(
                chunks, parsed, "claim_own", own, f"claim:{number}",
                claim_number=number, language=language,
            )
        resolved = str(claim.get("resolved_text") or "").strip()
        if resolved and claim.get("chain_complete"):
            _append_segments(
                chunks, parsed, "claim_resolved", resolved, f"claim:{number};resolved",
                claim_number=number, language=language,
            )
    for paragraph in parsed.get("description_paragraphs") or []:
        text = str(paragraph.get("text") or "").strip()
        if text:
            _append_segments(
                chunks, parsed,
                "description",
                text,
                str(paragraph.get("source_location") or f"paragraph:{paragraph.get('id', '')}"),
                language=str(paragraph.get("language") or parsed.get("language") or ""),
            )
    for index, caption in enumerate(parsed.get("figure_captions") or [], 1):
        if isinstance(caption, dict):
            text = str(caption.get("text") or caption.get("caption") or "")
            location = str(caption.get("source_location") or f"figure:{index}")
            language = str(caption.get("language") or parsed.get("language") or "")
        else:
            text, location, language = str(caption), f"figure:{index}", str(parsed.get("language") or "")
        if text.strip():
            _append_segments(
                chunks, parsed, "figure_caption", text, location, language=language
            )
    return chunks


def write_chunks_jsonl(chunks: Iterable[dict], path: str | os.PathLike) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    with temporary.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return destination


def chunks_arrow_table(chunks: Iterable[dict]):
    import pyarrow as pa

    schema = pa.schema([
        ("chunk_id", pa.string()),
        ("publication_id", pa.string()),
        ("family_id", pa.string()),
        ("chunk_kind", pa.string()),
        ("claim_number", pa.int64()),
        ("language", pa.string()),
        ("text", pa.string()),
        ("source_location", pa.string()),
        ("content_hash", pa.string()),
    ])
    return pa.Table.from_pylist(list(chunks), schema=schema)


def write_chunks_parquet(chunks: Iterable[dict], path: str | os.PathLike) -> Path:
    import pyarrow.parquet as pq

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    table = chunks_arrow_table(chunks)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    pq.write_table(table, temporary, compression="zstd")
    os.replace(temporary, destination)
    return destination
