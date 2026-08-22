"""Explicit PostgreSQL connection helpers for niche staging and read-only source access."""
from __future__ import annotations

import os
from pathlib import Path


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
