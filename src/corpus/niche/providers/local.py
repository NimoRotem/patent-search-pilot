"""Read-only access to patent text already owned by the active corpus.

This provider never creates tables and never writes back to the active corpus. A
separate niche manifest records the audit result.
"""
from __future__ import annotations

import json
import os
import re
import zlib
from collections.abc import Callable
from dataclasses import replace

from ..domains import all_cpc_prefixes, all_ipc_prefixes, in_niche, priority_for_record
from ..identifiers import normalize_publication_number, source_publication_variants
from ..manifest import PublicationRecord, merge_publications
from ..models import Completeness, FetchRequest, ProviderResult
from ..parse import item_text
from .base import BaseProvider

_MIN_CLAIMS_CHARS = max(1, int(os.environ.get("NICHE_MIN_CLAIMS_CHARS", "200")))
_MIN_DESCRIPTION_CHARS = max(
    1, int(os.environ.get("NICHE_MIN_DESCRIPTION_CHARS", "800"))
)
_MAX_ID_WINDOW = 250_000
_MAX_GRAPH_LIMIT = 20_000
_MAX_SEED_BATCH = 5_000

_MUTATING_SQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL|DO|"
    r"VACUUM|ANALYZE|REFRESH|REINDEX|CLUSTER)\b",
    re.IGNORECASE,
)


def guard_read_only_sql(statement: str) -> None:
    normalized = re.sub(r"/\*[\s\S]*?\*/|--[^\n]*", " ", str(statement or "")).strip()
    if not normalized or _MUTATING_SQL.search(normalized):
        raise ValueError("active-corpus SQL must be read-only")
    if not re.match(r"^(?:SELECT|WITH|SHOW|EXPLAIN)\b", normalized, re.IGNORECASE):
        raise ValueError("active-corpus SQL must be read-only")


def _inflate(value) -> str:
    if not value:
        return ""
    try:
        return zlib.decompress(bytes(value)).decode("utf-8", "replace")
    except (TypeError, ValueError, zlib.error, UnicodeError):
        return ""


class LocalCorpusProvider(BaseProvider):
    name = "local"

    QUERY = """
    SELECT p.id, p.publication_number, p.kind_code, p.country, p.publication_date,
           p.filing_date, p.earliest_priority_date, p.simple_family_id,
           p.title, p.abstract, p.abstract_lang, p.facsimile_path,
           ds.claims_z, ds.description_z, ds.meta AS docstore_meta,
           COALESCE((SELECT json_agg(json_build_object(
               'number', c.claim_no, 'text', c.text, 'resolved_text', c.resolved_text,
               'language', c.lang, 'independent', c.is_independent,
               'depends_on', COALESCE((SELECT array_agg(parent.claim_no ORDER BY parent.claim_no)
                   FROM claim_dependencies dep
                   JOIN claims parent ON parent.id = dep.depends_on_claim_id
                  WHERE dep.claim_id = c.id), '{}')
           ) ORDER BY c.claim_no) FROM claims c WHERE c.publication_id = p.id), '[]') AS claim_rows,
           COALESCE((SELECT json_agg(json_build_object(
               'id', para.para_no, 'text', para.text, 'section', para.heading,
               'page', para.page_no, 'language', para.lang
           ) ORDER BY para.id) FROM paragraphs para WHERE para.publication_id = p.id), '[]') AS paragraph_rows,
           COALESCE((SELECT json_agg(json_build_object(
               'number', fig.figure_no, 'text', fig.caption
           ) ORDER BY fig.id) FROM figures fig WHERE fig.publication_id = p.id), '[]') AS figure_rows,
           COALESCE((SELECT array_agg(DISTINCT cls.symbol)
               FROM classifications cls WHERE cls.publication_id = p.id AND cls.scheme = 'CPC'), '{}') AS cpc_codes,
           COALESCE((SELECT array_agg(DISTINCT cls.symbol)
               FROM classifications cls WHERE cls.publication_id = p.id AND cls.scheme = 'IPC'), '{}') AS ipc_codes,
           COALESCE((SELECT array_agg(DISTINCT cit.dst_pub)
               FROM citations cit WHERE cit.src_pub = p.publication_number), '{}') AS citations
      FROM publications p
      LEFT JOIN sources_docstore ds
        ON ds.publication_number = regexp_replace(upper(p.publication_number), '[^A-Z0-9]', '', 'g')
     WHERE p.publication_number = ANY(%s)
     ORDER BY p.id
     LIMIT 1
    """

    def __init__(self, connection_factory: Callable):
        self.connection_factory = connection_factory

    def enabled(self) -> bool:
        return self.connection_factory is not None

    def fetch(self, request: FetchRequest) -> ProviderResult | None:
        if not self.enabled():
            return None
        publication = normalize_publication_number(request.publication_number)
        guard_read_only_sql(self.QUERY)
        connection = self.connection_factory()
        try:
            connection.execute("SET TRANSACTION READ ONLY")
            with connection.cursor() as cursor:
                cursor.execute(self.QUERY, (list(source_publication_variants(publication)),))
                row = cursor.fetchone()
            connection.rollback()
        finally:
            connection.close()
        if not row:
            return None
        row = dict(row)
        claims = list(row.get("claim_rows") or [])
        paragraphs = list(row.get("paragraph_rows") or [])
        figures = list(row.get("figure_rows") or [])
        docstore_meta = dict(row.get("docstore_meta") or {})
        claims_text = _inflate(row.get("claims_z"))
        description_text = _inflate(row.get("description_z"))
        if not claims and claims_text:
            claims = [line for line in claims_text.splitlines() if line.strip()]
        if not paragraphs and description_text:
            paragraphs = [
                {"id": f"local-{index}", "text": line, "section": "", "page": None,
                 "language": docstore_meta.get("desc_lang") or ""}
                for index, line in enumerate(description_text.splitlines(), 1) if line.strip()
            ]
        canonical = {
            "publication_number": publication,
            "publication_id": publication,
            "family_id": row.get("simple_family_id") or "",
            "title": row.get("title") or "",
            "abstract": row.get("abstract") or "",
            "language": row.get("abstract_lang") or docstore_meta.get("claims_lang") or "",
            "claims": claims,
            "description_paragraphs": paragraphs,
            "figure_captions": figures,
            "citations": list(row.get("citations") or []),
            "cpc": list(row.get("cpc_codes") or []),
            "ipc": list(row.get("ipc_codes") or []),
            "dates": {
                "publication_date": str(row.get("publication_date") or ""),
                "filing_date": str(row.get("filing_date") or ""),
                "earliest_priority_date": str(row.get("earliest_priority_date") or ""),
            },
            "source": {
                "provider": self.name,
                "facsimile_path": row.get("facsimile_path") or "",
                "docstore_source": docstore_meta.get("source") or "",
            },
        }
        #  `claims` is a list of ROWS when the corpus has structured claims and a list of STRINGS
        #  when it only has the compressed claims text, which is the case for exactly the
        #  publications this provider exists to rescue. Measuring the text is not the place to
        #  discover that: `value.get` on a string failed 19,403 publications before this line
        #  stopped assuming a shape.
        claim_chars = sum(len(item_text(value)) for value in claims)
        description_chars = sum(len(item_text(value)) for value in paragraphs)
        completeness = Completeness(
            has_title=bool(canonical["title"]),
            has_abstract=bool(canonical["abstract"]),
            has_claims=bool(claims),
            has_complete_claims=claim_chars >= _MIN_CLAIMS_CHARS,
            has_description=bool(paragraphs),
            has_complete_description=description_chars >= _MIN_DESCRIPTION_CHARS,
            has_figures=bool(figures),
            has_citations=bool(canonical["citations"]),
        )
        canonical["completeness"] = completeness.__dict__
        content = json.dumps(canonical, ensure_ascii=False, default=str).encode("utf-8")
        return ProviderResult(
            provider=self.name,
            content=content,
            media_type="application/json",
            source_url="local-corpus",
            http_status=None,
            credits_used=0,
            completeness=completeness,
            metadata={"active_corpus_read_only": True},
        )


class LocalDiscoverySource:
    """Bounded primary-key range reader over the existing corpus.

    Explicit ranges use ``(range_start, range_end]``. Each range has its own durable watermark,
    so several discovery processes can cover adjacent intervals without sharing progress or
    reading the same source publication id.
    """

    def __init__(self, connection_factory, *, id_window: int | None = None,
                 graph_limit: int | None = None, range_start: int = 0,
                 range_end: int | None = None):
        self.connection_factory = connection_factory
        self.id_window = min(_MAX_ID_WINDOW, max(1000, int(
            id_window if id_window is not None
            else os.environ.get("NICHE_DISCOVERY_ID_WINDOW", "100000")
        )))
        self.graph_limit = min(_MAX_GRAPH_LIMIT, max(1, int(
            graph_limit if graph_limit is not None
            else os.environ.get("NICHE_DISCOVERY_GRAPH_LIMIT", "5000")
        )))
        self.range_start = max(0, int(range_start or 0))
        self.range_end = None if range_end in (None, 0) else int(range_end)
        if self.range_end is not None and self.range_end <= self.range_start:
            raise ValueError("discovery range end must be greater than range start")
        if self.range_start == 0 and self.range_end is None:
            self.watermark_key = "publication_id"
            self.source_max_watermark_key = "source_max_publication_id"
        else:
            label = str(self.range_end) if self.range_end is not None else "max"
            scope = f"{self.range_start}:{label}"
            self.watermark_key = f"publication_id:{scope}"
            self.source_max_watermark_key = f"source_max_publication_id:{scope}"

    def _connection(self):
        connection = self.connection_factory()
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '15s'")
        return connection

    def _publication_rows(self, ids, *, include_all: bool = False) -> list[PublicationRecord]:
        ids = sorted({int(value) for value in ids})
        if not ids:
            return []
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                query = """
                SELECT id, publication_number, kind_code, country, publication_date,
                       filing_date, earliest_priority_date, simple_family_id,
                       title, abstract, abstract_lang, facsimile_path
                  FROM publications
                 WHERE id = ANY(%s)
                 ORDER BY id
                """
                guard_read_only_sql(query)
                cursor.execute(query, (ids,))
                publications = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT publication_id, count(*) n, COALESCE(sum(length(text)), 0) chars "
                    "FROM claims WHERE publication_id = ANY(%s) GROUP BY publication_id",
                    (ids,),
                )
                claims = {int(row["publication_id"]): dict(row) for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT publication_id, count(*) n, COALESCE(sum(length(text)), 0) chars "
                    "FROM paragraphs WHERE publication_id = ANY(%s) GROUP BY publication_id",
                    (ids,),
                )
                paragraphs = {int(row["publication_id"]): dict(row) for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT publication_id, count(*) n FROM figures "
                    "WHERE publication_id = ANY(%s) GROUP BY publication_id",
                    (ids,),
                )
                figures = {int(row["publication_id"]): int(row["n"])
                           for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT publication_id, scheme, array_agg(DISTINCT symbol) codes "
                    "FROM classifications WHERE publication_id = ANY(%s) "
                    "GROUP BY publication_id, scheme",
                    (ids,),
                )
                codes = {}
                for row in cursor.fetchall():
                    codes.setdefault(int(row["publication_id"]), {})[str(row["scheme"] or "").upper()] = list(row["codes"] or [])
                raw_numbers = [row["publication_number"] for row in publications]
                canonical = [normalize_publication_number(value) for value in raw_numbers]
                cursor.execute(
                    "SELECT src_pub, count(*) n FROM citations WHERE src_pub = ANY(%s) GROUP BY src_pub",
                    (list(dict.fromkeys([*raw_numbers, *canonical])),),
                )
                citation_counts = {normalize_publication_number(row["src_pub"]): int(row["n"])
                                   for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT publication_number, meta, claims_z IS NOT NULL AS has_claims_raw, "
                    "description_z IS NOT NULL AS has_description_raw FROM sources_docstore "
                    "WHERE publication_number = ANY(%s)",
                    (canonical,),
                )
                docstore = {}
                for row in cursor.fetchall():
                    held = dict(row.get("meta") or {})
                    held["has_claims_raw"] = bool(row.get("has_claims_raw"))
                    held["has_description_raw"] = bool(row.get("has_description_raw"))
                    docstore[normalize_publication_number(row["publication_number"])] = held
            connection.rollback()
        finally:
            connection.close()

        out = []
        cpc_prefixes = all_cpc_prefixes()
        ipc_prefixes = all_ipc_prefixes()
        for row in publications:
            publication = normalize_publication_number(row["publication_number"])
            claim = claims.get(int(row["id"]), {})
            paragraph = paragraphs.get(int(row["id"]), {})
            held = docstore.get(publication, {})
            cpc = tuple(codes.get(int(row["id"]), {}).get("CPC") or ())
            ipc = tuple(codes.get(int(row["id"]), {}).get("IPC") or ())
            claim_chars = max(int(claim.get("chars") or 0), int(held.get("claims_chars") or 0))
            description_chars = max(
                int(paragraph.get("chars") or 0), int(held.get("desc_chars") or 0)
            )
            has_claims = bool(claim.get("n") or held.get("has_claims_raw"))
            has_description = bool(paragraph.get("n") or held.get("has_description_raw"))
            if any(str(code).replace(" ", "").upper().startswith(prefix)
                   for code in cpc for prefix in cpc_prefixes):
                signal = "cpc"
            elif any(str(code).replace(" ", "").upper().startswith(prefix)
                     for code in ipc for prefix in ipc_prefixes):
                signal = "ipc"
            else:
                signal = "terminology"
            record = PublicationRecord(
                publication_number=publication,
                family_id=str(row.get("simple_family_id") or ""),
                authority=str(row.get("country") or ""),
                kind_code=str(row.get("kind_code") or ""),
                title=str(row.get("title") or ""),
                abstract=str(row.get("abstract") or ""),
                language=str(row.get("abstract_lang") or held.get("claims_lang") or ""),
                cpc_codes=cpc,
                ipc_codes=ipc,
                publication_date=str(row.get("publication_date") or ""),
                filing_date=str(row.get("filing_date") or ""),
                earliest_priority_date=str(row.get("earliest_priority_date") or ""),
                has_claims=has_claims,
                has_complete_claims=claim_chars >= _MIN_CLAIMS_CHARS,
                has_description=has_description,
                has_complete_description=description_chars >= _MIN_DESCRIPTION_CHARS,
                has_figures=bool(figures.get(int(row["id"]))),
                has_citations=bool(citation_counts.get(publication)),
                preferred_source="local" if has_claims or has_description else "",
                raw_object_uri=str(row.get("facsimile_path") or ""),
                discovery_signals=(signal,),
                priority=4,
            )
            if include_all or in_niche(record):
                out.append(replace(record, priority=priority_for_record(record)))
        return out

    def seed_records(self, groups, watermarks, limit):
        limit = min(_MAX_SEED_BATCH, max(1, int(limit)))
        start = max(self.range_start, int(watermarks.get(
            self.watermark_key, self.range_start
        )))
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT max(id) AS max_id FROM publications")
                row = cursor.fetchone() or {}
                source_max_id = int(row.get("max_id") or 0)
            connection.rollback()
        finally:
            connection.close()
        max_id = min(source_max_id, self.range_end) if self.range_end is not None else source_max_id
        if start >= max_id:
            return [], {
                self.watermark_key: start,
                self.source_max_watermark_key: max_id,
            }
        end = min(max_id, start + self.id_window)
        cpc_prefixes = all_cpc_prefixes(groups)
        ipc_prefixes = all_ipc_prefixes(groups)
        patterns = [f"%{term.lower()}%" for group in groups for term in group.terms]
        cpc_range_sql = " OR ".join(
            "(c.symbol >= %s AND c.symbol < %s)" for _ in cpc_prefixes
        )
        ipc_range_sql = " OR ".join(
            "(c.symbol >= %s AND c.symbol < %s)" for _ in ipc_prefixes
        )
        sql = f"""
        SELECT p.id
          FROM publications p
         WHERE p.id > %s AND p.id <= %s
           AND (
             EXISTS (
                 SELECT 1 FROM classifications c
                  WHERE c.publication_id = p.id
                    AND ((c.scheme = 'CPC' AND ({cpc_range_sql}))
                         OR (c.scheme = 'IPC' AND ({ipc_range_sql})))
             )
             OR lower(COALESCE(p.title, '') || ' ' || COALESCE(p.abstract, '')) LIKE ANY(%s)
           )
         ORDER BY p.id
         LIMIT %s
        """
        guard_read_only_sql(sql)
        params = [start, end]
        for prefix in cpc_prefixes:
            params.extend((prefix, f"{prefix}\uffff"))
        for prefix in ipc_prefixes:
            params.extend((prefix, f"{prefix}\uffff"))
        params.extend((patterns, limit))
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                ids = [int(row["id"]) for row in cursor.fetchall()]
            connection.rollback()
        finally:
            connection.close()
        next_watermark = ids[-1] if len(ids) >= limit else end
        return self._publication_rows(ids), {
            self.watermark_key: next_watermark,
            self.source_max_watermark_key: max_id,
        }

    def family_members(self, records):
        families = sorted({record.family_id for record in records if record.family_id})
        if not families:
            return []
        sql = "SELECT id FROM publications WHERE simple_family_id = ANY(%s) ORDER BY id LIMIT %s"
        guard_read_only_sql(sql)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, (families, self.graph_limit))
                ids = [int(row["id"]) for row in cursor.fetchall()]
            connection.rollback()
        finally:
            connection.close()
        return self._publication_rows(ids, include_all=True)

    def citations(self, records):
        source_numbers = list(dict.fromkeys(
            variant
            for record in records
            for variant in source_publication_variants(record.publication_number)
        ))
        if not source_numbers:
            return []
        sql = (
            "SELECT src_pub, dst_pub FROM citations WHERE src_pub = ANY(%s) "
            "ORDER BY src_pub, dst_pub LIMIT %s"
        )
        guard_read_only_sql(sql)
        cited_by_number = {}
        batch_size = 250
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                for offset in range(0, len(source_numbers), batch_size):
                    remaining = self.graph_limit - len(cited_by_number)
                    if remaining <= 0:
                        break
                    cursor.execute(
                        sql,
                        (source_numbers[offset:offset + batch_size], remaining),
                    )
                    for row in cursor.fetchall():
                        raw = str(row.get("dst_pub") or "")
                        canonical = normalize_publication_number(raw)
                        if canonical:
                            cited_by_number.setdefault(canonical, raw)
                cited = list(cited_by_number)
                lookup_values = list(dict.fromkeys(
                    variant
                    for canonical, raw in cited_by_number.items()
                    for variant in (*source_publication_variants(canonical), raw)
                ))
                cursor.execute(
                    "SELECT id, publication_number FROM publications "
                    "WHERE publication_number = ANY(%s) ORDER BY id LIMIT %s",
                    (lookup_values, self.graph_limit),
                )
                matches = [dict(row) for row in cursor.fetchall()]
            connection.rollback()
        finally:
            connection.close()
        found = self._publication_rows(
            [row["id"] for row in matches], include_all=True
        )
        found_numbers = {record.publication_number for record in found}
        missing = [
            PublicationRecord(
                publication_number=publication,
                discovery_signals=("citation",),
                priority=2,
            )
            for publication in cited if publication and publication not in found_numbers
        ]
        return list(merge_publications((*found, *missing)).values())

    def co_classified(self, records):
        cpc_codes = sorted({code for record in records for code in record.cpc_codes})[:32]
        ipc_codes = sorted({code for record in records for code in record.ipc_codes})[:32]
        if not cpc_codes and not ipc_codes:
            return []
        sql = """
        SELECT DISTINCT publication_id AS id
          FROM classifications
         WHERE (scheme = 'CPC' AND symbol = ANY(%s))
            OR (scheme = 'IPC' AND symbol = ANY(%s))
         ORDER BY publication_id
         LIMIT %s
        """
        guard_read_only_sql(sql)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, (cpc_codes, ipc_codes, self.graph_limit))
                ids = [int(row["id"]) for row in cursor.fetchall()]
            connection.rollback()
        finally:
            connection.close()
        return self._publication_rows(ids, include_all=True)
