"""Permanent, content-addressed raw and parsed object storage."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .identifiers import normalize_publication_number

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_HEADER_ALLOWLIST = frozenset({
    "content-length", "content-type", "etag", "last-modified",
    "spb-cost", "spb-auto-cost", "spb-request-id", "spb-resolved-url",
})
_SENSITIVE_QUERY_PARTS = (
    "api_key", "apikey", "authorization", "credential", "password",
    "secret", "signature", "token", "x-goog-signature",
)


@dataclass(frozen=True)
class StoredObject:
    uri: str
    metadata_uri: str
    content_hash: str
    size_bytes: int


def _safe(value: str) -> str:
    return _SAFE.sub("_", str(value or "unknown"))


def _extension(media_type: str) -> str:
    explicit = {
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/html": ".html",
        "application/pdf": ".pdf",
        "application/json": ".json",
    }
    return explicit.get((media_type or "").split(";", 1)[0].lower()) \
        or mimetypes.guess_extension(media_type or "") or ".bin"


def sanitize_http_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() in _HEADER_ALLOWLIST
    }


def sanitize_source_url(source_url: str) -> str:
    parsed = urlsplit(str(source_url or ""))
    if not parsed.scheme:
        return str(source_url or "")
    safe_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(part in key.lower() for part in _SENSITIVE_QUERY_PARTS)
    ]
    return urlunsplit((
        parsed.scheme,
        parsed.netloc.rsplit("@", 1)[-1],
        parsed.path,
        urlencode(safe_query),
        "",
    ))


class FileObjectStore:
    """Durable filesystem store with the same key layout used by bucket stores."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()

    def _write_once(self, relative: Path, content: bytes) -> None:
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    def put_raw(
        self,
        *,
        authority: str,
        publication_number: str,
        provider: str,
        content: bytes,
        media_type: str,
        http_status: int | None,
        headers: Mapping[str, str],
        fetched_at: datetime | None = None,
        source_url: str = "",
    ) -> StoredObject:
        raw = bytes(content)
        digest = hashlib.sha256(raw).hexdigest()
        publication = normalize_publication_number(publication_number)
        base = Path("patents") / "raw" / _safe(authority.upper()) / _safe(publication) / _safe(provider)
        object_path = base / f"{digest}{_extension(media_type)}"
        metadata_path = base / f"{digest}.metadata.json"
        timestamp = (fetched_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        metadata = {
            "publication_number": publication,
            "authority": authority.upper(),
            "provider": provider,
            "fetched_at": timestamp.isoformat().replace("+00:00", "Z"),
            "content_hash": digest,
            "media_type": media_type,
            "http_status": http_status,
            "http_headers": sanitize_http_headers(headers),
            "source_url": sanitize_source_url(source_url),
            "size_bytes": len(raw),
        }
        self._write_once(object_path, raw)
        self._write_once(
            metadata_path,
            json.dumps(metadata, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return StoredObject(str(object_path), str(metadata_path), digest, len(raw))

    def put_parsed(self, authority: str, publication_number: str, parsed: dict) -> StoredObject:
        publication = normalize_publication_number(publication_number)
        raw = json.dumps(parsed, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        object_path = Path("patents") / "parsed" / _safe(authority.upper()) / f"{_safe(publication)}.json"
        self._write_atomic(object_path, raw)
        return StoredObject(str(object_path), "", digest, len(raw))

    def put_chunks(self, authority: str, publication_number: str, chunks, fmt: str = "jsonl") -> StoredObject:
        from .chunks import write_chunks_jsonl, write_chunks_parquet

        publication = normalize_publication_number(publication_number)
        suffix = ".parquet" if fmt == "parquet" else ".jsonl"
        object_path = Path("patents") / "chunks" / _safe(authority.upper()) / f"{_safe(publication)}{suffix}"
        destination = self.root / object_path
        rows = list(chunks)
        if fmt == "parquet":
            write_chunks_parquet(rows, destination)
        elif fmt == "jsonl":
            write_chunks_jsonl(rows, destination)
        else:
            raise ValueError("chunk format must be jsonl or parquet")
        raw = destination.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        return StoredObject(str(object_path), "", digest, len(raw))

    def _write_atomic(self, relative: Path, content: bytes) -> None:
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)

    def read(self, uri: str) -> bytes:
        path = (self.root / uri).resolve()
        path.relative_to(self.root)
        return path.read_bytes()


class GCSObjectStore:
    """Shared Google Cloud Storage implementation for multi-VM workers."""

    def __init__(self, root_uri: str, client=None):
        parsed = urlsplit(str(root_uri))
        if parsed.scheme != "gs" or not parsed.netloc:
            raise ValueError("GCS object root must look like gs://bucket/optional-prefix")
        self.bucket_name = parsed.netloc
        self.prefix = parsed.path.strip("/")
        if client is None:
            from google.cloud import storage
            client = storage.Client()
        self.client = client
        self.bucket = client.bucket(self.bucket_name)

    def _name(self, relative: Path | str) -> str:
        value = str(relative).lstrip("/")
        return f"{self.prefix}/{value}" if self.prefix else value

    def _uri(self, name: str) -> str:
        return f"gs://{self.bucket_name}/{name}"

    @staticmethod
    def _is_precondition_failure(exc: Exception) -> bool:
        return type(exc).__name__ in {"PreconditionFailed", "Conflict"} \
            or "precondition" in str(exc).lower()

    def _put_once(self, name: str, content: bytes, content_type: str) -> None:
        blob = self.bucket.blob(name)
        try:
            blob.upload_from_string(
                content, content_type=content_type, if_generation_match=0
            )
        except Exception as exc:
            if not self._is_precondition_failure(exc):
                raise

    def _put_replace(self, name: str, content: bytes, content_type: str) -> None:
        self.bucket.blob(name).upload_from_string(content, content_type=content_type)

    def put_raw(
        self,
        *,
        authority: str,
        publication_number: str,
        provider: str,
        content: bytes,
        media_type: str,
        http_status: int | None,
        headers: Mapping[str, str],
        fetched_at: datetime | None = None,
        source_url: str = "",
    ) -> StoredObject:
        raw = bytes(content)
        digest = hashlib.sha256(raw).hexdigest()
        publication = normalize_publication_number(publication_number)
        relative = Path("patents") / "raw" / _safe(authority.upper()) / _safe(publication) / _safe(provider)
        object_name = self._name(relative / f"{digest}{_extension(media_type)}")
        metadata_name = self._name(relative / f"{digest}.metadata.json")
        timestamp = (fetched_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        metadata = {
            "publication_number": publication,
            "authority": authority.upper(),
            "provider": provider,
            "fetched_at": timestamp.isoformat().replace("+00:00", "Z"),
            "content_hash": digest,
            "media_type": media_type,
            "http_status": http_status,
            "http_headers": sanitize_http_headers(headers),
            "source_url": sanitize_source_url(source_url),
            "size_bytes": len(raw),
        }
        self._put_once(object_name, raw, media_type or "application/octet-stream")
        self._put_once(
            metadata_name,
            json.dumps(metadata, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json",
        )
        return StoredObject(
            self._uri(object_name), self._uri(metadata_name), digest, len(raw)
        )

    def put_parsed(self, authority: str, publication_number: str, parsed: dict) -> StoredObject:
        publication = normalize_publication_number(publication_number)
        raw = json.dumps(parsed, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        name = self._name(
            Path("patents") / "parsed" / _safe(authority.upper()) / f"{_safe(publication)}.json"
        )
        self._put_replace(name, raw, "application/json")
        return StoredObject(self._uri(name), "", digest, len(raw))

    def put_chunks(self, authority: str, publication_number: str, chunks, fmt: str = "jsonl") -> StoredObject:
        rows = list(chunks)
        publication = normalize_publication_number(publication_number)
        if fmt == "jsonl":
            raw = b"".join(
                (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
                for row in rows
            )
            media_type, suffix = "application/x-ndjson", ".jsonl"
        elif fmt == "parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq
            from .chunks import chunks_arrow_table

            sink = pa.BufferOutputStream()
            pq.write_table(chunks_arrow_table(rows), sink, compression="zstd")
            raw = sink.getvalue().to_pybytes()
            media_type, suffix = "application/vnd.apache.parquet", ".parquet"
        else:
            raise ValueError("chunk format must be jsonl or parquet")
        name = self._name(
            Path("patents") / "chunks" / _safe(authority.upper()) / f"{_safe(publication)}{suffix}"
        )
        self._put_replace(name, raw, media_type)
        return StoredObject(self._uri(name), "", hashlib.sha256(raw).hexdigest(), len(raw))

    def read(self, uri: str) -> bytes:
        parsed = urlsplit(str(uri))
        if parsed.scheme != "gs" or parsed.netloc != self.bucket_name:
            raise ValueError("object URI is outside the configured GCS bucket")
        name = parsed.path.lstrip("/")
        if self.prefix and not name.startswith(f"{self.prefix}/"):
            raise ValueError("object URI is outside the configured GCS prefix")
        return self.bucket.blob(name).download_as_bytes()


def build_object_store(location: str | os.PathLike):
    value = str(location)
    return GCSObjectStore(value) if value.startswith("gs://") else FileObjectStore(value)
