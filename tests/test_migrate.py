"""Tests for the migration runner, written before it existed.

They run against a THROWAWAY database created and dropped by the tests themselves, never against
the live corpus. Real Postgres is used deliberately rather than a fake: the three things most worth
proving here, one transaction per file, an advisory lock, and whether a given DDL statement is
actually replayable, are exactly the things a fake connection would get wrong.

Skips cleanly if no local Postgres is reachable.
"""
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import migrate

psycopg = pytest.importorskip("psycopg")

ADMIN = {"host": "127.0.0.1", "port": 5432, "user": "deep", "dbname": "deep_research"}


def _admin_password():
    try:
        out = subprocess.run(
            ["docker", "inspect", "deep-research-postgres", "--format",
             "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=20, check=False).stdout
    except Exception:                                                # noqa: BLE001
        return None
    for line in out.splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            return line.split("=", 1)[1]
    return None


@pytest.fixture(scope="session")
def pw():
    p = _admin_password()
    if not p:
        pytest.skip("no local postgres credentials")
    try:
        psycopg.connect(connect_timeout=6, password=p, **ADMIN).close()
    except Exception as exc:                                         # noqa: BLE001
        pytest.skip(f"local postgres unreachable: {exc}")
    return p


@pytest.fixture
def db(pw):
    """A brand new empty database per test. Dropped afterwards."""
    testdb = f"patents_migrate_test_{uuid.uuid4().hex[:12]}"
    adm = psycopg.connect(autocommit=True, password=pw, **ADMIN)
    with adm.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{testdb}"')
    adm.close()
    dsn = dict(ADMIN, dbname=testdb, password=pw)
    yield dsn
    adm = psycopg.connect(autocommit=True, password=pw, **ADMIN)
    with adm.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{testdb}" WITH (FORCE)')
    adm.close()


def conn(dsn):
    return psycopg.connect(**dsn)


@pytest.fixture
def sqldir(tmp_path):
    d = tmp_path / "sql"
    d.mkdir()
    return d


def write(d, name, body):
    (d / name).write_text(textwrap.dedent(body), encoding="utf-8")


# --------------------------------------------------------------------------- ordering

def test_ordering_is_numeric_not_lexicographic(sqldir):
    """Sorting filenames as strings puts 010 before 002 and the schema is built backwards.
    The renumbering plan introduces 010 and 011, so this is about to matter."""
    for n in ("001_a.sql", "002_b.sql", "010_c.sql", "011_d.sql"):
        write(sqldir, n, "SELECT 1;")
    got = [m.version for m in migrate.discover(str(sqldir))]
    assert got == ["001", "002", "010", "011"]


def test_duplicate_version_numbers_are_refused(sqldir):
    """Two people numbered a migration 009 in the same week. Three, in fact."""
    write(sqldir, "009_durable_runs.sql", "SELECT 1;")
    write(sqldir, "009_corpus_release.sql", "SELECT 1;")
    with pytest.raises(migrate.DuplicateVersion) as e:
        migrate.discover(str(sqldir))
    assert "009" in str(e.value)


def test_numeric_alias_versions_are_duplicates(sqldir):
    """009 and 9 are the same numeric migration version, even if their strings differ."""
    write(sqldir, "009_durable_runs.sql", "SELECT 1;")
    write(sqldir, "9_alias.sql", "SELECT 1;")
    with pytest.raises(migrate.DuplicateVersion):
        migrate.discover(str(sqldir))


def test_an_unversioned_file_is_refused(sqldir):
    """sql/figure_images.sql really is in the repo with no version prefix. Silently skipping it
    is how a table goes missing on a fresh install."""
    write(sqldir, "001_a.sql", "SELECT 1;")
    write(sqldir, "figure_images.sql", "SELECT 1;")
    with pytest.raises(migrate.UnversionedFile) as e:
        migrate.discover(str(sqldir))
    assert "figure_images.sql" in str(e.value)


# --------------------------------------------------------------------------- ledger

def test_ledger_records_filename_checksum_and_applied_at(db, sqldir):
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int);")
    with conn(db) as c:
        migrate.apply(c, str(sqldir))
    with conn(db) as c, c.cursor() as cur:
        cur.execute("SELECT version, filename, checksum, applied_at FROM schema_migrations")
        row = cur.fetchone()
    assert row[0] == "001"
    assert row[1] == "001_a.sql"
    assert len(row[2]) == 64                       # sha256 hex
    assert row[3] is not None


def test_checksum_drift_is_refused(db, sqldir):
    """Editing an applied migration in place is the classic way two environments silently
    diverge."""
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int);")
    with conn(db) as c:
        migrate.apply(c, str(sqldir))
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int, extra text);")
    with conn(db) as c, pytest.raises(migrate.ChecksumDrift) as e:
        migrate.apply(c, str(sqldir))
    assert "001" in str(e.value)


def test_renaming_an_applied_migration_is_drift():
    """The ledger records a filename so a rename must not be silently ignored."""
    m = migrate.Migration("001", "001_new_name.sql", "/tmp/unused", "same", "SELECT 1;")
    with pytest.raises(migrate.ChecksumDrift):
        migrate._check_drift(m, {"001": ("001_old_name.sql", "same")})


def test_apply_is_idempotent(db, sqldir):
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int);")
    with conn(db) as c:
        first = migrate.apply(c, str(sqldir))
    with conn(db) as c:
        second = migrate.apply(c, str(sqldir))
    assert [m.version for m in first.applied] == ["001"]
    assert second.applied == []


# --------------------------------------------------------------------------- safety

def test_dry_run_changes_nothing(db, sqldir):
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int);")
    with conn(db) as c:
        res = migrate.apply(c, str(sqldir), dry_run=True)
    assert [m.version for m in res.would_apply] == ["001"]
    with conn(db) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass('t_a') IS NULL AS absent, "
                    "to_regclass('schema_migrations') IS NULL AS no_ledger")
        absent, no_ledger = cur.fetchone()
    assert absent, "dry run created the table"
    assert no_ledger, "dry run created the ledger"


def test_dry_run_refuses_to_call_a_legacy_schema_pending(monkeypatch):
    """A plan against an unrecorded legacy schema must not claim that present DDL is pending."""
    m = migrate.Migration("001", "001_a.sql", "/tmp/unused", "sum", "CREATE TABLE a(id int);")
    monkeypatch.setattr(migrate, "discover", lambda _path: [m])
    monkeypatch.setattr(migrate, "_ledger_exists", lambda _conn: False)
    monkeypatch.setattr(migrate, "presence", lambda _conn, _migration: "all")
    with pytest.raises(migrate.BootstrapRequired):
        migrate.apply(object(), "unused", dry_run=True)


def test_only_refuses_unknown_and_empty_version_sets(monkeypatch, sqldir):
    """A typo in --only must not turn a requested migration into a successful no-op."""
    write(sqldir, "001_a.sql", "CREATE TABLE a(id int);")
    monkeypatch.setattr(migrate, "_ledger_exists", lambda _conn: False)
    with pytest.raises(migrate.MigrationError):
        migrate.apply(object(), str(sqldir), dry_run=True, only=["099"])
    with pytest.raises(migrate.MigrationError):
        migrate.apply(object(), str(sqldir), dry_run=True, only=[])


def test_exclude_is_exact_and_cannot_mix_with_only(monkeypatch, sqldir):
    write(sqldir, "001_schema.sql", "CREATE TABLE a(id int);")
    write(sqldir, "002_indexes.sql", "CREATE INDEX a_id ON a(id);")
    monkeypatch.setattr(migrate, "_ledger_exists", lambda _conn: False)
    monkeypatch.setattr(migrate, "presence", lambda _conn, _migration: "none")
    result = migrate.apply(object(), str(sqldir), dry_run=True, exclude=["002"])
    assert [m.version for m in result.would_apply] == ["001"]
    with pytest.raises(migrate.MigrationError):
        migrate.apply(object(), str(sqldir), dry_run=True, exclude=["099"])
    with pytest.raises(migrate.MigrationError):
        migrate.apply(object(), str(sqldir), dry_run=True, only=["001"], exclude=["002"])


def test_run_sh_uses_the_migration_runner_without_a_password_literal():
    with open(os.path.join(ROOT, "run.sh"), encoding="utf-8") as run_script:
        script = run_script.read()
    assert "PGPASSWORD=" not in script
    assert "src/migrate.py apply --exclude 002" in script
    assert "src/migrate.py apply --only 002" in script
    assert 'PY="$ROOT/.venv/bin/python"' in script


def test_one_transaction_per_file_rolls_back_the_whole_file(db, sqldir):
    """A file that half applies leaves a schema nobody can reason about."""
    write(sqldir, "001_ok.sql", "CREATE TABLE t_ok (id int);")
    write(sqldir, "002_bad.sql", """
        CREATE TABLE t_partial (id int);
        SELECT this_function_does_not_exist();
    """)
    with conn(db) as c, pytest.raises(psycopg.errors.UndefinedFunction):
        migrate.apply(c, str(sqldir))
    with conn(db) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass('t_ok'), to_regclass('t_partial')")
        ok, partial = cur.fetchone()
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
    assert ok is not None, "the good file before the bad one was rolled back too"
    assert partial is None, "the failing file left half its objects behind"
    assert versions == ["001"], "the failing file was recorded as applied"


def test_a_second_migrator_is_refused_the_lock(db, sqldir):
    """Two deploys at once must not both run the same DDL."""
    write(sqldir, "001_a.sql", "SELECT pg_sleep(2);")
    holder = conn(db)
    migrate.ensure_ledger(holder)
    assert migrate.take_lock(holder) is True
    other = conn(db)
    try:
        assert migrate.take_lock(other) is False, "a second migrator got the lock"
    finally:
        other.close()
        holder.close()


def test_lock_is_taken_before_the_ledger_can_be_created(monkeypatch):
    """Losing lock contention must not mutate the schema by creating the ledger first."""
    calls = []

    class Dummy:
        def rollback(self):
            calls.append("rollback")

    monkeypatch.setattr(migrate, "discover", lambda _path: [])
    monkeypatch.setattr(migrate, "take_lock", lambda _conn: calls.append("lock") or False)
    monkeypatch.setattr(migrate, "ensure_ledger", lambda _conn: calls.append("ledger"))
    with pytest.raises(migrate.MigrationError):
        migrate.apply(Dummy(), "unused")
    assert calls == ["lock", "rollback"]


def test_apply_releases_its_session_lock_on_return(monkeypatch):
    """Library callers may keep the connection alive after apply(), so the lock cannot leak."""
    calls = []

    class Dummy:
        pass

    monkeypatch.setattr(migrate, "discover", lambda _path: [])
    monkeypatch.setattr(migrate, "take_lock", lambda _conn: True)
    monkeypatch.setattr(migrate, "_ledger_exists", lambda _conn: False)
    monkeypatch.setattr(migrate, "release_lock", lambda _conn: calls.append("release"))
    migrate.apply(Dummy(), "unused")
    assert calls == ["release"]


# --------------------------------------------------------------------------- bootstrap

def test_a_fresh_database_applies_everything(db, sqldir):
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int);")
    write(sqldir, "002_b.sql", "CREATE TABLE t_b (id int);")
    with conn(db) as c:
        res = migrate.apply(c, str(sqldir))
    assert [m.version for m in res.applied] == ["001", "002"]


def test_an_empty_ledger_with_every_object_present_refuses_without_adopt(db, sqldir):
    """The live corpus box: eight migrations' worth of tables, and no ledger. Replaying them is
    not safe, because 007's CREATE TRIGGER is not idempotent."""
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int);")
    write(sqldir, "002_b.sql", "CREATE TABLE t_b (id int);")
    with conn(db) as c, c.cursor() as cur:                 # pre-create, no ledger
        cur.execute("CREATE TABLE t_a (id int); CREATE TABLE t_b (id int);")
        c.commit()
    with conn(db) as c, pytest.raises(migrate.BootstrapRequired):
        migrate.apply(c, str(sqldir))


def test_an_empty_ledger_with_only_some_objects_present_is_undecidable(db, sqldir):
    """Half applied and unrecorded. There is no correct guess, so it must refuse and say so."""
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int);")
    write(sqldir, "002_b.sql", "CREATE TABLE t_b (id int);")
    with conn(db) as c, c.cursor() as cur:
        cur.execute("CREATE TABLE t_a (id int);")          # 001 present, 002 absent
        c.commit()
    with conn(db) as c, pytest.raises(migrate.BootstrapUndecidable) as e:
        migrate.apply(c, str(sqldir))
    msg = str(e.value)
    assert "001" in msg and "002" in msg, "the report must name which side each migration fell"


def test_adopt_records_without_executing(db, sqldir):
    """Adopting must not run the SQL: on the live box that would rebuild a 94 GB index."""
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int);")
    write(sqldir, "002_b.sql", "CREATE TABLE t_b (id int); INSERT INTO t_b VALUES (1);")
    with conn(db) as c, c.cursor() as cur:
        cur.execute("CREATE TABLE t_a (id int); CREATE TABLE t_b (id int);")
        c.commit()
    with conn(db) as c:
        res = migrate.apply(c, str(sqldir), adopt=True)
    assert [m.version for m in res.adopted] == ["001", "002"]
    with conn(db) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM t_b")
        assert cur.fetchone()[0] == 0, "adopt executed the SQL instead of recording it"
        cur.execute("SELECT count(*) FROM schema_migrations")
        assert cur.fetchone()[0] == 2


def test_adopt_refuses_a_target_that_is_not_fully_present(monkeypatch):
    """An explicit adopt is not permission to lie about absent or partial database objects."""
    m = migrate.Migration("002", "002_b.sql", "/tmp/unused", "sum", "CREATE TABLE b(id int);")
    monkeypatch.setattr(migrate, "presence", lambda _conn, _migration: "partial")
    with pytest.raises(migrate.BootstrapUndecidable) as exc:
        migrate._validate_adoption(object(), [m])
    assert "002" in str(exc.value) and "partial" in str(exc.value)


def test_never_replays_001_or_002_blindly_on_an_unrecorded_database(db, sqldir):
    """The explicit instruction, as a test. Whatever the runner decides, it must not execute the
    body of 001 or 002 when the ledger is empty and objects already exist."""
    write(sqldir, "001_a.sql", "CREATE TABLE t_a (id int); INSERT INTO t_a VALUES (1);")
    write(sqldir, "002_b.sql", "CREATE TABLE t_b (id int); INSERT INTO t_b VALUES (1);")
    with conn(db) as c, c.cursor() as cur:
        cur.execute("CREATE TABLE t_a (id int); CREATE TABLE t_b (id int);")
        c.commit()
    with conn(db) as c, pytest.raises(
            (migrate.BootstrapRequired, migrate.BootstrapUndecidable)):
        migrate.apply(c, str(sqldir))
    with conn(db) as c, c.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM t_a) + (SELECT count(*) FROM t_b)")
        assert cur.fetchone()[0] == 0, "the migration bodies were replayed"


# --------------------------------------------------------------------------- the real repo

def test_the_repo_migrations_are_discoverable_and_include_figure_images():
    """Every schema asset must have one deterministic place in the numbered history.

    This used to assert the literal list 001 through 009, which made it a tripwire that every
    workstream adding a migration trips at once for a reason that has nothing to do with their
    change. What actually has to hold is the property: discovery finds exactly the numbered files
    on disk, in ascending numeric order, with no duplicate version, and the adopted history 001
    through 009 is still there and still in front.
    """
    real = os.path.join(ROOT, "sql")
    migrations = migrate.discover(real)
    found = [m.version for m in migrations]

    on_disk = sorted(
        (name for name in os.listdir(real) if migrate.VERSION_RE.match(name)),
        key=lambda name: int(migrate.VERSION_RE.match(name).group(1)),
    )
    assert [m.filename for m in migrations] == on_disk, "discovery must find every numbered file"
    assert found == sorted(found, key=int), "order must be numeric, so 002 runs before 010"
    assert len(set(found)) == len(found), "a duplicate version number is never a coincidence"

    adopted_history = [f"{n:03d}" for n in range(1, 10)]
    assert found[: len(adopted_history)] == adopted_history, (
        "001 through 009 are recorded in the live ledger and must keep their numbers")

    first = next(m for m in migrations if m.version == "001")
    assert ("table", "figure_images") in migrate.sentinels(first.sql)


def test_sentinels_are_found_for_every_numbered_repo_migration():
    """005 adds columns and creates no table, so a table-only sentinel would call it 'absent' for
    ever and try to replay it. Every migration must yield at least one probe."""
    real = os.path.join(ROOT, "sql")
    for name in sorted(os.listdir(real)):
        if not name[:3].isdigit():
            continue
        with open(os.path.join(real, name), encoding="utf-8") as migration_file:
            sql = migration_file.read()
        assert migrate.sentinels(sql), f"{name} has no detectable object"


def test_the_007_trigger_is_known_to_be_unreplayable():
    """CREATE TRIGGER has no IF NOT EXISTS here, so 007 raises on a second run. This is the
    evidence for refusing to replay rather than 'just trying it'."""
    real = os.path.join(ROOT, "sql", "007_figure_compiler.sql")
    with open(real, encoding="utf-8") as migration_file:
        sql = migration_file.read()
    assert "CREATE TRIGGER" in sql
    assert "CREATE OR REPLACE TRIGGER" not in sql
    assert not migrate.is_replayable(sql)


# --------------------------------------------------------------------------- comment handling

def test_ddl_mentioned_in_a_comment_is_not_a_sentinel():
    """sql/008_sources_docstore.sql has a comment reading '(CREATE TABLE IF NOT' / 'EXISTS);'
    across two lines. Scanning raw text invented an object literally named 'IF', which is never
    present, which pins 008 at 'partial' for ever and blocks adoption permanently."""
    sql = textwrap.dedent("""
        -- The package also creates this table itself on first use (CREATE TABLE IF NOT
        -- EXISTS); this file records the schema in the migration set.
        CREATE TABLE IF NOT EXISTS sources_docstore (id int);
    """)
    names = [n for _, n in migrate.sentinels(sql)]
    assert names == ["sources_docstore"], names


def test_block_comments_are_stripped_too():
    sql = "/* CREATE TABLE ghost (id int); */ CREATE TABLE real_one (id int);"
    assert [n for _, n in migrate.sentinels(sql)] == ["real_one"]


def test_the_real_008_yields_exactly_one_table():
    path = os.path.join(ROOT, "sql", "008_sources_docstore.sql")
    with open(path, encoding="utf-8") as migration_file:
        sql = migration_file.read()
    assert [n for k, n in migrate.sentinels(sql) if k == "table"] == ["sources_docstore"]
