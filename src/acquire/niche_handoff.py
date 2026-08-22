"""Optional handoff from the proven acquisition worker into isolated niche parse staging."""
from __future__ import annotations

import os


def _factory(dsn: str):
    from psycopg import connect
    from psycopg.rows import dict_row

    def open_connection():
        return connect(dsn, row_factory=dict_row, connect_timeout=10)

    return open_connection


def enqueue(
    *,
    publication_number: str,
    family_id: str,
    authority: str,
    source_uri: str,
    source_generation: str,
    expected_database: str | None = None,
    fingerprint: str | None = None,
    connection_factory=None,
) -> bool:
    """Insert only a minimal manifest row and a parse job in the isolated staging schema."""
    if not source_uri:
        return False
    if connection_factory is None:
        dsn = str(os.environ.get("NICHE_PARSE_DATABASE_URL") or "").strip()
        if not dsn:
            return False
        connection_factory = _factory(dsn)
    expected_database = str(
        expected_database or os.environ.get("NICHE_EXPECTED_DATABASE") or ""
    ).strip()
    fingerprint = str(
        fingerprint or os.environ.get("NICHE_DATABASE_FINGERPRINT") or ""
    ).strip()
    if not expected_database or not fingerprint:
        raise RuntimeError("niche handoff requires the staging database identity")

    from corpus.niche.identifiers import normalize_publication_number

    publication = normalize_publication_number(publication_number)
    if not publication:
        raise ValueError("niche handoff requires a publication number")
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database() AS database_name, identity.fingerprint "
            "FROM niche_corpus.pipeline_identity AS identity WHERE identity.singleton=true"
        )
        identity = cursor.fetchone() or {}
        if str(identity.get("database_name") or "") != expected_database:
            raise RuntimeError("niche handoff database name mismatch")
        if str(identity.get("fingerprint") or "") != fingerprint:
            raise RuntimeError("niche handoff database fingerprint mismatch")
        cursor.execute(
            """
            INSERT INTO niche_corpus.niche_publications
                (publication_id, publication_number, family_id, authority,
                 fetch_status, preferred_source, discovery_signals)
            VALUES (%s,%s,%s,%s,'partial','fulltext_acquisition',ARRAY['fetch_handoff'])
            ON CONFLICT (publication_id) DO UPDATE SET
                family_id=COALESCE(NULLIF(niche_corpus.niche_publications.family_id, ''),
                                   EXCLUDED.family_id),
                authority=COALESCE(NULLIF(niche_corpus.niche_publications.authority, ''),
                                   EXCLUDED.authority),
                discovery_signals=ARRAY(
                    SELECT DISTINCT value FROM unnest(
                        niche_corpus.niche_publications.discovery_signals ||
                        EXCLUDED.discovery_signals
                    ) AS value ORDER BY value
                ),
                updated_at=now()
            """,
            (publication, publication, str(family_id or "") or None,
             str(authority or publication[:2])[:4]),
        )
        cursor.execute(
            """
            INSERT INTO niche_corpus.niche_parse_jobs
                (publication_id, source_kind, source_uri, source_generation)
            VALUES (%s,'gcs',%s,%s)
            ON CONFLICT (source_uri, source_generation) DO UPDATE
               SET publication_id=EXCLUDED.publication_id
            """,
            (publication, str(source_uri), str(source_generation or "")),
        )
    return True
