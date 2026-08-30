"""Apply sql/ migrations once, in order, and record what was applied.

Why this exists. `run.sh` applies exactly two files, `001_schema.sql` and `002_indexes.sql`, with
a raw `psql -f`, while files 003 through 008 had no applier. There was no record of what had run
against which database, so three workstreams independently wrote a `009_*.sql` in the same week
and nothing noticed. The live corpus database already had the objects from 001 through 008 with
no ledger, so the first thing any new runner must do is not make things worse. The formerly
unversioned `figure_images.sql` schema now lives in the legacy `001_schema.sql` baseline.

The design follows from that last point. Everything here is arranged so the tool refuses rather
than guesses:

* One transaction per file, so a file either lands whole or not at all.
* A Postgres advisory lock, so two deploys cannot both run DDL.
* A checksum per file, so editing an applied migration in place is caught instead of diverging.
* Numeric ordering, so 002 runs before 010.
* Duplicate version numbers and unversioned files are hard errors.
* An adoption is accepted only when every selected migration's objects are fully present.
* An empty ledger against a database that already has objects is NOT treated as a fresh install.
  If every migration's objects are present, it asks to be told to adopt. If only some are present,
  it reports exactly which and refuses, because there is no correct guess.

That last rule is not theoretical. `007_figure_compiler.sql` ends in a bare `CREATE TRIGGER`, which
has no `IF NOT EXISTS`, so replaying 007 raises. And `002_indexes.sql` builds the HNSW index, which
measured 94 GB on the live box. "Just re-run it and let IF NOT EXISTS sort it out" is wrong on both.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field

LOCK_NAMESPACE = 0x6D696772          # "migr"
LOCK_KEY = 1

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     text PRIMARY KEY,
  filename    text        NOT NULL,
  checksum    text        NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now(),
  applied_by  text        NOT NULL DEFAULT current_user,
  duration_ms integer,
  adopted     boolean     NOT NULL DEFAULT false
);
"""

VERSION_RE = re.compile(r"^(\d+)_.*\.sql$")


class MigrationError(RuntimeError):
    """Base for everything this module refuses to do."""


class DuplicateVersion(MigrationError):
    pass


class UnversionedFile(MigrationError):
    pass


class ChecksumDrift(MigrationError):
    pass


class BootstrapRequired(MigrationError):
    """The database already has every migration's objects but no ledger. Adopting is safe, but it
    is a decision a human makes, not one this tool makes for them."""


class BootstrapUndecidable(MigrationError):
    """Some migrations' objects are present and some are not, with no ledger to say which ran.
    Neither applying nor adopting is correct, so it reports and stops."""


@dataclass(frozen=True)
class Migration:
    version: str
    filename: str
    path: str
    checksum: str
    sql: str


@dataclass
class Result:
    applied: list = field(default_factory=list)
    adopted: list = field(default_factory=list)
    would_apply: list = field(default_factory=list)
    already: list = field(default_factory=list)


# --------------------------------------------------------------------------- discovery

def checksum(text):
    """Newlines are normalised so a file that only changed line endings is not called drift."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover(sql_dir):
    """Every migration in `sql_dir`, ordered by version NUMERICALLY.

    Refuses on an unversioned file or a duplicate version rather than skipping or picking one:
    both of those are how a migration silently never runs somewhere."""
    names = sorted(n for n in os.listdir(sql_dir) if n.endswith(".sql"))
    unversioned = [n for n in names if not VERSION_RE.match(n)]
    if unversioned:
        raise UnversionedFile(
            "these files have no NNN_ version prefix, so they have no place in the order: "
            + ", ".join(unversioned)
            + ". Give them a version or move them out of sql/.")

    out, seen = [], {}
    for n in names:
        v = VERSION_RE.match(n).group(1)
        numeric_version = int(v)
        if numeric_version in seen:
            raise DuplicateVersion(
                f"numeric version {numeric_version} is claimed by two files, "
                f"{seen[numeric_version]} and {n}. "
                "Renumber one before anything is applied.")
        seen[numeric_version] = n
        path = os.path.join(sql_dir, n)
        with open(path, encoding="utf-8") as migration_file:
            sql = migration_file.read()
        out.append(Migration(v, n, path, checksum(sql), sql))
    out.sort(key=lambda m: (int(m.version), m.filename))
    return out


# --------------------------------------------------------------------------- inspection

_OBJECT_PATTERNS = (
    # (regex, kind). Order matters only for readability.
    (re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.]*)", re.IGNORECASE), "table"),
    (re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
                r"([A-Za-z_][\w.]*)", re.IGNORECASE), "index"),
    (re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+([A-Za-z_][\w.]*)", re.IGNORECASE), "view"),
    (re.compile(r"ALTER\s+TABLE\s+([A-Za-z_][\w.]*)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                r"([A-Za-z_]\w*)", re.IGNORECASE), "column"),
    (re.compile(r"CREATE\s+TRIGGER\s+([A-Za-z_][\w.]*)", re.IGNORECASE), "trigger"),
    #  A migration that only tightens a CHECK creates no table, index or column, so without this
    #  it yields no probe at all and presence() can only answer "unknown" for ever. The constraint
    #  it adds must therefore carry a NAME THAT DID NOT EXIST BEFORE: probing a name the migration
    #  merely redefines would report it present before it had run, which is worse than no probe.
    (re.compile(r"ADD\s+CONSTRAINT\s+([A-Za-z_]\w*)", re.IGNORECASE), "constraint"),
)

# Statements with no IF NOT EXISTS form here, so a second run raises instead of no-opping.
_UNREPLAYABLE = (
    re.compile(r"CREATE\s+TRIGGER\s", re.IGNORECASE),
)


_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_comments(sql):
    """Comments out, before anything looks for DDL.

    `008_sources_docstore.sql` explains itself with '(CREATE TABLE IF NOT' at the end of one
    comment line and 'EXISTS);' at the start of the next. Scanning raw text matched across the
    line break and invented an object named literally 'IF'. A phantom object is never present, so
    it pinned 008 at 'partial' for ever, which would have blocked adoption permanently and for a
    reason nobody could see. Only sentinel extraction uses this; the SQL executed is always the
    original text."""
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))


def sentinels(sql):
    """Objects a migration creates, used to ask the database whether it already ran.

    Every migration must yield at least one, including `005_profile_and_notifications.sql`, which
    creates no table at all and only adds columns. A table-only probe would report 005 as absent
    for ever and try to replay it on every run."""
    sql = strip_comments(sql)
    found = []
    for rx, kind in _OBJECT_PATTERNS:
        for m in rx.finditer(sql):
            if kind == "column":
                found.append(("column", f"{m.group(1)}.{m.group(2)}"))
            else:
                found.append((kind, m.group(1)))
    seen, out = set(), []
    for s in found:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def is_replayable(sql):
    """False when re-running the file would raise rather than no-op."""
    body = strip_comments(sql)
    return not any(rx.search(body) for rx in _UNREPLAYABLE)


def _object_present(cur, kind, name):
    if kind in ("table", "view"):
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    elif kind == "index":
        cur.execute("SELECT count(*) > 0 FROM pg_class WHERE relkind='i' AND relname=%s", (name,))
    elif kind == "column":
        tbl, col = name.split(".", 1)
        cur.execute("SELECT count(*) > 0 FROM information_schema.columns "
                    "WHERE table_name=%s AND column_name=%s", (tbl, col))
    elif kind == "trigger":
        cur.execute("SELECT count(*) > 0 FROM pg_trigger WHERE tgname=%s AND NOT tgisinternal",
                    (name,))
    elif kind == "constraint":
        cur.execute("SELECT count(*) > 0 FROM pg_constraint WHERE conname=%s", (name,))
    else:
        return False
    return bool(cur.fetchone()[0])


def presence(conn, migration):
    """How much of this migration is already in the database: 'all', 'none' or 'partial'."""
    probes = sentinels(migration.sql)
    if not probes:
        return "unknown"
    with conn.cursor() as cur:
        hits = sum(1 for kind, name in probes if _object_present(cur, kind, name))
    if hits == 0:
        return "none"
    if hits == len(probes):
        return "all"
    return "partial"


# --------------------------------------------------------------------------- ledger + lock

def ensure_ledger(conn):
    with conn.cursor() as cur:
        cur.execute(LEDGER_DDL)
    conn.commit()


def take_lock(conn):
    """Session advisory lock. Released when the connection closes, so a killed deploy never leaves
    migrations permanently locked."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_KEY))
        return bool(cur.fetchone()[0])


def release_lock(conn):
    """Release the session lock even when a caller keeps the connection open.

    A failed DDL statement leaves the current transaction aborted. Roll it back first so the
    unlock query itself cannot be rejected and leave a library caller holding the lock."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, LOCK_KEY))
        unlocked = bool(cur.fetchone()[0])
    conn.commit()
    if not unlocked:
        raise MigrationError("the migration advisory lock was not held at release time")


def _recorded(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT version, filename, checksum FROM schema_migrations")
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


# --------------------------------------------------------------------------- apply

def _bootstrap_check(conn, pending):
    """Decide what an EMPTY ledger means, without guessing.

    Three cases, and only the first is safe to act on:
      every migration absent  -> a genuinely fresh database, apply normally
      every migration present -> already built, adopting is right but a human asks for it
      a mixture               -> undecidable, report which fell on each side and stop
    """
    states = {m.version: presence(conn, m) for m in pending}
    present = [v for v, s in states.items() if s == "all"]
    absent = [v for v, s in states.items() if s == "none"]
    partial = [v for v, s in states.items() if s in ("partial", "unknown")]

    if not present and not partial:
        return                                                  # fresh database

    lines = [f"  {m.version} {m.filename}: {states[m.version]}" for m in pending]
    detail = "\n".join(lines)

    if partial or (present and absent):
        raise BootstrapUndecidable(
            "the schema_migrations ledger is empty, but this database already contains some "
            "migrations' objects and not others. There is no safe guess, so nothing was applied.\n"
            f"{detail}\n"
            "Resolve by hand: confirm which migrations really ran, then record them with "
            "`migrate.py adopt --only <versions>`, or point this at the right database.")

    raise BootstrapRequired(
        "the schema_migrations ledger is empty, but every migration's objects are already "
        "present, so this database was built before the ledger existed. Nothing was applied.\n"
        f"{detail}\n"
        "If that is correct, record them without executing them: `migrate.py adopt`. "
        "Replaying them is NOT safe: 007_figure_compiler.sql ends in a bare CREATE TRIGGER, which "
        "raises on a second run, and 002_indexes.sql builds the 94 GB HNSW index.")


def _validate_adoption(conn, pending):
    """Adoption records history only when every selected migration is fully present.

    Saying ``adopt`` is an explicit request not to execute SQL. It is not permission to record an
    absent or partial migration as complete, because that would make every later run trust a lie.
    """
    states = {m.version: presence(conn, m) for m in pending}
    unsafe = [m for m in pending if states[m.version] != "all"]
    if not unsafe:
        return
    detail = "\n".join(
        f"  {m.version} {m.filename}: {states[m.version]}" for m in unsafe)
    raise BootstrapUndecidable(
        "adopt requires every selected migration to be fully present, but these are not:\n"
        f"{detail}\n"
        "Create or verify the missing objects first. Do not record a partial migration as applied.")


def apply(conn, sql_dir, dry_run=False, adopt=False, only=None, exclude=None):
    """Apply every pending migration, one transaction per file.

    `dry_run` touches nothing at all, not even the ledger. `adopt` records migrations as applied
    WITHOUT executing them, which is the only correct move on a database that predates the ledger.
    """
    migrations = discover(sql_dir)
    if only is not None and exclude is not None:
        raise MigrationError("--only and --exclude are mutually exclusive")
    selection = only if only is not None else exclude
    if selection is not None:
        flag = "--only" if only is not None else "--exclude"
        if not selection:
            raise MigrationError(f"{flag} requires at least one migration version")
        requested = set(selection)
        available = {m.version for m in migrations}
        unknown = sorted(requested - available, key=lambda v: (int(v) if v.isdigit() else 0, v))
        if unknown:
            raise MigrationError(
                f"{flag} names migration versions that do not exist: " + ", ".join(unknown))
        if only is not None:
            migrations = [m for m in migrations if m.version in requested]
        else:
            migrations = [m for m in migrations if m.version not in requested]
    res = Result()

    if dry_run:
        # Deliberately does not create the ledger: a dry run that writes is not a dry run.
        recorded = _recorded(conn) if _ledger_exists(conn) else {}
        for m in migrations:
            if m.version in recorded:
                _check_drift(m, recorded)
                res.already.append(m)
            else:
                res.would_apply.append(m)
        if not recorded and res.would_apply:
            _bootstrap_check(conn, res.would_apply)
        return res

    if not take_lock(conn):
        conn.rollback()
        raise MigrationError(
            "another migration run holds the advisory lock. Nothing was applied.")
    try:
        recorded = _recorded(conn) if _ledger_exists(conn) else {}
        for m in migrations:
            if m.version in recorded:
                _check_drift(m, recorded)
                res.already.append(m)
        pending = [m for m in migrations if m.version not in recorded]

        if not pending:
            return res

        if adopt:
            _validate_adoption(conn, pending)
        elif not recorded:
            _bootstrap_check(conn, pending)

        # Only create the ledger after every refusal check. Losing the lock, detecting legacy
        # ambiguity, or rejecting an unsafe adoption must leave the schema untouched.
        ensure_ledger(conn)

        if adopt:
            for m in pending:
                _record(conn, m, duration_ms=0, adopted=True)
                res.adopted.append(m)
            conn.commit()
            return res

        for m in pending:
            import time
            t0 = time.time()
            try:
                with conn.cursor() as cur:
                    cur.execute(m.sql)
                _record(conn, m, duration_ms=int((time.time() - t0) * 1000), adopted=False)
                conn.commit()
            except Exception:
                conn.rollback()                  # the whole file, including its ledger row
                raise
            res.applied.append(m)
        return res
    finally:
        release_lock(conn)


def _ledger_exists(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
        return bool(cur.fetchone()[0])


def _check_drift(m, recorded):
    recorded_filename, want = recorded[m.version]
    if recorded_filename != m.filename or want != m.checksum:
        raise ChecksumDrift(
            f"migration {m.version} has changed since it was applied.\n"
            f"  recorded: {recorded_filename}  {want}\n"
            f"  on disk:  {m.filename}  {m.checksum}\n"
            "An applied migration is history and must not be edited. Add a new one instead.")


def _record(conn, m, duration_ms, adopted):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (version, filename, checksum, duration_ms, adopted) "
            "VALUES (%s, %s, %s, %s, %s)",
            (m.version, m.filename, m.checksum, duration_ms, adopted))


# --------------------------------------------------------------------------- cli

def _connect():
    import psycopg
    env = os.environ.get("MIGRATE_ENV_FILE")
    if env:
        from dotenv import load_dotenv
        load_dotenv(env, override=False)
    missing = [k for k in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "missing connection settings: " + ", ".join(missing)
            + ". Set them, or point MIGRATE_ENV_FILE at the app's .env. "
              "There is deliberately no default password here.")
    return psycopg.connect(
        host=os.environ["PGHOST"], port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"], connect_timeout=30)


def main(argv=None):
    p = argparse.ArgumentParser(description="apply sql/ migrations once, in order")
    p.add_argument("command", choices=["status", "plan", "apply", "adopt"])
    p.add_argument("--sql-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql"))
    p.add_argument("--only", nargs="+", help="restrict to these versions")
    p.add_argument("--exclude", nargs="+", help="apply every version except these")
    a = p.parse_args(argv)

    conn = _connect()
    try:
        if a.command in ("status", "plan"):
            res = apply(conn, a.sql_dir, dry_run=True, only=a.only, exclude=a.exclude)
            for m in res.already:
                print(f"  applied  {m.version}  {m.filename}")
            for m in res.would_apply:
                print(f"  PENDING  {m.version}  {m.filename}")
            if not res.would_apply:
                print("nothing pending")
            return 0
        res = apply(conn, a.sql_dir, adopt=(a.command == "adopt"), only=a.only,
                    exclude=a.exclude)
        for m in res.applied:
            print(f"  applied  {m.version}  {m.filename}")
        for m in res.adopted:
            print(f"  adopted  {m.version}  {m.filename}  (recorded, not executed)")
        if not res.applied and not res.adopted:
            print("nothing to do")
        return 0
    except MigrationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
