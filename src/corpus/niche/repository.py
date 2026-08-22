"""PostgreSQL persistence confined to the niche_corpus staging schema."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import fields

from .manifest import PublicationRecord, choose_family_fetch_targets
from .waterfall import FetchAttempt

_RECORD_FIELDS = {field.name for field in fields(PublicationRecord)}


def _date(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] if len(text) >= 10 else None


class PostgresNicheRepository:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    def upsert_publications(self, records: Iterable[PublicationRecord]) -> None:
        sql = """
        INSERT INTO niche_corpus.niche_publications (
            publication_id, publication_number, family_id, authority, kind_code,
            title, abstract, language, cpc_codes, ipc_codes,
            publication_date, filing_date, earliest_priority_date,
            has_title, has_abstract, has_claims, has_complete_claims,
            has_description, has_complete_description, has_figures, has_citations,
            preferred_source, raw_object_uri, parsed_object_uri,
            fetch_status, fetch_attempts, last_provider, last_error,
            priority, discovery_signals
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (publication_id) DO UPDATE SET
            publication_number = EXCLUDED.publication_number,
            family_id = COALESCE(NULLIF(niche_corpus.niche_publications.family_id, ''), EXCLUDED.family_id),
            authority = COALESCE(NULLIF(niche_corpus.niche_publications.authority, ''), EXCLUDED.authority),
            kind_code = COALESCE(NULLIF(niche_corpus.niche_publications.kind_code, ''), EXCLUDED.kind_code),
            title = CASE WHEN length(COALESCE(EXCLUDED.title, '')) >
                                   length(COALESCE(niche_corpus.niche_publications.title, ''))
                         THEN EXCLUDED.title ELSE niche_corpus.niche_publications.title END,
            abstract = CASE WHEN length(COALESCE(EXCLUDED.abstract, '')) >
                                      length(COALESCE(niche_corpus.niche_publications.abstract, ''))
                            THEN EXCLUDED.abstract ELSE niche_corpus.niche_publications.abstract END,
            language = CASE WHEN EXCLUDED.language = 'en' THEN EXCLUDED.language
                            ELSE COALESCE(NULLIF(niche_corpus.niche_publications.language, ''), EXCLUDED.language) END,
            cpc_codes = ARRAY(SELECT DISTINCT value FROM unnest(
                niche_corpus.niche_publications.cpc_codes || EXCLUDED.cpc_codes
            ) AS value ORDER BY value),
            ipc_codes = ARRAY(SELECT DISTINCT value FROM unnest(
                niche_corpus.niche_publications.ipc_codes || EXCLUDED.ipc_codes
            ) AS value ORDER BY value),
            publication_date = COALESCE(niche_corpus.niche_publications.publication_date, EXCLUDED.publication_date),
            filing_date = COALESCE(niche_corpus.niche_publications.filing_date, EXCLUDED.filing_date),
            earliest_priority_date = COALESCE(
                niche_corpus.niche_publications.earliest_priority_date, EXCLUDED.earliest_priority_date
            ),
            has_title = niche_corpus.niche_publications.has_title OR EXCLUDED.has_title,
            has_abstract = niche_corpus.niche_publications.has_abstract OR EXCLUDED.has_abstract,
            has_claims = niche_corpus.niche_publications.has_claims OR EXCLUDED.has_claims,
            has_complete_claims = niche_corpus.niche_publications.has_complete_claims OR EXCLUDED.has_complete_claims,
            has_description = niche_corpus.niche_publications.has_description OR EXCLUDED.has_description,
            has_complete_description = niche_corpus.niche_publications.has_complete_description OR EXCLUDED.has_complete_description,
            has_figures = niche_corpus.niche_publications.has_figures OR EXCLUDED.has_figures,
            has_citations = niche_corpus.niche_publications.has_citations OR EXCLUDED.has_citations,
            preferred_source = COALESCE(NULLIF(EXCLUDED.preferred_source, ''), niche_corpus.niche_publications.preferred_source),
            raw_object_uri = COALESCE(NULLIF(EXCLUDED.raw_object_uri, ''), niche_corpus.niche_publications.raw_object_uri),
            parsed_object_uri = COALESCE(NULLIF(EXCLUDED.parsed_object_uri, ''), niche_corpus.niche_publications.parsed_object_uri),
            fetch_status = CASE
                WHEN 'completed' IN (niche_corpus.niche_publications.fetch_status, EXCLUDED.fetch_status)
                THEN 'completed'
                WHEN EXCLUDED.fetch_status <> 'pending' THEN EXCLUDED.fetch_status
                ELSE niche_corpus.niche_publications.fetch_status END,
            fetch_attempts = GREATEST(niche_corpus.niche_publications.fetch_attempts, EXCLUDED.fetch_attempts),
            last_provider = COALESCE(NULLIF(EXCLUDED.last_provider, ''), niche_corpus.niche_publications.last_provider),
            last_error = COALESCE(NULLIF(EXCLUDED.last_error, ''), niche_corpus.niche_publications.last_error),
            priority = LEAST(niche_corpus.niche_publications.priority, EXCLUDED.priority),
            discovery_signals = ARRAY(SELECT DISTINCT value FROM unnest(
                niche_corpus.niche_publications.discovery_signals || EXCLUDED.discovery_signals
            ) AS value ORDER BY value),
            updated_at = now()
        """
        values = []
        for record in records:
            values.append((
                record.publication_id, record.publication_number, record.family_id or None,
                record.authority or None, record.kind_code or None, record.title or None,
                record.abstract or None, record.language or None, list(record.cpc_codes),
                list(record.ipc_codes), _date(record.publication_date), _date(record.filing_date),
                _date(record.earliest_priority_date), record.has_title, record.has_abstract,
                record.has_claims, record.has_complete_claims, record.has_description,
                record.has_complete_description, record.has_figures, record.has_citations,
                record.preferred_source or None, record.raw_object_uri or None,
                record.parsed_object_uri or None, record.fetch_status, record.fetch_attempts,
                record.last_provider or None, record.last_error or None, record.priority,
                list(record.discovery_signals),
            ))
        if not values:
            return
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.executemany(sql, values)

    def load_watermarks(self, source: str = "local") -> dict[str, int]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT scope_key, last_value FROM niche_corpus.niche_discovery_watermarks "
                "WHERE source = %s",
                (source,),
            )
            return {str(row["scope_key"]): int(row["last_value"]) for row in cursor.fetchall()}

    def save_watermarks(self, watermarks: Mapping[str, int], source: str = "local") -> None:
        sql = """
        INSERT INTO niche_corpus.niche_discovery_watermarks (source, scope_key, last_value)
        VALUES (%s, %s, %s)
        ON CONFLICT (source, scope_key) DO UPDATE
           SET last_value = GREATEST(niche_corpus.niche_discovery_watermarks.last_value,
                                     EXCLUDED.last_value),
               updated_at = now()
        """
        values = [(source, str(key), int(value)) for key, value in watermarks.items()]
        if not values:
            return
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.executemany(sql, values)

    def get_publication(self, publication_id: str) -> PublicationRecord | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM niche_corpus.niche_publications WHERE publication_id = %s",
                (publication_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        values = {key: value for key, value in dict(row).items() if key in _RECORD_FIELDS}
        values["cpc_codes"] = tuple(values.get("cpc_codes") or ())
        values["ipc_codes"] = tuple(values.get("ipc_codes") or ())
        values["discovery_signals"] = tuple(values.get("discovery_signals") or ())
        for key in ("publication_date", "filing_date", "earliest_priority_date", "updated_at"):
            if values.get(key) is not None:
                values[key] = str(values[key])
        return PublicationRecord(**values)

    def cached_sources(self, publication_id: str) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT provider, media_type, raw_object_uri, metadata_object_uri, "
                "content_hash, source_url FROM niche_corpus.niche_source_objects "
                "WHERE publication_id = %s ORDER BY fetched_at DESC, source_object_id DESC",
                (publication_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def record_source(self, publication_id, result, stored) -> None:
        sql = """
        INSERT INTO niche_corpus.niche_source_objects (
            publication_id, provider, content_hash, media_type, source_url,
            raw_object_uri, metadata_object_uri, http_status, size_bytes
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (publication_id, provider, content_hash) DO NOTHING
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(sql, (
                publication_id, result.provider, stored.content_hash, result.media_type,
                result.source_url, stored.uri, stored.metadata_uri or None,
                result.http_status, stored.size_bytes,
            ))
            cursor.execute(
                "UPDATE niche_corpus.niche_publications SET raw_object_uri = %s, "
                "preferred_source = %s, last_provider = %s, updated_at = now() "
                "WHERE publication_id = %s",
                (stored.uri, result.provider, result.provider, publication_id),
            )

    def record_attempt(self, publication_id: str, attempt: FetchAttempt) -> None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                    "INSERT INTO niche_corpus.niche_fetch_attempts "
                    "(publication_id, provider, status, http_status, latency_ms, credits_used, "
                    "bytes_received, error_class, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        publication_id, attempt.provider, attempt.status, attempt.http_status,
                        attempt.latency_ms, attempt.credits_used, attempt.bytes_received,
                        attempt.error_class or None,
                        attempt.error or None,
                    ),
            )
            cursor.execute(
                    "UPDATE niche_corpus.niche_publications SET fetch_attempts = fetch_attempts + 1, "
                    "last_provider = %s, last_error = %s, updated_at = now() "
                    "WHERE publication_id = %s",
                    (attempt.provider, attempt.error or None, publication_id),
            )

    def save_parsed(self, publication_id, parsed, parsed_object, chunks_object) -> None:
        completeness = parsed.get("completeness") or {}
        dates = parsed.get("dates") or {}
        sql = """
        UPDATE niche_corpus.niche_publications
           SET family_id = COALESCE(NULLIF(%s, ''), family_id),
               title = CASE WHEN length(COALESCE(%s, '')) > length(COALESCE(title, ''))
                            THEN %s ELSE title END,
               abstract = CASE WHEN length(COALESCE(%s, '')) > length(COALESCE(abstract, ''))
                               THEN %s ELSE abstract END,
               language = COALESCE(NULLIF(%s, ''), language),
               cpc_codes = ARRAY(SELECT DISTINCT value FROM unnest(cpc_codes || %s) value ORDER BY value),
               ipc_codes = ARRAY(SELECT DISTINCT value FROM unnest(ipc_codes || %s) value ORDER BY value),
               publication_date = COALESCE(publication_date, %s),
               filing_date = COALESCE(filing_date, %s),
               earliest_priority_date = COALESCE(earliest_priority_date, %s),
               has_title = has_title OR %s,
               has_abstract = has_abstract OR %s,
               has_claims = has_claims OR %s,
               has_complete_claims = has_complete_claims OR %s,
               has_description = has_description OR %s,
               has_complete_description = has_complete_description OR %s,
               has_figures = has_figures OR %s,
               has_citations = has_citations OR %s,
               parsed_object_uri = %s,
               chunk_object_uri = %s,
               updated_at = now()
         WHERE publication_id = %s
        """
        title, abstract = parsed.get("title") or "", parsed.get("abstract") or ""
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(sql, (
                    parsed.get("family_id") or "", title, title, abstract, abstract,
                    parsed.get("language") or "", list(parsed.get("cpc") or []),
                    list(parsed.get("ipc") or []), _date(dates.get("publication_date")),
                    _date(dates.get("filing_date")), _date(dates.get("earliest_priority_date")),
                    bool(completeness.get("has_title")), bool(completeness.get("has_abstract")),
                    bool(completeness.get("has_claims")), bool(completeness.get("has_complete_claims")),
                    bool(completeness.get("has_description")),
                    bool(completeness.get("has_complete_description")),
                    bool(completeness.get("has_figures")), bool(completeness.get("has_citations")),
                    parsed_object.uri, chunks_object.uri,
                    publication_id,
            ))

    def mark_fetch_status(self, publication_id: str, status: str, **fields) -> None:
        if status not in {"pending", "leased", "partial", "completed", "failed"}:
            raise ValueError(f"invalid fetch status: {status}")
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                    "UPDATE niche_corpus.niche_publications SET fetch_status = CASE "
                    "WHEN fetch_status = 'completed' THEN 'completed' ELSE %s END, "
                    "last_provider = COALESCE(%s, last_provider), last_error = %s, "
                    "updated_at = now() WHERE publication_id = %s",
                    (
                        status, fields.get("last_provider"), fields.get("last_error"),
                        publication_id,
                    ),
            )

    def iter_publications(self, page_size: int = 1000):
        after = ""
        while True:
            with self.connection_factory() as connection, connection.cursor() as cursor:
                cursor.execute(
                        "SELECT * FROM niche_corpus.niche_publications "
                        "WHERE publication_id > %s ORDER BY publication_id LIMIT %s",
                        (after, max(1, int(page_size))),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            if not rows:
                break
            yield from rows
            after = str(rows[-1]["publication_id"])

    def iter_attempts(self, page_size: int = 2000):
        after = 0
        while True:
            with self.connection_factory() as connection, connection.cursor() as cursor:
                cursor.execute(
                        "SELECT * FROM niche_corpus.niche_fetch_attempts "
                        "WHERE attempt_id > %s ORDER BY attempt_id LIMIT %s",
                        (after, max(1, int(page_size))),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            if not rows:
                break
            yield from rows
            after = int(rows[-1]["attempt_id"])

    def iter_jobs(self, page_size: int = 2000):
        after = 0
        while True:
            with self.connection_factory() as connection, connection.cursor() as cursor:
                cursor.execute(
                        "SELECT * FROM niche_corpus.corpus_fetch_jobs "
                        "WHERE job_id > %s ORDER BY job_id LIMIT %s",
                        (after, max(1, int(page_size))),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            if not rows:
                break
            yield from rows
            after = int(rows[-1]["job_id"])

    def enqueue_incomplete_families(self, queue) -> int:
        records = []
        for row in self.iter_publications():
            values = {key: value for key, value in row.items() if key in _RECORD_FIELDS}
            values["cpc_codes"] = tuple(values.get("cpc_codes") or ())
            values["ipc_codes"] = tuple(values.get("ipc_codes") or ())
            values["discovery_signals"] = tuple(values.get("discovery_signals") or ())
            records.append(PublicationRecord(**values))
        count = 0
        for record in choose_family_fetch_targets(records):
            if (
                record.has_complete_claims
                and record.has_complete_description
                and record.parsed_object_uri
            ):
                continue
            queue.enqueue(record.publication_id, record.priority)
            count += 1
        return count
