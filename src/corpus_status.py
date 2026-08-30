"""What the corpus page shows: two numbers that mean something, and what is producing them.

THE TWO NUMBERS.

    Standard database   patents held locally with their WHOLE text: claims AND description.
                        A patent with a title and an abstract is not in this number. Neither is one
                        with claims but no description. The point of this corpus is reading a
                        document against a claim, and you cannot do that from an abstract, so a
                        count that includes abstract-only rows flatters the corpus by about 6x and
                        tells you nothing you can act on.

    Vector database     of those, the ones whose text is embedded and therefore actually findable
                        by meaning. A patent counts here only if it is in the standard database
                        first: an embedded abstract is not a searchable patent.

Deliberately NOT shown: chunk counts, embedding counts, percentages of partial text. They are
implementation detail. Nobody decides anything from "27,623,460 chunks".

WHERE THE DATA COMES FROM. Three different databases, and they are not interchangeable:

    staging   niche_full_v1 on patents-niche-build. The corpus being built. Both headline numbers.
    live      the hot corpus on patents-pilot-db. Holds the fetch POOL, which is the work queue,
              so the machine and rate panels read from here.
    shard     whatever this app instance serves. Not read here at all.

Everything is measured, nothing is estimated, and every panel says when it was taken.
"""
from __future__ import annotations

import json
import os
import time

import psycopg

CACHE_TTL = float(os.environ.get("CORPUS_STATUS_TTL", "60"))

#  LAST GOOD VALUE, ON DISK. The two headline counts are full scans of a database that the build
#  keeps at 100% CPU, so they time out more often than not and a page that shows a dash on timeout
#  shows a dash most of the day. Persist whatever succeeded and serve it with its age: a number
#  from twenty minutes ago is worth far more than an em dash, as long as it says it is twenty
#  minutes old. Written only on success, so a timeout can never overwrite a real number.
_LAST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "corpus_status_last.json")


def _load_last():
    try:
        return json.loads(open(_LAST).read())
    except Exception:
        return {}


def _save_last(d):
    try:
        os.makedirs(os.path.dirname(_LAST), exist_ok=True)
        tmp = _LAST + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, _LAST)
    except Exception:
        pass
_CACHE: dict = {"at": 0.0, "data": None}

#  One fetch worker per machine, because the scarce resource is the egress IP and not the CPU.
#  Names are the hostnames the workers stamp into `lease_owner`.
FLEET = {
    "nimo-iptorch-patents": "search app + fetch shard 0",
    "patents-niche-fetch-1": "fetch shard 1",
    "patents-niche-fetch-2": "fetch shard 2",
    "patents-niche-discovery": "discovery + fetch shard 3",
    "patents-niche-embed": "parse + embed + fetch shard 4",
    "patents-niche-build": "staging database + lexical index",
}

PROVIDER_WHAT = {
    "serp_self": "Google Patents, fetched from our own address. Free.",
    "pqai": "PQAI, a free prior-art API. US only.",
    "corpus": "Already held, under another publication.",
    "corpus:family": "Already held, under a family sibling.",
    "epo_ops": "The EPO's official API. Free, EP and WO only.",
    "himmpat": "CN/JP/KR English full text. Metered.",
    "scrapingbee": "The same Google pages through rented addresses. Paid.",
    "serpapi": "Paid Google Patents. Deliberately last.",
}


def _dsn(host, port, db, user, pw):
    return "host=%s port=%s dbname=%s user=%s password=%s" % (host, port, db, user, pw)


def _staging_dsn():
    return os.environ.get("NICHE_STAGE_DSN") or _dsn(
        os.environ.get("NICHE_DB_HOST", "10.128.0.4"), "5432", "niche_full_v1",
        "niche_factory", os.environ.get("NICHE_DB_PASSWORD", ""))


def _live_dsn():
    #  LIVE_PGPASSWORD, not PGPASSWORD. They were the same database until the v2 cutover, after
    #  which PGPASSWORD is the niche corpus's and reusing it here made every live panel on this
    #  page fail with an authentication error the page then printed three times.
    return _dsn(os.environ.get("LIVE_PGHOST", "10.128.0.53"),
                os.environ.get("LIVE_PGPORT", "5433"), "patents", "patents",
                os.environ.get("LIVE_PGPASSWORD") or os.environ.get("PGPASSWORD", ""))


def _q(dsn, sql, params=None, timeout_ms=25000):
    """One short-lived connection with a hard statement timeout.

    The staging database is the build's bottleneck and is routinely at 100% CPU. A page that waits
    on it without a timeout is a page that hangs, so every panel is allowed to fail on its own and
    say so rather than taking the others down with it.
    """
    with psycopg.connect(dsn, autocommit=True, connect_timeout=8) as c:
        c.execute("SET statement_timeout = %d" % int(timeout_ms))
        cur = c.execute(sql, params or ())
        return cur.fetchall()


def served():
    """WHAT THIS APP ACTUALLY SEARCHES, measured through its own connection. -> dict

    THE DEFECT THIS FIXES. Both headline numbers came from `niche_corpus.niche_publications`,
    which is the BUILD's inventory of what it intends to hold, reached with a `niche_factory`
    login whose password is not configured on this host. So every query failed with
    `fe_sendauth: no password supplied`, the error was swallowed into a field nothing rendered,
    and the page printed a last-good number from disk as though it had just measured it. It said
    383,970 patents with claims and description; the database the searches run against holds
    353,016. A coverage page that reports a different corpus from the one being searched is worse
    than no coverage page.

    This reads `public.publications` and `public.chunks` through `db`, the same connection every
    search uses, so it cannot drift from what a search can find and it cannot fail for want of a
    credential that is right there.
    """
    out = {"publications": None, "with_text": None, "passages": None, "with_figures": None,
           "min_date": None, "max_date": None, "error": ""}
    try:
        import db
        with db.corpus_cursor() as cur:
            cur.execute("SET statement_timeout = 60000")
            cur.execute("SELECT count(*) n, min(publication_date) mn, max(publication_date) mx "
                        "FROM publications")
            r = cur.fetchone()
            out["publications"] = int(r["n"])
            out["min_date"], out["max_date"] = r["mn"], r["mx"]
            cur.execute("SELECT count(*) n FROM chunks")
            out["passages"] = int(cur.fetchone()["n"])
            #  BOTH, not either: a patent counts as readable when we hold its claims AND its
            #  description. An abstract answers no claim chart.
            cur.execute("""
                SELECT count(*) FROM (
                    SELECT publication_id FROM chunks
                     WHERE kind IN ('claim_own','claim_resolved') GROUP BY 1
                    INTERSECT
                    SELECT publication_id FROM chunks WHERE kind='paragraph' GROUP BY 1) x""")
            out["with_text"] = int(list(cur.fetchone().values())[0])
            cur.execute("SELECT count(DISTINCT publication_id) n FROM chunks "
                        "WHERE kind='figure_caption'")
            out["with_figures"] = int(cur.fetchone()["n"])
    except Exception as exc:                                              # noqa: BLE001
        out["error"] = str(exc).split("\n")[0][:200]
    return out


def _headline():
    """The two numbers. Both from staging, both counting WHOLE patents."""
    out = {"standard": None, "vector": None, "with_figures": None, "error": "",
           "standard_age": None, "vector_age": None}
    last = _load_last()
    try:
        rows = _q(_staging_dsn(), """
            SELECT count(*) FILTER (WHERE has_complete_claims AND has_complete_description),
                   count(*) FILTER (WHERE has_complete_claims AND has_complete_description
                                      AND has_figures)
              FROM niche_corpus.niche_publications
        """, timeout_ms=40000)
        out["standard"], out["with_figures"] = int(rows[0][0]), int(rows[0][1])
    except Exception as exc:
        out["error"] = str(exc).split("\n")[0][:200]
    try:
        #  A PATENT IS IN THE VECTOR DATABASE ONLY WHEN EVERY ONE OF ITS CHUNKS HAS A VECTOR.
        #
        #  The previous version of this counted publications with `EXISTS (... a vector ...)`, which
        #  is the wrong question and flattered the number enormously: the v1 corpus reached 49% of
        #  passages embedded with 0.2% of PATENTS finished, and this query called nearly all of them
        #  done. A patent holding half its passages does not half-answer a prior-art search, it
        #  silently fails to surface the decisive paragraph and reads to the user as "no prior art
        #  found". So the only honest count is the complete one.
        #
        #  It is also CHEAP, which the honest count normally is not. v2 manifests are cut on
        #  publication boundaries, so every chunk of a publication carries the same manifest_id and
        #  a manifest marked 'done' means all of its publications are complete, by construction.
        #  That turns a GROUP BY over 11.2M chunks into an index-driven count on a narrow table.
        rows = _q(_staging_dsn(), """
            SELECT count(DISTINCT m.publication_id)
              FROM v2.chunk_manifest m
              JOIN v2.manifests mf USING (manifest_id)
             WHERE mf.status = 'done'
        """, timeout_ms=90000)
        out["vector"] = int(rows[0][0])
    except Exception as exc:
        out["error"] = out["error"] or str(exc).split("\n")[0][:200]

    #  Fall back to the last value that DID come back, and say how old it is.
    now = time.time()
    for key in ("standard", "vector", "with_figures"):
        if out.get(key) is not None:
            last[key] = {"n": out[key], "at": now}
        elif last.get(key):
            out[key] = last[key]["n"]
            out[key + "_age"] = round((now - last[key]["at"]) / 60)
    _save_last(last)
    return out


def _work_left():
    """How much is still to fetch, and where it is. From the live corpus, which holds the pool."""
    out = {"states": {}, "by_country": [], "error": ""}
    try:
        for state, n in _q(_live_dsn(),
                           "SELECT state, count(*) FROM fulltext_fetch_task GROUP BY 1"):
            out["states"][state] = int(n)
        out["by_country"] = [(c or "?", int(n)) for c, n in _q(
            _live_dsn(),
            "SELECT country, count(*) FROM fulltext_fetch_task WHERE state='pending' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 6")]
    except Exception as exc:
        out["error"] = str(exc).split("\n")[0][:200]
    return out


def _rate():
    """Scraping rate, measured over the last hour and the last ten minutes, plus who supplied it."""
    out = {"last_hour": 0, "last_10min": 0, "per_hour_now": 0, "providers": [], "error": ""}
    try:
        out["last_hour"] = int(_q(_live_dsn(),
            "SELECT count(*) FROM fulltext_fetch_task WHERE state='done' "
            "AND updated_at > now() - interval '1 hour'")[0][0])
        out["last_10min"] = int(_q(_live_dsn(),
            "SELECT count(*) FROM fulltext_fetch_task WHERE state='done' "
            "AND updated_at > now() - interval '10 minutes'")[0][0])
        out["per_hour_now"] = out["last_10min"] * 6
        out["providers"] = [(p or "?", int(n)) for p, n in _q(_live_dsn(),
            "SELECT provider, count(*) FROM fulltext_fetch_task WHERE state='done' "
            "AND updated_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC LIMIT 8")]
    except Exception as exc:
        out["error"] = str(exc).split("\n")[0][:200]
    return out


def _rebuild():
    """Progress of the voyage-4-lite rebuild: manifests, vectors, rate, spend.

    Rate is derived from manifests that actually COMPLETED, using the vector counts they really
    returned, not from an average of what we hoped to submit.
    """
    out = {"running": False, "manifests_done": 0, "manifests_total": 0, "in_flight": 0,
           "vectors": 0, "chunks": 0, "patents_total": 0, "per_hour": 0, "hours_left": None,
           "spend": 0.0, "budget": 0.0, "error": ""}
    try:
        r = _q(_staging_dsn(), """
            SELECT count(*) FILTER (WHERE status='done'),
                   count(*) FILTER (WHERE status IN ('claimed','submitted')),
                   count(*), coalesce(sum(n_tokens_est), 0)
              FROM v2.manifests""", timeout_ms=20000)[0]
        out["manifests_done"], out["in_flight"] = int(r[0]), int(r[1])
        out["manifests_total"], tokens = int(r[2]), int(r[3])
        out["running"] = out["manifests_total"] > 0 and out["manifests_done"] < out["manifests_total"]

        #  TWO ROUTES AT DIFFERENT PRICES, so a single blended rate is wrong in both directions.
        #  A manifest carrying a provider_batch_id went through the Voyage Batch API at $0.0134/M.
        #  One without went through the paid MongoDB sync route at $0.02/M, which is 49% dearer and
        #  about 28x faster. Price each for what it actually used and project the remainder at the
        #  route that will actually finish it.
        sp = _q(_staging_dsn(), """
            SELECT coalesce(sum(n_tokens_est) FILTER (
                       WHERE status='done' AND provider_batch_id IS NOT NULL), 0),
                   coalesce(sum(n_tokens_est) FILTER (
                       WHERE status='done' AND provider_batch_id IS NULL), 0),
                   coalesce(sum(n_tokens_est) FILTER (WHERE status <> 'done'), 0)
              FROM v2.manifests""", timeout_ms=20000)[0]
        b_tok, s_tok, left_tok = (float(x) for x in sp)
        out["spend"] = round(b_tok / 1e6 * 0.0134 + s_tok / 1e6 * 0.02, 2)
        out["budget"] = round(out["spend"] + left_tok / 1e6 * 0.02, 2)

        c = _q(_staging_dsn(), """SELECT count(*), count(DISTINCT publication_id)
                                    FROM v2.chunks""", timeout_ms=40000)[0]
        out["chunks"], out["patents_total"] = int(c[0]), int(c[1])
        out["vectors"] = int(_q(_staging_dsn(),
                               "SELECT count(*) FROM v2.embeddings", timeout_ms=40000)[0][0])

        d = _q(_staging_dsn(), """
            SELECT coalesce(sum(n_received), 0),
                   extract(epoch from (max(completed_at) - min(submitted_at)))
              FROM v2.manifests WHERE status='done' AND completed_at IS NOT NULL""",
              timeout_ms=20000)[0]
        got, span = float(d[0] or 0), float(d[1] or 0)
        if span > 300 and got:
            out["per_hour"] = int(got / (span / 3600.0))
            left = max(0, out["chunks"] - out["vectors"])
            out["hours_left"] = round(left / max(1.0, out["per_hour"]), 1)
    except Exception as exc:
        out["error"] = str(exc).split("\n")[0][:200]
    return out


def _machines():
    """Which machines are actually fetching, proved by a lease they hold right now.

    A machine is "working" here because it HOLDS A LEASE, not because a service file says it should.
    That distinction is the whole point: a unit can be active while its process is wedged, and the
    lease is the only evidence that work is moving.
    """
    out = {"rows": [], "error": ""}
    seen = {}
    try:
        for host, n, parts in _q(_live_dsn(), """
                SELECT split_part(lease_owner, ':', 1), count(*),
                       array_agg(DISTINCT partition_id ORDER BY partition_id)
                  FROM fulltext_fetch_task
                 WHERE state='leased' AND lease_expires_at > now()
                 GROUP BY 1"""):
            seen[host] = {"in_flight": int(n), "partitions": list(parts or [])}
    except Exception as exc:
        out["error"] = str(exc).split("\n")[0][:200]
    for host, role in FLEET.items():
        s = seen.get(host)
        out["rows"].append({"host": host, "role": role,
                            "fetching": bool(s),
                            "in_flight": (s or {}).get("in_flight", 0),
                            "partitions": (s or {}).get("partitions", [])})
    return out


#  THE PAGE MUST NOT MEASURE ON A VISIT.
#
#  `status()` is minutes of full scans across three databases when its caches are cold, and the
#  coverage page called it on every render: a page whose entire job is to state four numbers took
#  seconds to open, and on a cold cache it took as long as the scans did. None of those numbers
#  changes in a minute, and the corpus behind them changes on an ingest, not on a page view.
#
#  So the measurement is a job and the page is a reader. `snapshot()` measures and writes;
#  `latest()` reads what was written and never touches a database. The page uses `latest()`, and
#  `?refresh=1` is the deliberate way to make it measure now.
SNAPSHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "corpus_snapshot.json")


def snapshot() -> dict:
    """Measure everything and write it to disk. This is what the daily job runs."""
    data = status(force=True)
    tmp = SNAPSHOT + ".tmp"
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    with open(tmp, "w") as fh:
        json.dump(data, fh, default=str)
    os.replace(tmp, SNAPSHOT)
    return data


def latest(max_age_hours: float = 48.0) -> dict:
    """The last snapshot, without measuring anything. Measures only if there is none at all.

    A snapshot older than `max_age_hours` is still served, because a number from yesterday with
    its age printed beside it is worth far more than a page that hangs, and the page prints the
    age. Missing entirely is the one case worth paying for: a box that has never run the job
    should still answer.
    """
    try:
        with open(SNAPSHOT) as fh:
            d = json.load(fh)
        if isinstance(d, dict) and d.get("taken_at"):
            return d
    except Exception:                                                     # noqa: BLE001
        pass
    return snapshot()


#  MACHINE TYPE -> WHAT YOU ACTUALLY GET. GCE names a shape, not a size, and "c4-highmem-16"
#  tells a reader nothing. Derived from the name rather than listed per host, so a resize shows up
#  the next morning instead of when somebody remembers to edit a table.
_FAMILY_GB_PER_CPU = {"highmem": 8, "highcpu": 2, "standard": 4, "megamem": 14, "ultramem": 28}


def _machine_shape(mtype):
    """'c4-highmem-16' -> '16 vCPU, 128 GB'. Returns the raw name if it does not parse."""
    try:
        parts = (mtype or "").split("-")
        if len(parts) == 3 and parts[2].isdigit():
            cpu = int(parts[2])
            gb = cpu * _FAMILY_GB_PER_CPU.get(parts[1], 4)
            return "%d vCPU, %d GB" % (cpu, gb)
        #  custom-4-6144: the memory is stated outright, in MB.
        if len(parts) == 4 and parts[1] == "custom":
            return "%s vCPU, %d GB" % (parts[2], int(parts[3]) // 1024)
    except Exception:                                                     # noqa: BLE001
        pass
    return mtype or ""


def fleet():
    """The real machines behind the corpus, asked of GCE rather than written down. -> dict

    The roles in FLEET are ours and do not change; the SIZE of each machine does, and a page that
    prints a hardcoded 'e2-standard-4' beside a host that was resized months ago is the same class
    of defect as the stale headline this module already exists to have fixed. So the shapes are
    measured. This shells out to gcloud, which is slow and needs credentials, so it runs ONLY from
    the daily snapshot job and never on a page render.
    """
    out = {"rows": [], "error": ""}
    try:
        import subprocess
        raw = subprocess.run(
            ["gcloud", "compute", "instances", "list", "--project", "nimo-gpt",
             "--format=value(name,machineType.basename(),status,zone.basename())"],
            capture_output=True, text=True, timeout=90)
        if raw.returncode != 0:
            out["error"] = (raw.stderr or "gcloud failed").strip().split("\n")[0][:200]
            return out
        by_name = {}
        for line in raw.stdout.splitlines():
            f = line.split("\t")
            if len(f) >= 3:
                by_name[f[0]] = {"type": f[1], "status": f[2], "zone": f[3] if len(f) > 3 else ""}
        for host, role in FLEET.items():
            m = by_name.get(host) or {}
            out["rows"].append({"host": host, "role": role, "type": m.get("type", ""),
                                "shape": _machine_shape(m.get("type")),
                                "status": m.get("status", "not found"),
                                "zone": m.get("zone", "")})
    except Exception as exc:                                              # noqa: BLE001
        out["error"] = str(exc).split("\n")[0][:200]
    return out


def shape():
    """How the served corpus is actually built: tables, indexes, extension. -> dict

    The page states four counts. This is the layer under them, and it is measured for the same
    reason the counts are: an index that was dropped during a rebuild, or a pgvector that is a
    version behind what the query planner is assumed to have, changes what a search can do and
    changes nothing a count would show.
    """
    out = {"tables": [], "kinds": [], "vector_ext": "", "hnsw": "", "lexical": "",
           "server": "", "database": "", "error": ""}
    try:
        import db
        with db.corpus_cursor() as cur:
            cur.execute("SET statement_timeout = 60000")
            cur.execute("SELECT current_database() d, "
                        "split_part(current_setting('server_version'), ' ', 1) v")
            r = cur.fetchone()
            out["database"], out["server"] = r["d"], "PostgreSQL " + r["v"]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            r = cur.fetchone()
            out["vector_ext"] = "pgvector " + r["extversion"] if r else "pgvector not installed"
            cur.execute("SELECT indexdef FROM pg_indexes "
                        "WHERE schemaname='public' AND indexname='ix_chunks_hnsw'")
            r = cur.fetchone()
            if r:
                d = r["indexdef"]
                out["hnsw"] = d[d.find("USING"):][:120]
            cur.execute("SELECT indexdef FROM pg_indexes "
                        "WHERE schemaname='public' AND indexname='ix_chunks_tsv'")
            r = cur.fetchone()
            out["lexical"] = "GIN over the tsvector column" if r else "no lexical index"
            cur.execute("""
                SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid)) sz
                  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname='public' AND c.relkind='r'
                 ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 6""")
            out["tables"] = [{"name": x["relname"], "size": x["sz"]} for x in cur.fetchall()]
            cur.execute("SELECT kind, count(*) n FROM chunks GROUP BY 1 ORDER BY 2 DESC")
            out["kinds"] = [{"kind": x["kind"], "n": int(x["n"])} for x in cur.fetchall()]
    except Exception as exc:                                              # noqa: BLE001
        out["error"] = str(exc).split("\n")[0][:200]
    return out


def status(force: bool = False) -> dict:
    """Everything the page needs. Cached, because the headline counts are minute-long scans."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["at"] < CACHE_TTL:
        return _CACHE["data"]
    data = {"taken_at": now,
            "served": served(),
            "headline": _headline(),
            "work": _work_left(),
            "rate": _rate(),
            "machines": _machines(),
            "fleet": fleet(),
            "shape": shape(),
            "rebuild": _rebuild(),
            "provider_what": PROVIDER_WHAT}
    _CACHE["at"], _CACHE["data"] = now, data
    return data
