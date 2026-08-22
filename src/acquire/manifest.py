"""The seam onto the niche manifest, and a provisional reader to run against until it lands.

Workstream B owns the niche manifest (vacuum / gripping / handling / lifting). At the time this
was written B had published nothing on `v3/B-corpus-manifest`: the branch does not exist on the
remote, and there is no `docs/` or `data/manifests/` contract to read. So this module defines ONE
small interface, ships a provisional reader behind it, and ships the reader for the file shape B
is most likely to publish. Swapping to B's contract is a new `ManifestReader` subclass and one
line in `open_reader()`; nothing in the worker, the pool or the cascade changes.

INCREMENTAL BY CONTRACT. `read()` takes an opaque cursor and returns the next batch plus the next
cursor. The cursor is persisted in `fulltext_manifest_cursor`, so seeding resumes where it stopped
and a manifest that grows is picked up by re-running the seeder rather than by rebuilding the pool.

THE PROVISIONAL READER, and why it is honest work rather than a placeholder. It reads the
publications the corpus ALREADY HOLDS in the seed CPC subgroups (`config.SEED_CPC`) that have no
claims and no paragraphs at all. Measured on the live corpus 2026-08-22: 78,061 publications carry
one of the eight seed symbols and 52,176 of them (66.8%) hold no text of any kind. Their
jurisdictions are CN 25,601, KR 7,484, JP 3,749, DE 3,426, TW 2,070, GB 1,197, FR 1,189, which is
the same CJK-heavy starvation the lexical tier is blind to. Whatever B's manifest turns out to be,
these publications are inside it.

It is keyset paginated on `publications.id` and driven by `ix_class_symbol`, so it never
sequentially scans the 5M row corpus. Verified with EXPLAIN: Bitmap Index Scan on ix_class_symbol,
0.3 s for the whole seed set.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from typing import ClassVar

import db

CURSOR_TABLE = "fulltext_manifest_cursor"

#  A publication that has no sibling text in the corpus is the one worth money: nothing else in
#  the corpus carries its disclosure. One that has a texted family sibling is still worth
#  fetching, but it goes behind, so a run that is stopped early has spent its budget on the
#  documents that were genuinely unreachable.
PRIORITY_NO_SIBLING_TEXT = 50
PRIORITY_HAS_SIBLING_TEXT = 150


@dataclass
class ManifestEntry:
    publication_number: str          # canonical form, e.g. CN101112759A
    family_id: str = ""
    country: str = ""
    priority: int = 100
    manifest: str = ""

    def as_dict(self) -> dict:
        return {"publication_number": self.publication_number, "family_id": self.family_id,
                "country": self.country, "priority": self.priority, "manifest": self.manifest}


class ManifestReader:
    """Read a niche manifest incrementally.

    `read(cursor, limit)` returns `(entries, next_cursor, exhausted)`. `exhausted` True means the
    manifest has nothing more RIGHT NOW; the seeder sleeps and asks again, because a manifest that
    is still being built keeps growing.
    """
    name = "base"

    def read(self, cursor: str, limit: int):
        raise NotImplementedError


# ---------------------------------------------------------------------------------------------
# the provisional reader: the niche the corpus already holds, minus the text it does not
# ---------------------------------------------------------------------------------------------
def _seed_symbols() -> list:
    import config
    extra = [s.strip() for s in os.environ.get("FULLTEXT_EXTRA_CPC", "").split(",") if s.strip()]
    return list(dict.fromkeys(list(config.SEED_CPC) + extra))


def canonical(pub: str) -> str:
    from sources.schema import canonical_pub
    return canonical_pub(pub)


class CorpusNicheReader(ManifestReader):
    """Provisional. Publications in the seed CPC subgroups that hold no text at all."""
    name = "corpus-niche"

    def __init__(self, symbols=None):
        self.symbols = list(symbols or _seed_symbols())

    def read(self, cursor: str, limit: int):
        after = int(cursor or 0)
        with db.cursor(autocommit=True, readonly=True) as cur:
            cur.execute(
                """WITH s AS (SELECT DISTINCT publication_id AS id FROM classifications
                              WHERE symbol = ANY(%s))
                   SELECT p.id, p.publication_number, p.kind_code, p.country,
                          COALESCE(p.simple_family_id, p.extended_family_id, '') AS family_id,
                          EXISTS (SELECT 1 FROM publications q
                                    JOIN claims c2 ON c2.publication_id = q.id
                                   WHERE q.simple_family_id = p.simple_family_id
                                     AND p.simple_family_id IS NOT NULL
                                     AND q.id <> p.id) AS sibling_text
                     FROM s JOIN publications p ON p.id = s.id
                    WHERE p.id > %s
                      AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.publication_id = p.id)
                      AND NOT EXISTS (SELECT 1 FROM paragraphs g WHERE g.publication_id = p.id)
                    ORDER BY p.id LIMIT %s""",
                (self.symbols, after, int(limit)))
            rows = cur.fetchall() or []
        entries = []
        last = after
        for r in rows:
            last = int(r["id"])
            pn = canonical(r["publication_number"] or "")
            if not pn:
                continue
            entries.append(ManifestEntry(
                publication_number=pn,
                family_id=str(r["family_id"] or ""),
                country=str(r["country"] or "")[:4],
                priority=(PRIORITY_HAS_SIBLING_TEXT if r["sibling_text"]
                          else PRIORITY_NO_SIBLING_TEXT),
                manifest=self.name))
        return entries, str(last), len(rows) < int(limit)


# ---------------------------------------------------------------------------------------------
# the reader for a published file manifest (workstream B's expected shape)
# ---------------------------------------------------------------------------------------------
_PUB_KEYS = ("publication_number", "publication", "pub_number", "pub", "publication_id", "id")
_FAM_KEYS = ("family_id", "simple_family_id", "family", "docdb_family_id", "extended_family_id")


class JsonlManifestReader(ManifestReader):
    """One JSON object per line, over one file or a sorted glob of them.

    Deliberately generous about field names: `publication_number`, `publication`, `pub_number`,
    `pub`; `family_id`, `simple_family_id`, `family`, `docdb_family_id`. A manifest that names the
    same thing with a different key is not a reason to stop.

    Cursor is `"<file index>:<line offset>"`, so an appended line in the last file is picked up on
    the next pass and a file added after it is picked up after that.
    """
    name = "jsonl"

    def __init__(self, spec: str):
        self.spec = spec
        self.name = f"jsonl:{os.path.basename(spec.rstrip('/')) or spec}"

    def files(self) -> list:
        if os.path.isdir(self.spec):
            return sorted(glob.glob(os.path.join(self.spec, "*.jsonl"))
                          + glob.glob(os.path.join(self.spec, "*.json")))
        if any(ch in self.spec for ch in "*?["):
            return sorted(glob.glob(self.spec))
        return [self.spec] if os.path.exists(self.spec) else []

    @staticmethod
    def entry_from(obj: dict, manifest_name: str):
        if not isinstance(obj, dict):
            return None
        pn = ""
        for k in _PUB_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                pn = canonical(v)
                break
        if not pn:
            return None
        fam = ""
        for k in _FAM_KEYS:
            v = obj.get(k)
            if v not in (None, ""):
                fam = str(v)
                break
        pr = obj.get("priority")
        try:
            pr = int(pr)
        except (TypeError, ValueError):
            pr = PRIORITY_NO_SIBLING_TEXT
        return ManifestEntry(publication_number=pn, family_id=fam,
                             country=str(obj.get("country") or pn[:2])[:4],
                             priority=pr, manifest=manifest_name)

    def read(self, cursor: str, limit: int):
        files = self.files()
        if not files:
            return [], cursor or "0:0", True
        try:
            fi, off = (int(x) for x in (cursor or "0:0").split(":", 1))
        except ValueError:
            fi, off = 0, 0
        entries = []
        while fi < len(files) and len(entries) < limit:
            with open(files[fi], "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i < off:
                        continue
                    off = i + 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    e = self.entry_from(obj, self.name)
                    if e is not None:
                        entries.append(e)
                    if len(entries) >= limit:
                        break
                else:
                    #  File exhausted: advance to the next one, offset back to zero.
                    fi, off = fi + 1, 0
                    continue
            break
        exhausted = fi >= len(files) and len(entries) < limit
        return entries, f"{fi}:{off}", exhausted


class NicheDatabaseReader(ManifestReader):
    """Read one preferred incomplete publication per family from isolated staging.

    The durable cursor walks the staging manifest primary key. Family selection is restricted to
    the family keys in that bounded page and uses the family index, so it never scans or sorts the
    full manifest. A family seen on two pages offers the same preferred publication twice, which
    the acquisition queue primary key deduplicates without another provider call.
    """

    name = "niche-db:niche_full_v1"
    PRIORITIES: ClassVar[dict[int, int]] = {1: 50, 2: 60, 3: 70, 4: 150}

    PAGE_SQL = """
        SELECT publication_id, publication_number, family_id, authority, priority,
               has_complete_claims, has_complete_description, updated_at
          FROM niche_corpus.niche_publications
         WHERE (updated_at, publication_id) > (%s::timestamptz, %s)
         ORDER BY updated_at, publication_id
         LIMIT %s
    """
    FAMILY_TARGET_SQL = """
        SELECT DISTINCT ON (p.family_id)
               p.publication_number, p.family_id, p.authority,
               min(p.priority) OVER (PARTITION BY p.family_id) AS family_priority
          FROM niche_corpus.niche_publications AS p
         WHERE p.family_id = ANY(%s)
           AND NOT (p.has_complete_claims AND p.has_complete_description)
           AND NOT EXISTS (
               SELECT 1
                 FROM niche_corpus.niche_publications AS complete
                WHERE complete.family_id = p.family_id
                  AND complete.has_complete_claims
                  AND complete.has_complete_description
           )
         ORDER BY p.family_id,
                  (p.has_complete_claims::integer +
                   p.has_complete_description::integer) DESC,
                  (lower(COALESCE(p.language, '')) = 'en') DESC,
                  (p.has_claims::integer + p.has_description::integer) DESC,
                  (COALESCE(p.kind_code, '') LIKE 'A%%') DESC,
                  p.priority,
                  p.publication_id
    """

    def __init__(
        self,
        *,
        connection_factory=None,
        expected_database: str | None = None,
        fingerprint: str | None = None,
    ):
        dsn = str(os.environ.get("NICHE_PARSE_DATABASE_URL") or "").strip()
        if connection_factory is None:
            if not dsn:
                raise RuntimeError("NICHE_PARSE_DATABASE_URL is required for niche-db")
            from psycopg import connect
            from psycopg.rows import dict_row

            def connection_factory():
                return connect(
                    dsn,
                    row_factory=dict_row,
                    application_name="niche-fetch-seed",
                    connect_timeout=10,
                )

        self.connection_factory = connection_factory
        self.expected_database = str(
            expected_database or os.environ.get("NICHE_EXPECTED_DATABASE") or ""
        ).strip()
        self.fingerprint = str(
            fingerprint or os.environ.get("NICHE_DATABASE_FINGERPRINT") or ""
        ).strip()
        if not self.expected_database or not self.fingerprint:
            raise RuntimeError("niche-db requires its expected database name and fingerprint")

    @classmethod
    def _priority(cls, value) -> int:
        try:
            priority = min(4, max(1, int(value or 4)))
        except (TypeError, ValueError):
            priority = 4
        return cls.PRIORITIES[priority]

    def read(self, cursor: str, limit: int):
        page_size = min(10_000, max(1, int(limit)))
        try:
            after_time, after_publication = str(cursor or "").split("|", 1)
        except ValueError:
            after_time, after_publication = "1970-01-01T00:00:00+00:00", ""
        with self.connection_factory() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            with connection.cursor() as db_cursor:
                db_cursor.execute(
                    "SELECT current_database() AS database_name, identity.fingerprint "
                    "FROM niche_corpus.pipeline_identity AS identity "
                    "WHERE identity.singleton=true"
                )
                identity = db_cursor.fetchone() or {}
                if str(identity.get("database_name") or "") != self.expected_database:
                    raise RuntimeError("niche-db database name mismatch")
                if str(identity.get("fingerprint") or "") != self.fingerprint:
                    raise RuntimeError("niche-db database fingerprint mismatch")
                db_cursor.execute(
                    self.PAGE_SQL, (after_time, after_publication, page_size)
                )
                page = [dict(row) for row in db_cursor.fetchall()]
                families = sorted({
                    str(row.get("family_id") or "")
                    for row in page
                    if row.get("family_id")
                })
                targets = []
                if families:
                    db_cursor.execute(self.FAMILY_TARGET_SQL, (families,))
                    targets = [dict(row) for row in db_cursor.fetchall()]

        entries = [
            ManifestEntry(
                publication_number=canonical(row.get("publication_number") or ""),
                family_id=str(row.get("family_id") or ""),
                country=str(row.get("authority") or "")[:4],
                priority=self._priority(row.get("family_priority")),
                manifest=self.name,
            )
            for row in targets
            if canonical(row.get("publication_number") or "")
        ]
        entries.extend(
            ManifestEntry(
                publication_number=canonical(row.get("publication_number") or ""),
                family_id="",
                country=str(row.get("authority") or "")[:4],
                priority=self._priority(row.get("priority")),
                manifest=self.name,
            )
            for row in page
            if not row.get("family_id")
            and not (
                row.get("has_complete_claims") and row.get("has_complete_description")
            )
            and canonical(row.get("publication_number") or "")
        )
        deduplicated = {entry.publication_number: entry for entry in entries}
        next_cursor = (
            f"{page[-1]['updated_at'].isoformat()}|{page[-1]['publication_id']}"
            if page else str(cursor or "")
        )
        return list(deduplicated.values()), next_cursor, len(page) < page_size


def open_reader(spec: str = "") -> ManifestReader:
    """`spec` is `FULLTEXT_MANIFEST` or the `--manifest` flag.

    `corpus-niche` (the default) is the provisional reader. Anything else is treated as a path,
    a glob or a directory of JSON lines, which is the shape workstream B's manifest is expected
    to take. When B publishes a different contract, add its reader here.
    """
    spec = spec or os.environ.get("FULLTEXT_MANIFEST", "corpus-niche")
    if spec in ("corpus-niche", "", "default"):
        return CorpusNicheReader()
    if spec == "niche-db":
        return NicheDatabaseReader()
    return JsonlManifestReader(spec)


# ---------------------------------------------------------------------------------------------
# cursor persistence
# ---------------------------------------------------------------------------------------------
def get_cursor(reader_name: str) -> str:
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute(f"SELECT cursor FROM {CURSOR_TABLE} WHERE reader=%s", (reader_name,))
        row = cur.fetchone()
    return str(row["cursor"]) if row else ""


def set_cursor(reader_name: str, cursor: str, seeded_delta: int = 0) -> None:
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO {CURSOR_TABLE} (reader, cursor, seeded) VALUES (%s,%s,%s) "
            f"ON CONFLICT (reader) DO UPDATE SET cursor=EXCLUDED.cursor, "
            f"seeded = {CURSOR_TABLE}.seeded + EXCLUDED.seeded, updated_at=now()",
            (reader_name, str(cursor), int(seeded_delta)))


def reset_cursor(reader_name: str) -> None:
    with db.cursor() as cur:
        cur.execute(f"DELETE FROM {CURSOR_TABLE} WHERE reader=%s", (reader_name,))
