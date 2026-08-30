"""Tests for the weekly incremental corpus refresh (src/incremental_ingest.py).

Covers the three things that would be expensive or dangerous to get wrong:
  1. watermark + safety-lookback computation (never hardcoded, always rewinds)
  2. idempotency -- re-running must not duplicate publications or chunks
  3. the BigQuery cost guard -- an automated job must refuse a runaway scan

Plus a parity test asserting the delta chunker produces byte-identical chunks to the
original whole-corpus chunker, since a drift there would silently poison the index.
"""
import datetime as dt
import re

import pytest

import bqclient
import incremental_ingest as inc
import ingest_bq
import db


# ---------------------------------------------------------------------------------------
# 1. watermark + lookback
# ---------------------------------------------------------------------------------------
def test_watermark_comes_from_db_not_a_constant():
    """The watermark must be read from the corpus, and must be a real date."""
    wm = inc.db_watermark()
    assert isinstance(wm, dt.date)
    # sanity: it should agree with a direct query
    assert wm == db.scalar("SELECT max(publication_date) FROM publications")


def test_effective_since_rewinds_by_lookback():
    wm = dt.date(2026, 4, 21)
    assert inc.effective_since(wm, lookback_days=90) == dt.date(2026, 1, 21)
    assert inc.effective_since(wm, lookback_days=0) == wm


def test_effective_since_override_wins():
    wm = dt.date(2026, 4, 21)
    override = dt.date(2020, 1, 1)
    assert inc.effective_since(wm, lookback_days=90, override=override) == override


def test_effective_since_refuses_to_guess_on_empty_corpus():
    """An empty corpus with no --since must abort rather than invent a start date
    (a wrong guess would either cost a full-table scan or silently skip history)."""
    with pytest.raises(ValueError):
        inc.effective_since(None, lookback_days=90)
    # ...unless the operator states it explicitly
    assert inc.effective_since(None, 90, override=dt.date(2000, 1, 1)) == dt.date(2000, 1, 1)


def test_lookback_is_strictly_before_watermark():
    """The whole point of the lookback is late-arriving rows BEHIND the watermark."""
    wm = inc.db_watermark()
    assert inc.effective_since(wm, lookback_days=inc.LOOKBACK_DAYS) < wm


# ---------------------------------------------------------------------------------------
# 2. SQL shape + idempotency
# ---------------------------------------------------------------------------------------
def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def test_delta_extract_has_same_column_shape_as_core():
    """The delta staging table must be loadable by the existing ingest_pg.load_table().
    That only holds if its SELECT list matches the core extract exactly."""
    core = _norm(ingest_bq.CORE_EXTRACT_SQL)
    delta = _norm(ingest_bq.delta_extract_sql(dt.date(2026, 1, 1)))
    body = _norm(ingest_bq._CORE_COLUMNS)
    assert body in core
    assert body in delta
    # every column the loader reads must be present
    for col in ["publication_number", "country_code", "kind_code", "application_number",
                "publication_date", "filing_date", "priority_date", "family_id",
                "title_en", "abstract_en", "abstract_orig", "claims_en", "claims_orig",
                "description_en", "cpc", "ipc", "cites", "assignees", "inventors"]:
        assert f"AS {col}" in body or col in body, col


def test_delta_sql_applies_the_date_window_and_seed_cpc_filter():
    sql = ingest_bq.delta_extract_sql(dt.date(2026, 1, 15), dt.date(2026, 3, 1))
    assert "publication_date >= 20260115" in sql
    assert "publication_date <= 20260301" in sql
    assert "UNNEST(cpc)" in sql
    # open-ended window omits the upper bound entirely
    assert "publication_date <=" not in ingest_bq.delta_extract_sql(dt.date(2026, 1, 15))


def test_delta_jurisdiction_comes_from_config_not_a_hardcoded_list():
    """The delta must search the SAME offices the corpus was built from.

    This used to assert the literal `country_code IN ('US','EP','WO','DE')`, hard-coded in five
    places across ingest_bq. That is now config.INGEST_JURISDICTIONS (empty = worldwide), because
    BigQuery bills columns referenced rather than rows matched — the four-office filter bought
    nothing at the source and only shrank the corpus.

    The real risk the test now guards is a SPLIT: if the weekly delta kept a narrower scope than
    the bootstrap, it would re-narrow the corpus one week at a time, silently, and the only
    symptom would be slowly worsening recall on non-US art.
    """
    import bqclient
    assert bqclient.juris_predicate() in ingest_bq.delta_extract_sql(dt.date(2026, 1, 15))
    assert bqclient.juris_predicate() in ingest_bq._CORE_WHERE, \
        "delta and bootstrap must share one jurisdiction scope"

    # worldwide (the configured default) must be a real predicate, not an omitted clause, so a
    # call site can never forget to handle the empty case and pull the whole table by accident.
    assert bqclient.juris_predicate() == "TRUE" or "country_code IN (" in bqclient.juris_predicate()

    import config
    saved = config.INGEST_JURISDICTIONS
    try:
        config.INGEST_JURISDICTIONS = ["US", "EP"]
        assert bqclient.juris_predicate() == "country_code IN ('US','EP')"
        config.INGEST_JURISDICTIONS = []
        assert bqclient.juris_predicate() == "TRUE"
    finally:
        config.INGEST_JURISDICTIONS = saved


def test_delta_writes_to_its_own_staging_table_not_core():
    """A delta must never clobber the bootstrap core staging table."""
    sql = ingest_bq.delta_extract_sql(dt.date(2026, 1, 1))
    assert ingest_bq.DELTA_TBL in sql
    assert ingest_bq.DELTA_TBL != ingest_bq.CORE_TBL
    assert f"CREATE OR REPLACE TABLE {ingest_bq.CORE_TBL}" not in sql


def test_publication_natural_key_is_unique():
    """Idempotency of the load rests on this constraint: a re-run's INSERT ... ON CONFLICT
    (publication_number, kind_code) DO NOTHING can only be a no-op if the key is unique."""
    dup = db.scalar("""SELECT count(*) FROM (
                         SELECT publication_number, kind_code FROM publications
                         GROUP BY 1,2 HAVING count(*) > 1) t""")
    assert dup == 0


def test_loader_conflict_clause_is_idempotent():
    """Guard the contract we depend on in ingest_pg.load_table()."""
    import inspect
    import ingest_pg
    src = inspect.getsource(ingest_pg.load_table)
    assert "ON CONFLICT (publication_number, kind_code) DO NOTHING" in src


def test_unchunked_queue_is_empty_on_a_fully_processed_corpus():
    """The work queue is 'publications with no chunks that DO have text THIS DEPTH will chunk'.
    On the live, fully-processed corpus it must be empty -- that emptiness is what makes the job
    resumable: after a crash, whatever is left in this queue is exactly the outstanding work,
    with no separate bookkeeping table to get out of sync.

    The live corpus is chunked TWO-TIER, so the queue must be asked at that depth.

    Scoped to the ETL tiers. A third tier now exists -- `external`, publications a live search
    discovered through the patent APIs and inserted -- and those legitimately sit in the queue
    with a title, an abstract and no chunks until the next chunking pass. That is the feature,
    not a backlog: it is how art found once becomes retrievable by vector next time. So the
    invariant is about what the ETL loaded, which is what resumability was ever about."""
    assert inc.unchunked_publication_ids(limit=5, two_tier=True,
                                         tiers=("core", "expanded")) == []


def test_textless_publications_are_excluded_from_the_queue():
    """210 publications (old DE-*-C) are biblio-only in BigQuery: no title, abstract, claims
    or description. They can never produce a chunk. If the queue did not exclude them they
    would be re-scanned and re-attempted on every weekly run forever, and the backlog
    counter would never reach zero -- masking genuine backlog."""
    textless = inc.textless_publication_count()
    assert textless > 0, "expected the known biblio-only DE publications to still be present"
    # they have no chunks...
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) c FROM publications p
                       WHERE NOT EXISTS (SELECT 1 FROM chunks ch WHERE ch.publication_id=p.id)""")
        no_chunks = cur.fetchone()["c"]
    assert no_chunks >= textless
    # ...yet the ETL work queue is empty, i.e. every chunkable loaded publication is chunked
    assert inc.unchunked_publication_ids(two_tier=True, tiers=("core", "expanded")) == []


def test_unchunked_query_respects_min_id_and_limit():
    ids = inc.unchunked_publication_ids(min_pub_id=0, limit=3)
    assert isinstance(ids, list) and len(ids) <= 3


# ---------------------------------------------------------------------------------------
# 3. cost guard
# ---------------------------------------------------------------------------------------
def test_cost_guard_refuses_over_ceiling(monkeypatch):
    monkeypatch.setattr(bqclient, "dry_run_gb", lambda sql: 2500.0)
    with pytest.raises(bqclient.CostCeilingExceeded) as e:
        bqclient.estimate_and_guard("SELECT 1", ceiling_gb=1800.0, label="delta", log=lambda m: None)
    assert e.value.est_gb == 2500.0
    assert e.value.ceiling_gb == 1800.0
    assert "Refusing to run" in str(e.value)


def test_cost_guard_allows_under_ceiling(monkeypatch):
    monkeypatch.setattr(bqclient, "dry_run_gb", lambda sql: 12.0)
    assert bqclient.estimate_and_guard("SELECT 1", 1800.0, "probe", log=lambda m: None) == 12.0


def test_cost_guard_boundary_is_inclusive(monkeypatch):
    """Exactly at the ceiling is allowed; a hair over is not."""
    monkeypatch.setattr(bqclient, "dry_run_gb", lambda sql: 1800.0)
    assert bqclient.estimate_and_guard("SELECT 1", 1800.0, "x", log=lambda m: None) == 1800.0
    monkeypatch.setattr(bqclient, "dry_run_gb", lambda sql: 1800.1)
    with pytest.raises(bqclient.CostCeilingExceeded):
        bqclient.estimate_and_guard("SELECT 1", 1800.0, "x", log=lambda m: None)


def test_run_guarded_sets_maximum_bytes_billed(monkeypatch):
    """Server-side belt to the dry-run's braces: BigQuery must abort the job itself if the
    real scan overshoots the estimate."""
    captured = {}

    class FakeJob:
        total_bytes_billed = 5 * 10**9
        def result(self): return []

    class FakeClient:
        def query(self, sql, job_config=None):
            captured["max_bytes"] = job_config.maximum_bytes_billed
            return FakeJob()

    monkeypatch.setattr(bqclient, "dry_run_gb", lambda sql: 4.0)
    monkeypatch.setattr(bqclient, "client", lambda: FakeClient())
    rows, est, billed = bqclient.run_guarded("SELECT 1", 50.0, "p", log=lambda m: None)
    assert captured["max_bytes"] == int(50.0 * 1e9)
    assert billed == 5.0


def test_usd_conversion():
    assert bqclient.usd(1000.0) == pytest.approx(bqclient.USD_PER_TB)


def test_default_ceiling_is_below_a_full_table_scan():
    """The source table is ~3.1 TB. The default ceiling must sit below a full scan so an
    accidental unfiltered query cannot sail through."""
    assert inc.MAX_EXTRACT_GB < 3000
    assert inc.MAX_PROBE_GB < 100


# ---------------------------------------------------------------------------------------
# freshness gating
# ---------------------------------------------------------------------------------------
def test_run_exits_cheaply_when_bigquery_is_not_ahead(monkeypatch, tmp_path):
    """The weekly cron's normal case: BigQuery has published nothing new, so the job must
    stop after the ~2 GB probe and never touch the expensive extract."""
    called = {"extract": False}
    monkeypatch.setattr(inc, "db_watermark", lambda: dt.date(2026, 4, 21))
    monkeypatch.setattr(inc, "bq_freshness", lambda log=print: (dt.date(2026, 4, 21), 2.0))
    monkeypatch.setattr(inc, "bq_delta_count",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not probe")))
    monkeypatch.setattr(bqclient, "run_ddl_guarded",
                        lambda *a, **k: called.__setitem__("extract", True))
    monkeypatch.setattr(inc, "STATEFILE", tmp_path / "state.json")

    out = inc.run(log=lambda m: None)
    assert out["status"] == "up_to_date"
    assert out["billed_gb"] == 2.0
    assert called["extract"] is False


def test_dry_run_makes_no_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(inc, "db_watermark", lambda: dt.date(2026, 4, 21))
    monkeypatch.setattr(inc, "bq_freshness", lambda log=print: (dt.date(2026, 6, 1), 2.0))
    monkeypatch.setattr(inc, "bq_delta_count",
                        lambda since, until=None, log=print: ({"n": 1234, "mn": 1, "mx": 2}, 17.7))
    monkeypatch.setattr(bqclient, "dry_run_gb", lambda sql: 1495.7)
    monkeypatch.setattr(bqclient, "run_ddl_guarded",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run wrote!")))
    monkeypatch.setattr(inc, "STATEFILE", tmp_path / "state.json")

    out = inc.run(dry_run=True, log=lambda m: None)
    assert out["status"] == "dry_run"
    assert out["candidates"] == 1234
    assert out["would_refuse"] is False
    assert out["billed_gb"] == pytest.approx(19.7)


def test_dry_run_reports_refusal_when_over_ceiling(monkeypatch, tmp_path):
    monkeypatch.setattr(inc, "db_watermark", lambda: dt.date(2026, 4, 21))
    monkeypatch.setattr(inc, "bq_freshness", lambda log=print: (dt.date(2026, 6, 1), 2.0))
    monkeypatch.setattr(inc, "bq_delta_count",
                        lambda since, until=None, log=print: ({"n": 5, "mn": 1, "mx": 2}, 17.7))
    monkeypatch.setattr(bqclient, "dry_run_gb", lambda sql: 9999.0)
    monkeypatch.setattr(inc, "STATEFILE", tmp_path / "state.json")
    out = inc.run(dry_run=True, max_gb=1800.0, log=lambda m: None)
    assert out["would_refuse"] is True


# ---------------------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------------------
def test_logger_does_not_double_write_under_cron(tmp_path, monkeypatch, capsys):
    """ops/refresh_corpus.sh redirects stdout into the log file. If the logger also wrote
    there directly, every cron line would be duplicated (it was, before this guard)."""
    p = tmp_path / "x.log"
    monkeypatch.setenv("DELTA_LOG_TO_FILE", "0")
    inc.Log(p)("hello")
    assert not p.exists()                       # nothing written directly
    assert "hello" in capsys.readouterr().out   # ...it went to stdout for the wrapper

    monkeypatch.setenv("DELTA_LOG_TO_FILE", "1")
    inc.Log(p)("world")
    assert p.read_text().count("world") == 1


def test_cron_wrapper_disables_direct_file_logging():
    """The wrapper and the logger must agree, or the log double-writes."""
    from pathlib import Path
    sh = Path(__file__).resolve().parent.parent / "ops" / "refresh_corpus.sh"
    body = sh.read_text()
    assert "DELTA_LOG_TO_FILE=0" in body
    assert ">> \"$LOG\" 2>&1" in body


def test_lock_is_exclusive(tmp_path):
    p = tmp_path / "l.lock"
    with inc.Lock(p):
        with pytest.raises(RuntimeError, match="refusing to run"):
            with inc.Lock(p):
                pass
    # released afterwards -> reacquirable
    with inc.Lock(p):
        pass


# ---------------------------------------------------------------------------------------
# chunk parity with the original whole-corpus chunker
# ---------------------------------------------------------------------------------------
def test_chunk_parity_with_existing_corpus():
    """Rebuild chunks for publications the ORIGINAL chunker already processed and assert we
    would produce exactly the same (kind, ref_id, text) rows.

    This is the anti-drift guard: build_chunk_rows() mirrors chunker.run(), and if the two
    ever diverge the delta would embed subtly different text than the rest of the corpus.
    """
    with db.cursor() as cur:
        # pick core publications that have claims, paragraphs AND figures -> exercises
        # every chunk kind, not just the easy ones
        cur.execute("""
            SELECT c.publication_id AS id
            FROM chunks c
            GROUP BY c.publication_id
            HAVING count(*) FILTER (WHERE c.kind='claim_own') > 0
               AND count(*) FILTER (WHERE c.kind='paragraph') > 0
               AND count(*) FILTER (WHERE c.kind='figure_caption') > 0
            ORDER BY c.publication_id
            LIMIT 5""")
        pub_ids = [r["id"] for r in cur.fetchall()]
        assert pub_ids, "no fully-populated publications found to compare against"

        rebuilt = inc.build_chunk_rows(cur, pub_ids)

        cur.execute("SELECT publication_id, kind, ref_id, text FROM chunks "
                    "WHERE publication_id = ANY(%s)", (pub_ids,))
        existing = cur.fetchall()

    def key(pid, kind, ref, text):
        return (pid, kind, ref, text)

    got = sorted(key(r[0], r[1], r[2], r[5]) for r in rebuilt)
    want = sorted(key(r["publication_id"], r["kind"], r["ref_id"], r["text"])
                  for r in existing)
    assert got == want, (
        f"delta chunker drifted from chunker.run(): "
        f"{len(got)} rebuilt vs {len(want)} existing")


def test_chunk_kinds_match_the_established_vocabulary():
    """The delta must only ever emit the six kinds the retrieval layer knows about."""
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT kind FROM chunks")
        corpus_kinds = {r["kind"] for r in cur.fetchall()}
        cur.execute("SELECT id FROM publications WHERE id IN "
                    "(SELECT publication_id FROM chunks LIMIT 200) LIMIT 5")
        pub_ids = [r["id"] for r in cur.fetchall()]
        rows = inc.build_chunk_rows(cur, pub_ids)
    assert corpus_kinds == {"whole", "abstract", "claim_own", "claim_resolved",
                            "paragraph", "figure_caption"}
    assert {r[1] for r in rows} <= corpus_kinds


def test_chunks_are_inserted_unembedded():
    """Chunks must land with embedding NULL so the 6 GB HNSW index does no work at insert
    time; embedding (and therefore all HNSW maintenance) happens in a separate throttled
    pass. Asserted on the COPY column list."""
    import inspect
    src = inspect.getsource(inc.chunk_publications)
    assert "COPY chunks (publication_id, kind, ref_id, coord, lang, text, " in src
    assert "embedding" not in src.split("COPY chunks (")[1].split(")")[0]


def test_embed_path_reuses_the_shared_embedder():
    """Embedding config must not be re-declared here -- it must come from embed.py, which
    is what the rest of the corpus used."""
    import inspect
    import embed
    src = inspect.getsource(inc.embed_pending)
    assert "embed.run(" in src
    assert embed.VERTEX_EMBED_MODEL == "gemini-embedding-001"
    from config import EMBED_DIM
    assert EMBED_DIM == 768


def test_verify_embeddings_detects_norm_drift(monkeypatch):
    """A wrong model/task_type yields same-shape vectors with a different magnitude. The
    verifier must refuse to certify such a batch."""
    calls = {"n": 0}

    class FakeCur:
        def execute(self, sql, params=None):
            calls["n"] += 1
        def fetchall(self):
            # first call = newest (drifted), second = oldest (baseline)
            return ([{"dims": 768, "avg_norm": 0.95, "n": 200}] if calls["n"] == 1
                    else [{"dims": 768, "avg_norm": 0.58, "n": 200}])

    import contextlib

    @contextlib.contextmanager
    def fake_cursor(*a, **k):
        yield FakeCur()

    monkeypatch.setattr(inc.db, "cursor", fake_cursor)
    with pytest.raises(RuntimeError, match="norm drift"):
        inc.verify_embeddings(log=lambda m: None)


def test_verify_embeddings_passes_on_the_live_corpus():
    """End-to-end sanity against the real DB: newest and oldest vectors agree."""
    out = inc.verify_embeddings(log=lambda m: None, sample=200)
    assert out["ok"] is True
    assert out["newest"][0]["dims"] == 768
    assert out["drift"] < inc.NORM_DRIFT_TOLERANCE


def test_queue_depth_must_match_chunker_depth():
    """The queue and the chunker have to agree about what counts as chunkable.

    Two-tier depth skips description paragraphs, so a publication whose ONLY text is a
    description yields zero chunks and — if the queue still counted it as having text — would be
    re-queued, re-scanned and re-attempted forever, exactly the failure the text predicate exists
    to prevent. The live corpus contains three such documents (pre-1950 US grants with description
    text but no title, abstract or claims), which is why the symptom was three ids rather than a
    flood and could easily have been dismissed as noise.

    Full depth SHOULD still queue them: at that depth their paragraphs do produce chunks.
    """
    ETL = ("core", "expanded")     # `external` rows are outstanding by design, see above
    two = inc.unchunked_publication_ids(two_tier=True, tiers=ETL)
    full = inc.unchunked_publication_ids(two_tier=False, tiers=ETL)
    assert two == [], f"two-tier queue must be drained, got {two[:5]}"
    assert set(two) <= set(full), "two-tier queue can only ever be a subset of full-depth"
    for pid in set(full) - set(two):
        with db.cursor() as cur:
            cur.execute("""SELECT coalesce(title,'') t, coalesce(abstract,'') a,
                             EXISTS (SELECT 1 FROM claims c WHERE c.publication_id=p.id) cl,
                             EXISTS (SELECT 1 FROM paragraphs g WHERE g.publication_id=p.id) pa
                           FROM publications p WHERE p.id=%s""", (pid,))
            r = cur.fetchone()
        assert not r["t"] and not r["a"] and not r["cl"], \
            f"pub {pid} has two-tier-chunkable text yet is missing from the two-tier queue"
        assert r["pa"], f"pub {pid} is queued at full depth but has no paragraphs to chunk"


def test_externally_discovered_publications_enter_the_work_queue():
    """Art a search FOUND must become art the next search can retrieve by vector.

    external.materialise inserts a publication with a title and an abstract and no chunks, so it
    is chunkable work the ordinary weekly pass will pick up. If the queue excluded tier
    'external', the corpus would accumulate rows that no dense channel could ever reach and the
    only way to find them again would be to run the same external query again.
    """
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM publications WHERE tier='external'")
        n_external = cur.fetchone()["c"]
    if not n_external:
        pytest.skip("no externally-discovered publications on this box yet")
    all_tiers = set(inc.unchunked_publication_ids(two_tier=True))
    etl_only = set(inc.unchunked_publication_ids(two_tier=True, tiers=("core", "expanded")))
    assert etl_only <= all_tiers
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM publications WHERE tier='external' "
                    "AND coalesce(abstract,'') <> '' "
                    "AND NOT EXISTS (SELECT 1 FROM chunks ch WHERE ch.publication_id=id)")
        chunkable = cur.fetchone()["c"]
    if chunkable:
        assert all_tiers - etl_only, "external rows with text must be queued for chunking"
