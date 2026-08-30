"""Persistent patent corpus — every document the sources ever fetch, kept.

Ported from patents-app `docstore.py`, re-backed onto the pilot's Postgres.
App A's engine used to be amnesiac: each search paid SerpApi again for the same
publication, re-parsed the same claims, and threw it all away when the process
ended; two searches in the same technical field re-bought the same 100
documents. This is the store that ends that: fetch a document once, own it for
good.

STORAGE (one table instead of App A's five — the pilot already has its own
publication/chunk machinery, this is only the acquisition cache):

    sources_docstore(
        publication_number  text primary key,
        title               text,
        abstract            text,
        claims_z            bytea,        -- zlib-compressed claims text
        description_z       bytea,        -- zlib-compressed description text
        meta                jsonb,        -- everything else (see below)
        updated_at          timestamptz
    )

`meta` carries: claims_chars, desc_chars, claims_lang, desc_lang, source
(fulltext source), biblio_source, assignee, inventors, date, priority_date,
filing_date, cpc, country, kind, legal_state, legal_status, images, pdf,
family, backward, forward.

DESIGN NOTES kept from App A (they are the point of the store):
  * Text is zlib-compressed. A median Google Patents description is ~110 KB of
    HTML-derived text; at 10,000 documents that is over a gigabyte raw.
    Compression takes it to roughly a quarter of that.
  * MERGE, never overwrite. `put` only replaces a field when the incoming value
    is actually better (non-empty, and for text: longer, or English where what we
    hold is not). A cheap source that answers first must not be able to blank out
    a richer record fetched later.
  * All sync DB work is dispatched through asyncio.to_thread so the async
    acquisition ladder fans out without blocking the loop (App A did the same
    over SQLite).

App A behaviors deliberately NOT ported: the `sections` table (the pilot has its
own chunker; persisting 60 split sections per document in jsonb would defeat the
compression rationale) — the section split still rides along in the in-process
record when gpatents_direct produced it, it just is not persisted.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import zlib
from typing import Any, Optional

TABLE = "sources_docstore"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    publication_number  text PRIMARY KEY,
    title               text,
    abstract            text,
    claims_z            bytea,
    description_z       bytea,
    meta                jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
"""

_READY = False
_READY_LOCK = threading.Lock()


def _db():
    """The pilot's Postgres helper (src/db.py, dict rows). Imported lazily so
    `import sources` works on hosts without psycopg (the facade fails soft)."""
    import db
    return db


def _jsonb(v):
    from psycopg.types.json import Jsonb
    return Jsonb(v)


def _ensure_table() -> None:
    global _READY
    if _READY:
        return
    with _READY_LOCK:
        if _READY:
            return
        with _db().cursor(autocommit=True) as cur:
            cur.execute(DDL)
        _READY = True


def _z(s) -> Optional[bytes]:
    if not s:
        return None
    return zlib.compress(str(s).encode("utf-8", "replace"), 6)


def _uz(b) -> str:
    if not b:
        return ""
    try:
        return zlib.decompress(bytes(b)).decode("utf-8", "replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
def _get_sync(pn: str, want_text: bool = True) -> Optional[dict]:
    _ensure_table()
    with _db().cursor() as cur:
        cur.execute(f"SELECT * FROM {TABLE} WHERE publication_number=%s", (pn,))
        row = cur.fetchone()
    if row is None:
        return None
    meta = row.get("meta") or {}
    d: dict = {"pub_number": pn,
               "title": row.get("title") or "",
               "abstract": row.get("abstract") or ""}
    for k in ("country", "kind", "assignee", "date", "priority_date", "filing_date",
              "legal_state", "legal_status", "biblio_source", "claims_lang", "desc_lang",
              "pdf"):
        d[k] = meta.get(k) or ""
    for k in ("inventors", "cpc", "images", "family", "backward", "forward"):
        v = meta.get(k)
        if v:
            d[k] = list(v)
    d["claims_chars"] = int(meta.get("claims_chars") or 0)
    d["desc_chars"] = int(meta.get("desc_chars") or 0)
    d["fulltext_source"] = meta.get("source") or ""
    if want_text:
        d["claims"] = _uz(row.get("claims_z"))
        d["description"] = _uz(row.get("description_z"))
    return d


async def get(pn: str, want_text: bool = True) -> Optional[dict]:
    return await asyncio.to_thread(_get_sync, pn, want_text)


def _have_sync(pns: list) -> dict:
    """Which of these do we already hold, and how complete is each? No text."""
    if not pns:
        return {}
    _ensure_table()
    out: dict[str, dict] = {}
    with _db().cursor() as cur:
        for i in range(0, len(pns), 400):
            chunk = list(pns[i:i + 400])
            cur.execute(
                f"SELECT publication_number, meta FROM {TABLE} "
                f"WHERE publication_number = ANY(%s)", (chunk,))
            for r in cur.fetchall():
                meta = r.get("meta") or {}
                out[r["publication_number"]] = {
                    "biblio": bool(meta.get("biblio_source")),
                    "claims_chars": int(meta.get("claims_chars") or 0),
                    "desc_chars": int(meta.get("desc_chars") or 0),
                    "fulltext_source": meta.get("source") or "",
                }
    return out


async def have(pns: list) -> dict:
    return await asyncio.to_thread(_have_sync, list(pns))


# ---------------------------------------------------------------------------
# writes — merge, never blank out
# ---------------------------------------------------------------------------
def _better_text(new: str, old_chars: int, new_lang: str, old_lang: str) -> bool:
    """Is the incoming text an upgrade on what we hold?

    Longer wins, and English wins over a non-English original of similar length —
    an English translation of a CN description is worth more to a reader here than
    the Chinese original, even when the original is a few characters longer.
    """
    n = len(new or "")
    if n == 0:
        return False
    if old_chars == 0:
        return True
    if new_lang == "en" and old_lang not in ("en", ""):
        return n >= old_chars * 0.5
    return n > old_chars


_BIBLIO_SCALARS = ("country", "kind", "assignee", "date", "priority_date",
                   "filing_date", "legal_state", "legal_status", "pdf")
_BIBLIO_LISTS = ("inventors", "cpc", "images", "family", "backward", "forward")


def _put_sync(pn: str, rec: dict) -> None:
    _ensure_table()
    src = rec.get("source") or rec.get("biblio_source") or "unknown"
    with _db().cursor() as cur:
        # One transaction, row locked: App A leaned on SQLite's serialization,
        # here concurrent ladder rungs may land the same publication at once.
        cur.execute(f"SELECT * FROM {TABLE} WHERE publication_number=%s FOR UPDATE", (pn,))
        old = cur.fetchone()
        ometa = dict((old.get("meta") or {})) if old is not None else {}

        meta = dict(ometa)
        title = (old.get("title") if old is not None else "") or ""
        abstract = (old.get("abstract") if old is not None else "") or ""
        claims_z = old.get("claims_z") if old is not None else None
        description_z = old.get("description_z") if old is not None else None

        # -- biblio: prefer-new-if-set scalars, dedup-merged lists, longest text
        wrote_biblio = False
        for k in _BIBLIO_SCALARS:
            new = rec.get(k)
            if new:
                meta[k] = str(new)
                wrote_biblio = True
        for k in _BIBLIO_LISTS:
            new = rec.get(k)
            if new:
                meta[k] = list(dict.fromkeys(list(meta.get(k) or []) + list(new)))
                wrote_biblio = True
        new_title = str(rec.get("title") or "")
        if len(new_title) > len(title):
            title = new_title
            wrote_biblio = True
        new_abs = str(rec.get("abstract") or "")
        if len(new_abs) > len(abstract):
            abstract = new_abs
            wrote_biblio = True
        if wrote_biblio:
            meta["biblio_source"] = src

        # -- full text: the merge that must never blank out a richer record
        claims, desc = rec.get("claims") or "", rec.get("description") or ""
        if claims or desc:
            oc = int(ometa.get("claims_chars") or 0)
            od = int(ometa.get("desc_chars") or 0)
            ocl = str(ometa.get("claims_lang") or "")
            odl = str(ometa.get("desc_lang") or "")
            cl = str(rec.get("claims_lang", "") or "")
            dl = str(rec.get("desc_lang", "") or "")
            take_c = _better_text(claims, oc, cl, ocl)
            take_d = _better_text(desc, od, dl, odl)
            if take_c:
                claims_z = _z(claims)
                meta["claims_chars"] = len(claims)
                meta["claims_lang"] = cl
            if take_d:
                description_z = _z(desc)
                meta["desc_chars"] = len(desc)
                meta["desc_lang"] = dl
            if take_c or take_d:
                meta["source"] = src

        cur.execute(
            f"INSERT INTO {TABLE} (publication_number, title, abstract, claims_z, "
            f"description_z, meta, updated_at) VALUES (%s,%s,%s,%s,%s,%s,now()) "
            f"ON CONFLICT (publication_number) DO UPDATE SET title=EXCLUDED.title, "
            f"abstract=EXCLUDED.abstract, claims_z=EXCLUDED.claims_z, "
            f"description_z=EXCLUDED.description_z, meta=EXCLUDED.meta, "
            f"updated_at=EXCLUDED.updated_at",
            (pn, title or None, abstract or None, claims_z, description_z, _jsonb(meta)))


async def put(pn: str, rec: dict) -> None:
    await asyncio.to_thread(_put_sync, pn, rec)


async def put_many(records: dict) -> None:
    def _w():
        for pn, rec in records.items():
            try:
                _put_sync(pn, rec)
            except Exception:
                continue
    await asyncio.to_thread(_w)


def delete_sync(pns: list) -> None:
    """Remove rows (tests clean up their fixtures with this)."""
    if not pns:
        return
    _ensure_table()
    with _db().cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE} WHERE publication_number = ANY(%s)", (list(pns),))


# ---------------------------------------------------------------------------
def _stats_sync() -> dict:
    _ensure_table()
    with _db().cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS docs, "
            f"COUNT(*) FILTER (WHERE (meta->>'claims_chars')::int > 0) AS with_claims, "
            f"COUNT(*) FILTER (WHERE (meta->>'desc_chars')::int > 0) AS with_description, "
            f"COALESCE(SUM(COALESCE(octet_length(claims_z),0) "
            f"+ COALESCE(octet_length(description_z),0)), 0) AS text_bytes "
            f"FROM {TABLE}")
        r = cur.fetchone() or {}
    return {"docs": int(r.get("docs") or 0),
            "with_claims": int(r.get("with_claims") or 0),
            "with_description": int(r.get("with_description") or 0),
            "text_bytes": int(r.get("text_bytes") or 0),
            "table": TABLE}


async def stats() -> dict:
    return await asyncio.to_thread(_stats_sync)


def stats_sync() -> dict:
    return _stats_sync()
