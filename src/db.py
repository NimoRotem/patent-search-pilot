"""Postgres helpers (psycopg 3)."""
import contextlib
import psycopg
from psycopg.rows import dict_row
from config import PG_DSN


def connect(autocommit=False):
    return psycopg.connect(PG_DSN, autocommit=autocommit, row_factory=dict_row)


@contextlib.contextmanager
def cursor(autocommit=False):
    conn = connect(autocommit=autocommit)
    try:
        with conn.cursor() as cur:
            yield cur
        if not autocommit:
            conn.commit()
    finally:
        conn.close()


def get_source_id(name, version=None):
    """Upsert a source row, return its id (for the provenance ledger)."""
    with cursor(autocommit=True) as cur:
        cur.execute(
            """INSERT INTO sources(name, version) VALUES (%s, %s)
               ON CONFLICT (name, version) DO UPDATE SET name=EXCLUDED.name
               RETURNING id""",
            (name, version),
        )
        return cur.fetchone()["id"]


def scalar(sql, params=None):
    with cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return None if row is None else list(row.values())[0]
