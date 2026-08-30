"""The live corpus is read only to a search, and that is a property of the connection.

WHAT THESE PROVE AND WHAT THEY DO NOT. They prove that an armed process refuses every write
statement that names a corpus table, that it still writes the application's own tables, and that
the offline ingestion path is distinguished by its CONNECTION rather than by its intentions. They
do NOT execute a real INSERT into `chunks` even inside a rolled back transaction: the live corpus
is read only to this whole workstream, and an insert-then-rollback is still a write attempt
against a 27 million row table on an I/O saturated box. The ingestion side is therefore asserted
at the policy and connection level, which is where the distinction actually lives.
"""
import uuid

import pytest

import corpus_guard
import db


@pytest.fixture()
def armed():
    corpus_guard.arm("test")
    try:
        yield
    finally:
        corpus_guard.disarm()


# ------------------------------------------------------------------ what is refused
SEARCH_PATH_WRITES = [
    #  enrich._persist_full_text, verbatim, one statement per line it actually runs.
    "INSERT INTO chunks(publication_id,kind,ref_id,coord,lang,text,token_count) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
    "INSERT INTO claims(publication_id,claim_no,is_independent,lang,text,resolved_text) "
    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
    "INSERT INTO paragraphs(publication_id,para_no,heading,page_no,lang,text) "
    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
    "UPDATE publications SET abstract=%s WHERE id=%s",
    "UPDATE publications SET facsimile_path=%s WHERE id=%s",
    "UPDATE chunks SET embedding=%s::vector WHERE id=%s",
    "INSERT INTO legal_events(publication_id,event_code,event_date,raw) VALUES (%s,%s,%s,%s)",
    "INSERT INTO field_provenance(entity,entity_id,field,source_id,ocr_status) "
    "VALUES ('publication',%s,'recovered_fulltext',%s,%s)",
    "INSERT INTO sources(name, version) VALUES (%s, %s)",
    #  incremental_ingest / chunker: the COPY path and the index maintenance that has blocked
    #  live searches.
    "COPY chunks (publication_id, kind, ref_id, coord, lang, text, token_count) FROM STDIN",
    "CREATE INDEX CONCURRENTLY ix_chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)",
    "REINDEX TABLE chunks",
    "DELETE FROM citations WHERE publication_id = %s",
    "TRUNCATE TABLE parties",
    "ALTER TABLE classifications ADD COLUMN x int",
    "VACUUM ANALYZE publications",
]


@pytest.mark.parametrize("sql", SEARCH_PATH_WRITES)
def test_every_search_reachable_corpus_write_is_refused(armed, sql):
    with pytest.raises(corpus_guard.CorpusWriteBlocked):
        corpus_guard.check(sql)


def test_a_write_to_chunks_raises_on_a_real_connection(armed):
    """Not a unit test of the matcher: a cursor from db.connect() in an armed process refuses it,
    which is the thing that makes the prohibition structural rather than advisory."""
    with db.cursor() as cur:
        with pytest.raises(corpus_guard.CorpusWriteBlocked):
            cur.execute("INSERT INTO chunks(publication_id, kind, text) VALUES (1, 'x', 'y')")


def test_the_guard_travels_with_the_connection_not_the_caller(armed):
    """conn.execute() uses the connection's own cursor factory, so there is no way to obtain an
    unguarded cursor from a guarded connection."""
    conn = db.connect(autocommit=True)
    try:
        with pytest.raises(corpus_guard.CorpusWriteBlocked):
            conn.execute("UPDATE publications SET abstract='x' WHERE id=1")
        with pytest.raises(corpus_guard.CorpusWriteBlocked):
            conn.cursor().execute("DELETE FROM chunks WHERE id=1")
    finally:
        conn.close()


def test_the_settings_that_would_undo_the_prohibition_are_refused(armed):
    for sql in ("SET default_transaction_read_only = off",
                "SET SESSION session_replication_role = 'replica'",
                "SET ROLE patents",
                "SET SESSION AUTHORIZATION postgres"):
        with pytest.raises(corpus_guard.CorpusWriteBlocked):
            corpus_guard.check(sql)


def test_an_unparsed_write_fails_closed(armed):
    """A write verb whose target we cannot read is a defect in the matcher, not a permission."""
    with pytest.raises(corpus_guard.CorpusWriteBlocked):
        corpus_guard.check("INSERT /* who knows */ 1")


def test_comment_tokens_inside_literals_cannot_hide_a_later_write(armed):
    """The SQL lexer must distinguish string data from real comments across statements."""
    for sql in (
            "SELECT '-- this is data'; UPDATE publications SET abstract='x' WHERE id=1",
            "SELECT '/* also data */'; DELETE FROM chunks WHERE id=1",
            "SELECT $$-- dollar quoted data$$; TRUNCATE parties",
    ):
        with pytest.raises(corpus_guard.CorpusWriteBlocked):
            corpus_guard.check(sql)


def test_runtime_ddl_and_privilege_changes_on_the_corpus_are_refused(armed):
    for sql in (
            "CREATE TABLE publications (id bigint)",
            "GRANT UPDATE ON publications TO app_user",
            "REVOKE SELECT ON chunks FROM app_user",
            "COMMENT ON TABLE citations IS 'mutable metadata'",
    ):
        with pytest.raises(corpus_guard.CorpusWriteBlocked):
            corpus_guard.check(sql)


def test_a_protected_table_cannot_hide_later_in_a_multi_target_write(armed):
    for sql in (
            "TRUNCATE app_temp, publications",
            "DROP TABLE app_temp, chunks",
    ):
        with pytest.raises(corpus_guard.CorpusWriteBlocked):
            corpus_guard.check(sql)


# ------------------------------------------------------------------ what is allowed
def test_reads_of_the_corpus_are_untouched(armed):
    """SELECT is the whole point of the corpus and must not pay for the guard."""
    for sql in ("SELECT id, abstract FROM publications WHERE publication_number=%s LIMIT 1",
                "SELECT ch.text FROM chunks ch JOIN publications p ON p.id=ch.publication_id "
                "WHERE p.publication_number=%s ORDER BY ch.id LIMIT 150",
                "WITH c AS (SELECT id FROM chunks LIMIT 1) SELECT * FROM c",
                "SELECT count(*) FROM citations WHERE origin='X'"):
        corpus_guard.check(sql)
    corpus_guard.check("SELECT '-- DELETE FROM publications' AS harmless_text")


def test_select_for_update_is_a_read(armed):
    """`FOR UPDATE` is a row lock inside a SELECT. The writes it guards are refused on their own,
    and treating the lock itself as a write would break the run store's own claim query."""
    corpus_guard.check("SELECT run_id FROM search_runs WHERE status='queued' "
                       "ORDER BY priority FOR UPDATE SKIP LOCKED LIMIT 1")


def test_an_armed_process_still_writes_its_own_tables(armed):
    """The app's own state (runs, accounts, evidence, the scratch store) is not the corpus and
    must keep working, or the guard would take the web app down with it."""
    for sql in ("INSERT INTO search_runs (run_id, slug) VALUES (%s,%s)",
                "UPDATE app_run_queue SET state='done' WHERE slug=%s",
                "INSERT INTO sources_docstore (publication_number, title) VALUES (%s,%s)",
                "INSERT INTO evidence_charts (publication_number, subject_fp, chart) "
                "VALUES (%s,%s,%s)",
                "UPDATE corpus_ingest_queue SET state='ingested' WHERE id=%s"):
        corpus_guard.check(sql)


def test_sources_docstore_is_not_mistaken_for_sources(armed):
    """The scratch store's name starts with a protected table's name. Identifier matching, not
    prefix matching, is what keeps demand fetched text writable."""
    corpus_guard.check("INSERT INTO sources_docstore (publication_number) VALUES (%s)")
    with pytest.raises(corpus_guard.CorpusWriteBlocked):
        corpus_guard.check("INSERT INTO sources (name) VALUES (%s)")


def test_a_real_write_to_an_app_table_still_lands(armed):
    """End to end on a real connection, so this is not just the matcher agreeing with itself."""
    pub = f"TEST-GUARD-{uuid.uuid4().hex[:8]}"
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO sources_docstore (publication_number, title) VALUES (%s,%s)",
                        (pub, "guard test"))
        with db.cursor() as cur:
            cur.execute("SELECT title FROM sources_docstore WHERE publication_number=%s", (pub,))
            assert cur.fetchone()["title"] == "guard test"
    finally:
        corpus_guard.disarm()
        with db.cursor() as cur:
            cur.execute("DELETE FROM sources_docstore WHERE publication_number=%s", (pub,))


# ------------------------------------------------------------------ the offline path
def test_the_offline_ingestion_path_still_writes():
    """An UNARMED process is the ingestion role. Its connections carry no guard at all, so
    ingest_bq, ingest_pg, chunker, embed, ops and incremental_ingest are byte-for-byte unchanged.
    """
    corpus_guard.disarm()
    assert corpus_guard.armed() is False
    for sql in SEARCH_PATH_WRITES:
        corpus_guard.check(sql)                    # no exception: this IS the ingestion path
    conn = db.connect()
    try:
        #  no cursor_factory was installed, so the cursor is psycopg's own
        assert type(conn.cursor()).__name__ == "Cursor"
    finally:
        conn.close()


def test_an_armed_process_can_still_take_the_ingestion_role_explicitly(armed):
    """One named, thread scoped, greppable escape hatch, for a batch job that happens to run
    inside an armed process. Nothing in the search path uses it."""
    with pytest.raises(corpus_guard.CorpusWriteBlocked):
        corpus_guard.check("INSERT INTO chunks(publication_id) VALUES (1)")
    with corpus_guard.allow_corpus_writes("test: the nightly release"):
        corpus_guard.check("INSERT INTO chunks(publication_id) VALUES (1)")
    #  and it is closed again on the way out
    with pytest.raises(corpus_guard.CorpusWriteBlocked):
        corpus_guard.check("INSERT INTO chunks(publication_id) VALUES (1)")


def test_the_escape_hatch_demands_a_reason(armed):
    with pytest.raises(ValueError), corpus_guard.allow_corpus_writes(""):
        pass


def test_the_env_disarm_is_honoured(armed, monkeypatch):
    """A batch job started from an armed parent says so in its environment."""
    monkeypatch.setenv("PATENT_CORPUS_INGEST", "1")
    assert corpus_guard.armed() is False
    corpus_guard.check("INSERT INTO chunks(publication_id) VALUES (1)")


def test_a_read_only_corpus_cursor_is_enforced_by_postgres(armed):
    """The belt to the guard's braces: even a caller that got past this module cannot write on a
    READ ONLY transaction, because Postgres refuses it."""
    import psycopg
    with db.corpus_cursor() as cur:
        cur.execute("SELECT 1 AS one")
        assert cur.fetchone()["one"] == 1
        #  A write the GUARD permits (sources_docstore is the scratch store, not the corpus), so
        #  the refusal below can only be coming from Postgres.
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            cur.execute("INSERT INTO sources_docstore (publication_number) VALUES ('X-READONLY')")


def test_retrieval_connections_request_postgres_readonly(monkeypatch):
    """Every retrieval connection gets the database-enforced read-only option, armed or not."""
    from retrieval import base

    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            pass

    class Connection:
        closed = False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(base.db, "connect", lambda **kw: calls.append(kw) or Connection())
    base.close_worker_conn()
    base.RetrieverBase(family_map={})
    base.worker_conn()
    assert len(calls) == 2 and all(call.get("readonly") is True for call in calls)
