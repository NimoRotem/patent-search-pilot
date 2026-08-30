"""The work pool: one shared dedup and lease mechanism for every fetch worker.

This is the piece that makes horizontal scaling safe. The previous backfill needed exactly this
after duplicate and stalled worker incidents: two workers fetching the same publication is money
spent twice, and a worker killed mid-batch used to take its rows with it.

DEDUP is the primary key. `fulltext_fetch_task.publication_number` is the PK, so a publication is
in the pool exactly once however many times a manifest names it. Seeding is
`ON CONFLICT DO NOTHING`, so re-seeding is free and idempotent.

PARTITIONS are assigned at seed time from a hash of the FAMILY, not the publication, so every
member of a family lands in the same partition. That matters: the corpus rung answers a
publication from a family sibling, and two workers racing to fill in siblings of one family would
each do the other's work. `PARTITIONS` is fixed at 16 and a worker takes `partition_id % of == i`,
so 1 to 16 workers run on disjoint sets without ever reseeding the pool.

LEASES have an expiry. Claiming is `FOR UPDATE SKIP LOCKED` over the partial index of pending rows
only, so two workers never wait on each other and never take the same row. A dead worker's rows
come back to the pool when `reap()` finds their lease expired; `attempts` is incremented at LEASE
time, not at completion, so a publication that reliably kills its worker is retired to 'failed'
after `MAX_ATTEMPTS` instead of poisoning the pool for ever.

COMPLETION IS LEASE CHECKED. `complete()` updates only where `lease_owner` is still this worker.
A worker that lost its lease mid-fetch does not get to overwrite the state of a row that somebody
else now owns. It keeps whatever text it fetched (the docstore merges, so that is never a loss),
it just may not claim the row.
"""
from __future__ import annotations

import hashlib
import os
import socket
import uuid

import db

TABLE = "fulltext_fetch_task"

#  Fixed, and deliberately larger than the worker count: it is what lets the operator scale from
#  one worker to four (and back) without touching a seeded row.
PARTITIONS = int(os.environ.get("FULLTEXT_PARTITIONS", "16"))
LEASE_SECONDS = int(os.environ.get("FULLTEXT_LEASE_SECONDS", "600"))
MAX_ATTEMPTS = int(os.environ.get("FULLTEXT_MAX_ATTEMPTS", "3"))

STATES = ("pending", "leased", "done", "missing", "failed", "skipped")


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def partition_of(family_key: str) -> int:
    """Stable across processes and restarts. `hash()` is NOT: PYTHONHASHSEED randomises str
    hashing per process, so a partition computed with it would move under the worker's feet."""
    h = hashlib.md5((family_key or "").encode("utf-8", "replace")).digest()
    return int.from_bytes(h[:2], "big") % PARTITIONS


def partitions_for(shard: int, of: int) -> list:
    """The partition ids one worker owns. Disjoint by construction, and their union over
    shard in range(of) is every partition, so no row is ever orphaned."""
    if of < 1 or shard < 0 or shard >= of:
        raise ValueError(f"shard {shard} of {of} is not a valid partition assignment")
    return [p for p in range(PARTITIONS) if p % of == shard]


# ---------------------------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------------------------
def _ddl_path():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent.parent / "sql" / "014_fulltext_acquisition.sql"


REQUIRED_TABLES = ("fulltext_fetch_task", "fulltext_fetch_event", "fulltext_budget",
                   "fulltext_manifest_cursor")


def missing_tables() -> list:
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute("SELECT name FROM unnest(%s::text[]) AS required(name) "
                    "WHERE to_regclass(required.name) IS NULL ORDER BY name",
                    (list(REQUIRED_TABLES),))
        return [r["name"] for r in cur.fetchall() or []]


def ensure_schema() -> list:
    """Apply sql/014 (all IF NOT EXISTS). An EXPLICIT operator action, invoked by
    `ops/fulltext_acquire.py ensure-schema`, never by a worker at startup: a runtime process that
    applies DDL turns an ordinary import into a schema change and lets two starts race."""
    sql = _ddl_path().read_text()
    with db.cursor(autocommit=True) as cur:
        cur.execute(sql)
    return missing_tables()


def require_schema() -> None:
    missing = missing_tables()
    if missing:
        raise RuntimeError(
            f"full-text acquisition schema is not installed: {', '.join(missing)}. "
            f"Run: ops/fulltext_acquire.py ensure-schema")


# ---------------------------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------------------------
def seed(entries, manifest: str = "") -> dict:
    """Add manifest entries to the pool. Idempotent: a publication already in the pool, in any
    state, is left exactly as it is. Returns {"offered": n, "added": n}.

    `entries` is an iterable of dicts with publication_number and, optionally, family_id, country,
    priority.
    """
    rows = []
    for e in entries:
        pn = str(e.get("publication_number") or "").strip().upper()
        if not pn:
            continue
        fam = str(e.get("family_id") or "") or ""
        rows.append((pn, fam, str(e.get("country") or "")[:4],
                     partition_of(fam or pn), int(e.get("priority") or 100),
                     str(e.get("manifest") or manifest)))
    if not rows:
        return {"offered": 0, "added": 0}
    added = 0
    with db.cursor() as cur:
        for i in range(0, len(rows), 1000):
            batch = rows[i:i + 1000]
            cur.executemany(
                f"INSERT INTO {TABLE} (publication_number, family_id, country, partition_id, "
                f"priority, manifest) VALUES (%s,%s,%s,%s,%s,%s) "
                f"ON CONFLICT (publication_number) DO NOTHING",
                batch)
            added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return {"offered": len(rows), "added": added}


# ---------------------------------------------------------------------------------------------
# leasing
# ---------------------------------------------------------------------------------------------
def lease(partition_ids, owner: str, limit: int = 16, ttl: int = None, manifests=None) -> list:
    """Take up to `limit` pending rows from these partitions. SKIP LOCKED, so a second worker on
    the same partitions (an operator mistake, not a design) still cannot take the same row.

    `manifests` pins a worker to rows from named manifests. Production leaves it None and leases
    everything, so a manifest nobody named can never be stranded; it exists for a worker dedicated
    to one manifest, and for the end-to-end test, whose fixture rows must not be visible to the
    running production worker.
    """
    ttl = int(ttl or LEASE_SECONDS)
    manifests = list(manifests) if manifests else None
    with db.cursor() as cur:
        cur.execute(
            f"""WITH picked AS (
                    SELECT publication_number FROM {TABLE}
                     WHERE state='pending' AND partition_id = ANY(%s)
                       AND (%s::text[] IS NULL OR manifest = ANY(%s::text[]))
                     ORDER BY priority, created_at
                     FOR UPDATE SKIP LOCKED LIMIT %s)
                UPDATE {TABLE} t
                   SET state='leased', lease_owner=%s, attempts=t.attempts+1, updated_at=now(),
                       lease_expires_at = now() + make_interval(secs => %s)
                  FROM picked p WHERE t.publication_number = p.publication_number
              RETURNING t.publication_number, t.family_id, t.country, t.partition_id,
                        t.priority, t.attempts, t.manifest""",
            (list(partition_ids), manifests, manifests, int(limit), owner, ttl))
        return [dict(r) for r in cur.fetchall() or []]


def renew(publication_numbers, owner: str, ttl: int = None) -> list:
    """Push the lease out. Returns the publications STILL owned by this worker: anything absent
    from the result was reaped and handed to somebody else, and must be abandoned."""
    pubs = list(publication_numbers)
    if not pubs:
        return []
    ttl = int(ttl or LEASE_SECONDS)
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE} SET lease_expires_at = now() + make_interval(secs => %s), "
            f"updated_at=now() WHERE state='leased' AND lease_owner=%s "
            f"AND publication_number = ANY(%s) RETURNING publication_number",
            (ttl, owner, pubs))
        return [r["publication_number"] for r in cur.fetchall() or []]


def reap(now_expired_only: bool = True) -> dict:
    """Return dead workers' rows to the pool. A row that has burned MAX_ATTEMPTS goes to 'failed'
    rather than round again: a publication that kills its worker every time is a defect to look
    at, not a reason to keep the pool churning."""
    with db.cursor() as cur:
        cur.execute(
            f"""UPDATE {TABLE}
                   SET state = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                       lease_owner = NULL, lease_expires_at = NULL, updated_at = now(),
                       last_error = CASE WHEN attempts >= %s
                                         THEN 'lease expired ' || attempts || ' times'
                                         ELSE last_error END
                 WHERE state='leased' AND lease_expires_at < now()
             RETURNING publication_number, state""",
            (MAX_ATTEMPTS, MAX_ATTEMPTS))
        rows = cur.fetchall() or []
    out = {"requeued": 0, "failed": 0}
    for r in rows:
        out["failed" if r["state"] == "failed" else "requeued"] += 1
    return out


def complete(publication_number: str, owner: str, *, state: str, provider: str = "",
             claims_chars: int = 0, desc_chars: int = 0, raw_uri: str = "",
             parsed_uri: str = "", error: str = "") -> bool:
    """Finish a leased row. False means the lease was lost and another worker owns it now."""
    if state not in STATES:
        raise ValueError(f"unknown task state {state!r}")
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE} SET state=%s, provider=%s, claims_chars=%s, desc_chars=%s, "
            f"raw_uri=%s, parsed_uri=%s, last_error=%s, lease_owner=NULL, "
            f"lease_expires_at=NULL, updated_at=now() "
            f"WHERE publication_number=%s AND lease_owner=%s AND state='leased'",
            (state, provider, int(claims_chars), int(desc_chars), raw_uri, parsed_uri,
             str(error)[:500], publication_number, owner))
        return bool(cur.rowcount)


def release(publication_numbers, owner: str) -> int:
    """Hand rows back on a clean shutdown, so a restart resumes immediately instead of waiting
    out the lease."""
    pubs = list(publication_numbers)
    if not pubs:
        return 0
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE} SET state='pending', lease_owner=NULL, lease_expires_at=NULL, "
            f"attempts = GREATEST(0, attempts - 1), updated_at=now() "
            f"WHERE state='leased' AND lease_owner=%s AND publication_number = ANY(%s)",
            (owner, pubs))
        return cur.rowcount or 0


def counts(partition_ids=None) -> dict:
    where, params = "", []
    if partition_ids is not None:
        where, params = "WHERE partition_id = ANY(%s)", [list(partition_ids)]
    with db.cursor(autocommit=True, readonly=True) as cur:
        cur.execute(f"SELECT state, count(*) n FROM {TABLE} {where} GROUP BY state", params)
        return {r["state"]: int(r["n"]) for r in cur.fetchall() or []}


def delete(publication_numbers) -> int:
    """Exact publication numbers only. Tests clean up their own fixtures with this; a prefix or a
    LIKE here would be how a cleanup takes real rows with it."""
    pubs = list(publication_numbers)
    if not pubs:
        return 0
    with db.cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE} WHERE publication_number = ANY(%s)", (pubs,))
        return cur.rowcount or 0
