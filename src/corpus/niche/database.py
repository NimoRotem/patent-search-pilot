"""Explicit PostgreSQL connection helpers for niche staging and read-only source access."""
from __future__ import annotations

import os
from pathlib import Path

_APPROVED_DATABASE_PREFIX = "niche_full_v1"


def require_dsn(value: str | None, variable: str) -> str:
    dsn = str(value or "").strip()
    if not dsn:
        raise RuntimeError(
            f"{variable} is required; the niche pipeline never guesses a production database"
        )
    return dsn


def connection_factory(dsn: str, *, application_name: str = "niche-corpus"):
    from psycopg import connect
    from psycopg.rows import dict_row

    safe_dsn = require_dsn(dsn, "database DSN")

    def open_connection():
        return connect(
            safe_dsn,
            row_factory=dict_row,
            application_name=application_name,
            connect_timeout=10,
        )

    return open_connection


def apply_schema(factory, migration_path: str | os.PathLike) -> None:
    sql = Path(migration_path).read_text()
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(sql)


def validate_database_target(factory, expected_database: str) -> None:
    """Check the database name before any schema-changing statement can run."""
    expected = str(expected_database or "").strip()
    if not expected:
        raise RuntimeError("NICHE_EXPECTED_DATABASE is required")
    if not expected.startswith(_APPROVED_DATABASE_PREFIX):
        raise RuntimeError("NICHE_EXPECTED_DATABASE is not an approved niche staging name")
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_database() AS database_name")
        row = cursor.fetchone() or {}
    if str(row.get("database_name") or "") != expected:
        raise RuntimeError("niche staging database name mismatch")


def validate_staging_database(
    factory,
    expected_database: str,
    fingerprint: str,
) -> None:
    """Refuse work unless both the database name and durable marker match."""
    expected = str(expected_database or "").strip()
    marker = str(fingerprint or "").strip()
    if not expected or not marker:
        raise RuntimeError("staging database name and fingerprint are required")
    validate_database_target(factory, expected)
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT identity.database_name, identity.fingerprint "
            "FROM niche_corpus.pipeline_identity AS identity "
            "WHERE identity.singleton = true"
        )
        row = cursor.fetchone() or {}
    if str(row.get("database_name") or "") != expected:
        raise RuntimeError("niche staging identity database name mismatch")
    if str(row.get("fingerprint") or "") != marker:
        raise RuntimeError("niche staging database fingerprint mismatch")


def initialize_staging_identity(
    factory,
    expected_database: str,
    fingerprint: str,
) -> None:
    """Initialize once, without replacing a marker from another pipeline."""
    expected = str(expected_database or "").strip()
    marker = str(fingerprint or "").strip()
    if not marker:
        raise RuntimeError("NICHE_DATABASE_FINGERPRINT is required")
    validate_database_target(factory, expected)
    with factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO niche_corpus.pipeline_identity "
            "(singleton,database_name,fingerprint) VALUES (true,%s,%s) "
            "ON CONFLICT (singleton) DO NOTHING",
            (expected, marker),
        )
    validate_staging_database(factory, expected, marker)
