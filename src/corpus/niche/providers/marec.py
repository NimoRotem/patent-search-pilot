"""MAREC archive provider using deterministic local archive paths."""
from __future__ import annotations

import gzip
import os
from pathlib import Path

from ..identifiers import normalize_publication_number
from ..models import FetchRequest, ProviderResult
from .base import BaseProvider, quick_completeness


class MarecProvider(BaseProvider):
    name = "marec"

    def __init__(self, root: str | os.PathLike | None = None):
        configured = root if root is not None else os.environ.get("MAREC_ROOT", "")
        self.root = Path(configured).expanduser().resolve() if configured else None

    def enabled(self) -> bool:
        return bool(self.root and self.root.is_dir())

    def disabled_reason(self) -> str:
        return "MAREC_ROOT is not a readable directory" if not self.enabled() else ""

    def _paths(self, request: FetchRequest):
        publication = normalize_publication_number(request.publication_number)
        names = (
            f"{publication}.xml",
            f"{publication}.xml.gz",
            f"{publication}.json",
            f"{publication}.json.gz",
        )
        for directory in (self.root / request.authority.upper(), self.root):
            for name in names:
                yield directory / name

    def fetch(self, request: FetchRequest) -> ProviderResult | None:
        if not self.enabled():
            return None
        path = next((candidate for candidate in self._paths(request) if candidate.is_file()), None)
        if path is None:
            return None
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                content = handle.read()
            logical_suffix = path.with_suffix("").suffix
        else:
            content = path.read_bytes()
            logical_suffix = path.suffix
        media_type = "application/json" if logical_suffix == ".json" else "application/xml"
        return ProviderResult(
            provider=self.name,
            content=content,
            media_type=media_type,
            source_url=path.as_uri(),
            http_status=None,
            credits_used=0,
            completeness=quick_completeness(content),
            metadata={"archive_path": str(path)},
        )
