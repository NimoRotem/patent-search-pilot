"""Connection plumbing, funnel widths and the chunk-to-publication aggregator.

Everything here is shared by every channel module, so it must import NOTHING from the rest of the
package (the channel mixins all import this one).

THREADING. `retrieval.orchestrator` still runs the channels sequentially in this refactor. A later
fan-out cannot share one psycopg connection across statements, so the connection plumbing is ready
for that change: the constructing thread keeps its connection, and any future worker thread gets
one from a process-wide thread-local. The fan-out itself must land with its own tests and measured
connection bound.
"""
from __future__ import annotations

import os
import threading

import db

CHUNK_FETCH = 4000     # chunks pulled before aggregating to publications
PUB_CAP = 1000         # per-channel publication cap (the spec's ~1000 width)

# Channels whose cap is a SQL LIMIT cannot collapse families before the database has already
# truncated. They over-fetch by this factor and collapse the surplus down to `cap` FAMILIES.
# 4 because the corpus averages well under four members per family, so a channel that wants 1,000
# families reads 4,000 rows to find them; raising it costs read time for families that are not
# there, and lowering it lets a heavily-published family exhaust the surplus and short the cap.
FAMILY_OVERFETCH = int(os.environ.get("RETRIEVAL_FAMILY_OVERFETCH", "4"))

# ---- funnel width -------------------------------------------------------------------------
# MEASURED (RECALL_STUDY_2026-08-02.md): CHUNK_FETCH=4000 aggregates to **893 distinct
# publications** on today's corpus, i.e. 0.018% of 4.95M, and PUB_CAP never binds. The note that
# "CHUNK_FETCH 4000 -> 12000 moves recall@100 by exactly 0.0000" was measured at 107k publications
# and 2.7M chunks; the corpus is 46x larger now and that conclusion has expired.
#
# But width is NOT free and NOT uniformly worth paying for: with the live query, going from 4,000
# to 60,000 chunks (123 s) did not move the target reference at all. So the width goes where it
# pays: the SEED passes, which describe the whole invention and are the ranking backbone, run wide;
# the per-element sub-searches (there are ~20 of them) keep the cheap profile. A seed pass at these
# settings measures ~20-25 s against ~13 s at the narrow profile.
SEED_CHUNK_FETCH = int(os.environ.get("SEED_CHUNK_FETCH", "9000"))
#  Raising this to 6,000 was tried and REVERTED. The reasoning was sound (it truncates
#  brief_dense, whose pool is ~6,100 publications) and the effect on retrieval was real: cited
#  references moved from fusion rank 3,000-4,000 to 141-191. But top-50 recall FELL, because every
#  downstream stage has a fixed size and a wider pool is more competition at each of them. Widening
#  a funnel only helps if the stage below it widens too, and the page does not.
SEED_PUB_CAP = int(os.environ.get("SEED_PUB_CAP", "2500"))
# pgvector caps hnsw.ef_search at 1000 in this build; 400 is the measured knee for a LIMIT of ~9k.
EF_SEARCH = int(os.environ.get("HNSW_EF_SEARCH", "200"))
SEED_EF_SEARCH = int(os.environ.get("SEED_EF_SEARCH", "400"))
MAX_SCAN_TUPLES = int(os.environ.get("HNSW_MAX_SCAN_TUPLES", "12000"))
SEED_MAX_SCAN_TUPLES = int(os.environ.get("SEED_MAX_SCAN_TUPLES", "60000"))


def _vec(e):
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


# ---- per-request scan profile ---------------------------------------------------------------
# `webapp.retriever()` hands every request the SAME process-global Retriever. `search()` used to
# record its width on `self._wide`, and each fan-out task then read that attribute again at
# EXECUTION time. Two overlapping calls on that one object therefore flipped each other's HNSW
# profile: a wide seed pass could silently run at the narrow ef_search and max_scan_tuples, or a
# narrow element pass could run wide and take the seed budget with it. Neither shows up as an
# error; the seed pass just returns a thinner pool than the funnel was sized for.
#
# So the width a search decided on is carried per TASK instead, captured when the task is created
# and restored when it finishes. `self._wide` is left alone for sequential callers, `scan_profile`
# and `fork`; it is simply no longer what the fan-out reads.
_PROFILE = threading.local()


def active_wide(default=False):
    """The scan profile THIS task captured, or `default` when there is no active context."""
    v = getattr(_PROFILE, "wide", None)
    return default if v is None else v


class profile_context:
    """Bind a scan profile for the duration of a block, then restore the previous one.

    Restored on the way out of an exception as well as a return, so a search that raises cannot
    leave a stale wide setting behind for whatever runs next on this thread. The pool threads are
    reused for the life of the process, so "whatever runs next" is a real search, not a hypothesis.
    """

    __slots__ = ("wide", "prev")

    def __init__(self, wide):
        self.wide = bool(wide)
        self.prev = None

    def __enter__(self):
        self.prev = getattr(_PROFILE, "wide", None)
        _PROFILE.wide = self.wide
        return self

    def __exit__(self, *exc):
        _PROFILE.wide = self.prev
        return False


# ---- per-thread connections ----------------------------------------------------------------
_TLS = threading.local()


def _apply_scan_profile(conn, wide):
    with conn.cursor() as c:
        c.execute("SET hnsw.ef_search = %d" % (SEED_EF_SEARCH if wide else EF_SEARCH))
        c.execute("SET hnsw.iterative_scan = relaxed_order")
        c.execute("SET hnsw.max_scan_tuples = %d"
                  % (SEED_MAX_SCAN_TUPLES if wide else MAX_SCAN_TUPLES))


def worker_conn(wide=False):
    """This thread's own autocommit connection, at the requested ANN width.

    One connection per fan-out thread, created on first use and kept for the thread's life. The
    scan profile is re-asserted only when it changes, because a `SET` is a round trip and a narrow
    element pass and a wide seed pass alternate on the same threads.
    """
    conn = getattr(_TLS, "conn", None)
    if conn is None or getattr(conn, "closed", False):
        conn = db.connect(autocommit=True, readonly=True)
        _TLS.conn = conn
        _TLS.wide = None
    if getattr(_TLS, "wide", None) != bool(wide):
        _apply_scan_profile(conn, bool(wide))
        _TLS.wide = bool(wide)
    return conn


def close_worker_conn():
    """Release this thread's fan-out connection. Only the pool teardown needs this."""
    conn = getattr(_TLS, "conn", None)
    _TLS.conn = None
    _TLS.wide = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


class RetrieverBase:
    """Connection ownership, ANN width and the chunk->publication aggregation every channel uses."""

    def __init__(self, family_map=None):
        self.conn = db.connect(readonly=True)
        self.conn.autocommit = True
        self._wide = False
        self.scan_profile(wide=False)
        # pre-load pid -> family map once (one query), per-pub lookups during dedup were the
        # dominant hidden cost (~1000 queries/search).
        if family_map is None:
            self._fam = {}
            with self.conn.cursor() as c:
                c.execute("SELECT id, COALESCE(NULLIF(simple_family_id,''), publication_number) k FROM publications")
                for r in c.fetchall():
                    self._fam[r["id"]] = r["k"]
        else:
            # Agent element searches run concurrently on independent PostgreSQL connections. The
            # publication->family map is several million rows and immutable during a search, so
            # share it instead of re-reading and duplicating it for every short-lived worker.
            self._fam = family_map

    # -- connection --------------------------------------------------------------------------
    @property
    def conn(self):
        """The connection THIS THREAD may use.

        The owning thread gets the connection handed to `__init__` (or assigned by a test double).
        Any other thread, i.e. a channel running in the fan-out pool, gets its own, so two
        channels never share a psycopg connection.
        """
        #  A Retriever built with object.__new__ (test doubles, pure-logic unit tests) has no
        #  owning connection at all; it gets the thread-local one, which is what it would have
        #  needed anyway the moment it ran SQL.
        own = self.__dict__.get("_conn")
        if own is not None and threading.get_ident() == self.__dict__.get("_conn_tid"):
            return own
        #  The task's OWN captured width, never the shared attribute, which another request may
        #  have flipped between this task being created and being run.
        return worker_conn(self._profile_wide)

    @conn.setter
    def conn(self, value):
        self.__dict__["_conn"] = value
        self.__dict__["_conn_tid"] = threading.get_ident()

    @property
    def _profile_wide(self):
        """This task's captured scan profile, falling back to the instance attribute.

        The fallback is what keeps sequential callers, the owning thread and `fork()` behaving
        exactly as before: outside a fan-out there is no context, so `self._wide` is used.
        """
        return active_wide(self.__dict__.get("_wide", False))

    def scan_profile(self, wide=False):
        """Set this connection's ANN search width. `wide` is for whole-invention seed passes.

        `hnsw.iterative_scan = relaxed_order` (pgvector 0.8) keeps scanning past ef_search until
        enough rows pass the date/status WHERE filter, which is what stops a restrictive novelty
        filter from starving the dense channel. `max_scan_tuples` bounds that, so it has to move
        with ef_search or the wider fetch is silently truncated to the narrow profile's budget.
        """
        self._wide = bool(wide)
        _apply_scan_profile(self.conn, self._wide)

    def _fetch(self):
        return SEED_CHUNK_FETCH if self._profile_wide else CHUNK_FETCH

    def _cap(self):
        return SEED_PUB_CAP if self._profile_wide else PUB_CAP

    def fork(self):
        """Return a search worker with its own DB connection and the shared read-only family map."""
        return type(self)(family_map=self._fam)

    def close(self):
        """Release this retriever's connection (used by bounded agent search workers)."""
        own = self.__dict__.get("_conn")
        if own is not None:
            own.close()

    # -- aggregation -------------------------------------------------------------------------
    def _families_from_chunks(self, sql, params, cap):
        """Run a chunk-level ranking query and cap it at `cap` DISTINCT FAMILIES.

        The family-aware replacement for `_pubs_from_chunks` in the retrieval channels. The old
        order was chunk hits -> best publication -> channel cap -> fusion -> family dedup, so a
        family with forty members in the corpus could spend forty of a channel's slots and then be
        collapsed to one anyway. Rows arrive best-first, so the first member seen is the one that
        carried the evidence and the one that inherits the family's rank, which is the same member
        `dedup_family` would have picked after fusion.
        """
        with self.conn.cursor() as c:
            c.execute(sql, params)
            return self.collapse_rows(c.fetchall(), cap)

    def _pubs_from_chunks(self, sql, params, cap=PUB_CAP):
        """Run a chunk-level ranking query, aggregate to best-per-publication, cap.

        `cap` counts PUBLICATIONS here. `family.collapse_rows` is the family-aware version the
        channels use once family collapse moved ahead of the channel limit; this one is kept
        because chunk_budget.py and the unit tests drive it directly.
        """
        with self.conn.cursor() as c:
            c.execute(sql, params)
            best = {}
            for r in c.fetchall():
                pid = r["publication_id"]
                if pid not in best:      # rows already ordered best-first
                    best[pid] = r["score"]
                if len(best) >= cap:
                    break
        return list(best.items())   # [(pid, score)] best-first
