"""Incremental, restart-safe Tantivy BM25 index construction for niche_full_v1."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import threading
import uuid
from pathlib import Path

from .database import connection_factory, require_dsn, validate_staging_database


class TantivyRepository:
    NEXT_SQL = """
        SELECT chunk_id, corpus_release, publication_id, family_id, chunk_kind,
               claim_number, language, text, source_location, content_hash
          FROM niche_corpus.niche_vector_documents
         WHERE active
           AND tantivy_index_generation IS DISTINCT FROM %s
         ORDER BY chunk_id, corpus_release
         LIMIT %s
    """

    def __init__(self, factory, generation: str):
        self.factory = factory
        self.generation = str(generation)

    def next_batch(self, limit: int) -> list[dict]:
        with self.factory() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            with connection.cursor() as cursor:
                cursor.execute(
                    self.NEXT_SQL,
                    (self.generation, min(50_000, max(1, int(limit)))),
                )
                return [dict(row) for row in cursor.fetchall()]

    def next_deletions(self, limit: int) -> list[str]:
        with self.factory() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT document_key FROM niche_corpus.niche_tantivy_deletions "
                    "ORDER BY created_at,document_key LIMIT %s",
                    (min(50_000, max(1, int(limit))),),
                )
                return [str(row["document_key"]) for row in cursor.fetchall()]

    def complete_deletions(self, document_keys) -> None:
        values = [(str(value),) for value in document_keys]
        if not values:
            return
        with self.factory() as connection, connection.cursor() as cursor:
            cursor.executemany(
                "DELETE FROM niche_corpus.niche_tantivy_deletions WHERE document_key=%s",
                values,
            )

    def mark_indexed(self, rows) -> None:
        values = [
            (
                self.generation,
                str(row["chunk_id"]),
                str(row["corpus_release"]),
                str(row["content_hash"]),
            )
            for row in rows
        ]
        if not values:
            return
        with self.factory() as connection, connection.cursor() as cursor:
            cursor.executemany(
                "UPDATE niche_corpus.niche_vector_documents "
                "SET tantivy_index_generation=%s, tantivy_indexed_at=now(), updated_at=now() "
                "WHERE chunk_id=%s AND corpus_release=%s AND content_hash=%s AND active",
                values,
            )

    def current_mark_count(self) -> int:
        with self.factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) AS n FROM niche_corpus.niche_vector_documents "
                "WHERE active AND tantivy_index_generation=%s",
                (self.generation,),
            )
            return int((cursor.fetchone() or {}).get("n") or 0)

    def status(self) -> dict:
        with self.factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE tantivy_index_generation IS DISTINCT FROM %s) "
                "AS pending, count(*) FILTER (WHERE tantivy_index_generation=%s) AS indexed "
                "FROM niche_corpus.niche_vector_documents WHERE active",
                (self.generation, self.generation),
            )
            result = dict(cursor.fetchone() or {})
            cursor.execute(
                "SELECT count(*) AS n FROM niche_corpus.niche_tantivy_deletions"
            )
            result["deletions"] = int((cursor.fetchone() or {}).get("n") or 0)
            result["generation"] = self.generation
            return result


def build_schema(tantivy_module):
    builder = tantivy_module.SchemaBuilder()
    for field in (
        "document_key",
        "chunk_id",
        "corpus_release",
        "publication_id",
        "family_id",
        "chunk_kind",
        "language",
        "source_location",
        "content_hash",
    ):
        builder.add_text_field(
            field, stored=True, tokenizer_name="raw", index_option="basic"
        )
    builder.add_integer_field("claim_number", stored=True, indexed=True)
    builder.add_text_field(
        "text", stored=True, tokenizer_name="default", index_option="position"
    )
    return builder.build()


def open_index(path: str | os.PathLike, tantivy_module=None):
    if tantivy_module is None:
        import tantivy as tantivy_module
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    if tantivy_module.Index.exists(str(target)):
        return tantivy_module.Index.open(str(target))
    return tantivy_module.Index(build_schema(tantivy_module), path=str(target))


def ensure_index_generation(path: str | os.PathLike, *, rotate: bool = False) -> str:
    """Bind database completion marks to one exact persistent index directory."""
    target = Path(path).resolve()
    target.mkdir(parents=True, exist_ok=True)
    marker = target / ".niche-index-generation"
    path_fingerprint = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:16]
    if marker.exists() and not rotate:
        current = marker.read_text(encoding="utf-8").strip()
        if current.startswith(f"{path_fingerprint}:"):
            return current
    generation = f"{path_fingerprint}:{uuid.uuid4().hex}"
    temporary = target / f".niche-index-generation.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(generation + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, marker)
    return generation


class TantivyIndexer:
    """Delete then add by a raw document key, making a post-commit retry idempotent."""

    def __init__(
        self,
        repository,
        index,
        tantivy_module,
        *,
        heap_size: int = 2_000_000_000,
        threads: int = 4,
    ):
        self.repository = repository
        self.index = index
        self.tantivy = tantivy_module
        self.heap_size = max(50_000_000, int(heap_size))
        self.threads = max(1, int(threads))

    @staticmethod
    def document_key(row: dict) -> str:
        return f"{row['corpus_release']}\x1f{row['chunk_id']}"

    def index_batch(self, limit: int = 10_000) -> int:
        deletion_reader = getattr(self.repository, "next_deletions", lambda _limit: [])
        deletions = deletion_reader(limit)
        rows = self.repository.next_batch(limit)
        if not rows and not deletions:
            return 0
        writer = self.index.writer(heap_size=self.heap_size, num_threads=self.threads)
        try:
            for key in deletions:
                writer.delete_documents_by_term("document_key", str(key))
            for row in rows:
                key = self.document_key(row)
                writer.delete_documents_by_term("document_key", key)
                values = {
                    "document_key": key,
                    "chunk_id": str(row["chunk_id"]),
                    "corpus_release": str(row["corpus_release"]),
                    "publication_id": str(row["publication_id"]),
                    "family_id": str(row.get("family_id") or ""),
                    "chunk_kind": str(row["chunk_kind"]),
                    "language": str(row.get("language") or ""),
                    "text": str(row["text"]),
                    "source_location": str(row["source_location"]),
                    "content_hash": str(row["content_hash"]),
                }
                if row.get("claim_number") is not None:
                    values["claim_number"] = int(row["claim_number"])
                writer.add_document(self.tantivy.Document(**values))
            writer.commit()
        except Exception:
            writer.rollback()
            raise
        self.index.reload()
        deletion_writer = getattr(
            self.repository, "complete_deletions", lambda _document_keys: None
        )
        deletion_writer(deletions)
        self.repository.mark_indexed(rows)
        return len(rows) + len(deletions)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.corpus.niche.tantivy_build",
        description="Continuously build the isolated niche Tantivy BM25 index.",
    )
    parser.add_argument(
        "--niche-dsn", default=os.environ.get("NICHE_DATABASE_URL", "")
    )
    parser.add_argument(
        "--index-dir", default=os.environ.get("NICHE_TANTIVY_INDEX_DIR", "")
    )
    parser.add_argument("--batch-size", type=int, default=10_000)
    parser.add_argument("--heap-bytes", type=int, default=2_000_000_000)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    index_dir = str(args.index_dir or "").strip()
    if not index_dir or not Path(index_dir).is_absolute():
        raise RuntimeError("NICHE_TANTIVY_INDEX_DIR must be an explicit absolute path")
    expected = str(os.environ.get("NICHE_EXPECTED_DATABASE") or "").strip()
    fingerprint = str(os.environ.get("NICHE_DATABASE_FINGERPRINT") or "").strip()
    factory = connection_factory(
        require_dsn(args.niche_dsn, "NICHE_DATABASE_URL"),
        application_name="niche-tantivy-build",
    )
    validate_staging_database(factory, expected, fingerprint)
    import tantivy

    index = open_index(index_dir, tantivy)
    generation = ensure_index_generation(index_dir)
    repository = TantivyRepository(factory, generation)
    index.reload()
    if index.searcher().num_docs == 0 and repository.current_mark_count():
        generation = ensure_index_generation(index_dir, rotate=True)
        repository = TantivyRepository(factory, generation)
    if args.status:
        index.reload()
        print(json.dumps({
            **repository.status(),
            "documents": index.searcher().num_docs,
            "segments": index.searcher().num_segments,
            "index_dir": index_dir,
        }, sort_keys=True, default=str))
        return 0
    indexer = TantivyIndexer(
        repository,
        index,
        tantivy,
        heap_size=args.heap_bytes,
        threads=args.threads,
    )
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    total = 0
    while not stop.is_set():
        count = indexer.index_batch(args.batch_size)
        total += count
        print(json.dumps({"indexed": count, "total": total}), flush=True)
        if args.once:
            break
        if count == 0:
            stop.wait(max(1.0, float(args.poll_seconds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
