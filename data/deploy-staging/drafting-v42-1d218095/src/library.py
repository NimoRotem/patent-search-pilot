"""A personal library of publications kept across searches.

Triage flags already exist, but they belong to ONE report: mark a reference Relevant, run a
different search next week, and it is gone. That is the wrong lifetime for the thing being
recorded. When somebody flags a document it is because the DOCUMENT matters to them — it is the
closest art they have seen in this field, or the one the client keeps asking about — and that
outlives the search that happened to surface it.

So this is a per-user list of publications, each carrying a note and a record of which search it
came from, addressable on its own page and re-usable as the reference set for a later draft.

Deliberately thin: it stores the publication NUMBER and the user's own note, nothing else. Title,
dates, assignee and drawings are already resolvable from the corpus and the display cache, and
copying them in here would create a second, staler answer to "what is US-11338449-B2".
"""
from __future__ import annotations

import re
import threading

import db

MAX_NOTE_CHARS = 4000
MAX_TAG_CHARS = 120
MAX_ROWS_RETURNED = 500
# The corpus canonical spelling: 'US-11338449-B2'. Anything that is not a publication number is
# refused rather than stored, so the library cannot become a place arbitrary strings accumulate.
# The digit lookahead matters: without it "hello" parses as country HE + serial LLO and was
# accepted as a publication.
_PUB_RE = re.compile(r"^(?=.*\d)[A-Z]{2}-?[0-9A-Z.\-/]{2,40}$")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS app_saved_patents (
         id bigserial PRIMARY KEY,
         user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
         publication_number text NOT NULL,
         title text NOT NULL DEFAULT '',
         note text NOT NULL DEFAULT '',
         tag text NOT NULL DEFAULT '',
         source_slug text NOT NULL DEFAULT '',
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (user_id, publication_number))""",
    "CREATE INDEX IF NOT EXISTS app_saved_patents_user_idx "
    "ON app_saved_patents (user_id, updated_at DESC)",
)

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def ensure_schema(force: bool = False) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return
        with db.cursor(autocommit=True) as cur:
            for statement in _SCHEMA:
                cur.execute(statement)
        _SCHEMA_READY = True


def reset_schema_cache_for_tests() -> None:
    global _SCHEMA_READY
    _SCHEMA_READY = False


def normalize_pub(pub) -> str:
    p = re.sub(r"\s+", "", str(pub or "")).upper()
    if not p or len(p) > 64 or not _PUB_RE.match(p):
        raise ValueError("that is not a publication number")
    return p


def _clean(value, limit):
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))
    return s.strip()[:limit]


def save(user_id, pub, *, title="", note="", tag="", source_slug=""):
    """Add or update a publication in this user's library. Idempotent on (user, publication).

    An existing note is NOT overwritten by a save that carries no note — the common case is the
    same document being saved again from a different search, and silently wiping what the user
    wrote about it would be worse than not saving at all.
    """
    ensure_schema()
    pub = normalize_pub(pub)
    note = _clean(note, MAX_NOTE_CHARS)
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO app_saved_patents (user_id,publication_number,title,note,tag,source_slug)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (user_id,publication_number) DO UPDATE SET
                 title = CASE WHEN EXCLUDED.title <> '' THEN EXCLUDED.title
                              ELSE app_saved_patents.title END,
                 note = CASE WHEN EXCLUDED.note <> '' THEN EXCLUDED.note
                             ELSE app_saved_patents.note END,
                 tag = CASE WHEN EXCLUDED.tag <> '' THEN EXCLUDED.tag
                            ELSE app_saved_patents.tag END,
                 source_slug = CASE WHEN app_saved_patents.source_slug = ''
                                    THEN EXCLUDED.source_slug ELSE app_saved_patents.source_slug END,
                 updated_at = now()
               RETURNING *""",
            (int(user_id), pub, _clean(title, 400), note, _clean(tag, MAX_TAG_CHARS),
             _clean(source_slug, 200)))
        return dict(cur.fetchone())


def update_note(user_id, pub, note):
    """Set the note explicitly — this one DOES clear it when passed an empty string."""
    ensure_schema()
    pub = normalize_pub(pub)
    with db.cursor() as cur:
        cur.execute("UPDATE app_saved_patents SET note=%s, updated_at=now() "
                    "WHERE user_id=%s AND publication_number=%s RETURNING *",
                    (_clean(note, MAX_NOTE_CHARS), int(user_id), pub))
        row = cur.fetchone()
    return dict(row) if row else None


def remove(user_id, pub) -> bool:
    ensure_schema()
    try:
        pub = normalize_pub(pub)
    except ValueError:
        return False
    with db.cursor() as cur:
        cur.execute("DELETE FROM app_saved_patents WHERE user_id=%s AND publication_number=%s",
                    (int(user_id), pub))
        return cur.rowcount > 0


def listing(user_id, *, query="", limit=MAX_ROWS_RETURNED):
    ensure_schema()
    q = _clean(query, 200)
    with db.cursor() as cur:
        if q:
            like = f"%{q.lower()}%"
            cur.execute("SELECT * FROM app_saved_patents WHERE user_id=%s AND ("
                        "lower(publication_number) LIKE %s OR lower(title) LIKE %s "
                        "OR lower(note) LIKE %s OR lower(tag) LIKE %s) "
                        "ORDER BY updated_at DESC LIMIT %s",
                        (int(user_id), like, like, like, like, int(limit)))
        else:
            cur.execute("SELECT * FROM app_saved_patents WHERE user_id=%s "
                        "ORDER BY updated_at DESC LIMIT %s", (int(user_id), int(limit)))
        return [dict(r) for r in cur.fetchall()]


def saved_set(user_id, pubs=None):
    """Which of these publications are already in the library — for marking the report cards."""
    ensure_schema()
    with db.cursor() as cur:
        if pubs:
            wanted = []
            for p in list(pubs)[:MAX_ROWS_RETURNED]:
                try:
                    wanted.append(normalize_pub(p))
                except ValueError:
                    continue
            if not wanted:
                return set()
            cur.execute("SELECT publication_number FROM app_saved_patents "
                        "WHERE user_id=%s AND publication_number = ANY(%s)",
                        (int(user_id), wanted))
        else:
            cur.execute("SELECT publication_number FROM app_saved_patents WHERE user_id=%s",
                        (int(user_id),))
        return {r["publication_number"] for r in cur.fetchall()}


def count(user_id) -> int:
    ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM app_saved_patents WHERE user_id=%s", (int(user_id),))
        return int((cur.fetchone() or {}).get("n") or 0)
