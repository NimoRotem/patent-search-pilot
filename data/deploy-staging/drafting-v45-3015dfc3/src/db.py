"""Postgres helpers (psycopg 3).

Every connection this module hands out is subject to `corpus_guard`: in an ARMED process, currently
only the dormant standalone search worker, a statement that writes `publications`, `chunks`,
`classifications`, `citations`, `families`, `parties` or the rest of the live corpus raises
`CorpusWriteBlocked` before it reaches Postgres. The production web process is not armed until its
route is cut over to that worker. Offline ingestion processes never arm, so they write exactly as
before. See docs/corpus_write_policy.md.
"""
import contextlib
import psycopg
from psycopg.rows import dict_row
from config import PG_DSN

import corpus_guard


def connect(autocommit=False, readonly=False):
    """A connection. Guarded whenever the process is armed.

    `readonly=True` additionally puts the SESSION in a READ ONLY transaction, which Postgres
    enforces itself: it is the belt to the guard's braces, and it holds even if a caller reached
    psycopg directly. Use it for the retrieval path, which never writes anything.
    """
    kw = {}
    if corpus_guard.armed():
        kw["cursor_factory"] = corpus_guard.cursor_class()
    if readonly:
        #  A SESSION GUC, not `conn.read_only`. psycopg applies the transaction property on BEGIN,
        #  and an autocommit connection never issues one, so `conn.read_only = True` is silently a
        #  no-op exactly where the retrieval path uses it. `default_transaction_read_only` is set
        #  by the server at connect time and applies to every statement, autocommit or not.
        kw["options"] = "-c default_transaction_read_only=on"
    return psycopg.connect(PG_DSN, autocommit=autocommit, row_factory=dict_row, **kw)


@contextlib.contextmanager
def cursor(autocommit=False, readonly=False):
    conn = connect(autocommit=autocommit, readonly=readonly)
    try:
        with conn.cursor() as cur:
            yield cur
        if not autocommit:
            conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def corpus_cursor():
    """A READ ONLY cursor over the live corpus. Postgres refuses any write on it, guard or no
    guard. This is what the retrieval path should hold."""
    with cursor(autocommit=True, readonly=True) as cur:
        yield cur


@contextlib.contextmanager
def ingest_cursor(reason):
    """THE OFFLINE INGESTION SEAM. The only way to write the corpus from an armed process.

    `reason` is required and is logged by corpus_guard. Nothing in the search path may call this.
    """
    with corpus_guard.allow_corpus_writes(reason):
        with cursor() as cur:
            yield cur


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
