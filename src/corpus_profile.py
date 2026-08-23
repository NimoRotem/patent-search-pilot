"""What is actually in our own database, measured, and what widening it would cost.

WHY A PAGE FOR THIS. A search that lands outside the indexed field says so — "OUT OF DOMAIN: the
local corpus covers only vacuum / suction gripping and lifting... treat these as adjacent art" —
and that is the only place the scope was ever stated. It tells a reader what the corpus is NOT
without ever telling them what it IS: how big, how old, which offices, which classifications, how
it is embedded, on what machine, and what it would take to cover a second field.

WHAT IS MEASURED WHEN. Sizes come from `pg_class`, which is a catalogue read and costs
milliseconds. Publication and chunk totals come from `corpus_facts`, which the app already caches
for an hour. The expensive ones — 53 M classification rows, 9 M claim rows, chunk kinds — take
three to four minutes of I/O on a 293 GB database and are NOT run per request: they are snapshotted
by `snapshot()` and served with the date they were taken. A page that recomputes them on view is a
page that times out.

Nothing here is estimated except where it says so, and the two places it says so are the planner's
row estimates (never shown) and the projections at the bottom, which are arithmetic on measured
ratios with the ratio printed beside the answer.
"""
from __future__ import annotations

import json
import os
import time
import traceback

from config import DATA, EMBED_DIM, INGEST_CPC, SEED_CPC, SEED_CPC_TITLES

SNAPSHOT = DATA / "corpus_profile.json"

#  Imported rather than re-spelled: see the `families` step in snapshot().
try:
    from retrieval.family import family_key_sql as _family_key_sql
    _FAMILY_KEY = _family_key_sql()
except Exception:                                                         # noqa: BLE001
    _FAMILY_KEY = ("COALESCE(NULLIF(NULLIF(NULLIF(simple_family_id,''),'-1'),'0'), "
                   "publication_number)")

#  The machine the corpus lives on. Not discoverable from inside the database, and a fact a reader
#  needs to make sense of a 94 GB index: it does not fit in RAM, which is the retrieval floor.
DB_HOST = {
    "name": os.environ.get("CORPUS_DB_HOST", "patents-pilot-db"),
    "machine_type": os.environ.get("CORPUS_DB_MACHINE", "e2-highmem-8"),
    "vcpu": int(os.environ.get("CORPUS_DB_VCPU", "8")),
    "ram_gb": int(os.environ.get("CORPUS_DB_RAM_GB", "64")),
    "disk_gb": int(os.environ.get("CORPUS_DB_DISK_GB", "600")),
    "disk_type": os.environ.get("CORPUS_DB_DISK_TYPE", "pd-balanced"),
    "zone": os.environ.get("CORPUS_DB_ZONE", "us-central1-b"),
    "note": "Docker pgvector/pgvector:pg17, pgdata bind-mounted at /srv/patents/pgdata",
}
#  Moved off instance-3 on 2026-08-21 onto a machine that runs nothing else. The old values sat
#  here as defaults for a day and the page went on reporting a host the app had left.
APP_HOST = {
    "name": os.environ.get("CORPUS_APP_HOST", "nimo-iptorch-patents"),
    "machine_type": os.environ.get("CORPUS_APP_MACHINE", "t2d-standard-8"),
    "vcpu": int(os.environ.get("CORPUS_APP_VCPU", "8")),
    "ram_gb": int(os.environ.get("CORPUS_APP_RAM_GB", "32")),
    "disk_gb": int(os.environ.get("CORPUS_APP_DISK_GB", "500")),
    "note": "gunicorn, 1 worker x 16 threads, plus the durable search workers, "
            "behind nginx at nimo.iptorch.com",
}
EMBEDDING = {
    "provider": "Google Vertex AI",
    "model": "gemini-embedding-001",
    "dims": EMBED_DIM,
    "shortening": "Matryoshka (output_dimensionality), full model is 3072",
    "asymmetric": "documents embedded as RETRIEVAL_DOCUMENT, queries as RETRIEVAL_QUERY",
    "storage": "pgvector `vector` (float32, 4 bytes per dimension)",
}
#  What a 1M-token embedding run costs at Vertex list price. Named so a reader can substitute their
#  own rate rather than trust a number with no provenance.
EMBED_USD_PER_MTOK = float(os.environ.get("EMBED_USD_PER_MTOK", "0.15"))


def _fast_sizes():
    """Per-table and per-index size, from the catalogue. Milliseconds, no table scan."""
    out = {"tables": [], "indexes": [], "database": None, "server": None, "settings": {}}
    try:
        import db
        with db.cursor() as cur:
            cur.execute("SELECT current_database() db, version() v, "
                        "pg_size_pretty(pg_database_size(current_database())) size, "
                        "pg_database_size(current_database()) bytes")
            r = cur.fetchone()
            out["database"] = {"name": r["db"], "size": r["size"], "bytes": r["bytes"]}
            out["server"] = (r["v"] or "").split(" on ")[0]
            cur.execute(
                "SELECT c.relname tbl, pg_total_relation_size(c.oid) bytes, "
                "pg_size_pretty(pg_total_relation_size(c.oid)) total, "
                "pg_size_pretty(pg_relation_size(c.oid)) heap, "
                "pg_size_pretty(pg_indexes_size(c.oid)) idx "
                "FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid "
                "ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 12")
            out["tables"] = [dict(x) for x in cur.fetchall()]
            cur.execute(
                "SELECT c.relname idx, t.relname tbl, am.amname method, "
                "pg_relation_size(c.oid) bytes, pg_size_pretty(pg_relation_size(c.oid)) size "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_am am ON am.oid = c.relam "
                "WHERE t.relkind = 'r' ORDER BY pg_relation_size(c.oid) DESC LIMIT 8")
            out["indexes"] = [dict(x) for x in cur.fetchall()]
            #  `current_setting` gives the human form ("16GB"). `pg_settings.setting` gives the
            #  raw count in the parameter's own unit, which for shared_buffers is 8 kB blocks —
            #  concatenating that with the unit produced "20971528kB" on the page.
            for name in ("shared_buffers", "effective_cache_size", "work_mem",
                         "maintenance_work_mem", "max_parallel_workers_per_gather"):
                cur.execute("SELECT current_setting(%s) v", (name,))
                got = cur.fetchone()
                if got and got["v"]:
                    out["settings"][name] = got["v"]
            cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
            out["extensions"] = {x["extname"]: x["extversion"] for x in cur.fetchall()}
    except Exception:
        traceback.print_exc()
    return out


def snapshot(log=print):
    """Run the EXPENSIVE counts once and write them down. Minutes, not milliseconds.

    Called from ops, not from a request. Each answer carries the seconds it took, so the next
    person to wonder whether this can go on the request path can read the answer instead of
    finding out.
    """
    steps = [
        ("claims", "SELECT count(*) rows, count(DISTINCT publication_id) pubs FROM claims"),
        ("classifications",
         "SELECT count(*) rows, count(DISTINCT substring(symbol,1,4)) subclasses, "
         "count(DISTINCT substring(symbol,1,8)) groups, count(DISTINCT publication_id) pubs "
         "FROM classifications"),
        ("chunk_kinds", "SELECT kind, count(*) n FROM chunks GROUP BY 1 ORDER BY 2 DESC"),
        ("by_country", "SELECT country, count(*) n FROM publications GROUP BY 1 "
                       "ORDER BY 2 DESC LIMIT 16"),
        ("by_decade", "SELECT (extract(year FROM publication_date)::int/10)*10 decade, count(*) n "
                      "FROM publications WHERE publication_date IS NOT NULL GROUP BY 1 "
                      "ORDER BY 1"),
        ("tiers", "SELECT tier, count(*) n FROM publications GROUP BY 1 ORDER BY 2 DESC"),
        #  THE SAME FAMILY KEY THE SEARCH USES. Spelling it as
        #  `COALESCE(NULLIF(simple_family_id,''), publication_number)` catches the empty string and
        #  nothing else, so the 21,862 publications DOCDB marks `-1`, meaning NO family, all shared
        #  one key and the page under-reported the family count by that many. `family_key_sql` is
        #  the one place the sentinels are listed.
        ("families", "SELECT count(DISTINCT " + _FAMILY_KEY + ") families, "
                     "count(*) FILTER (WHERE abstract <> '') with_abstract FROM publications"),
        ("cpc_top", "SELECT substring(symbol,1,8) cpc, count(DISTINCT publication_id) pubs "
                    "FROM classifications GROUP BY 1 ORDER BY 2 DESC LIMIT 15"),
    ]
    out = {"taken_at": time.time(), "seconds": {}}
    import db
    for name, sql in steps:
        t0 = time.time()
        try:
            with db.cursor() as cur:
                cur.execute(sql)
                out[name] = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            out[name] = [{"error": str(e)[:180]}]
        out["seconds"][name] = round(time.time() - t0, 1)
        log("[corpus_profile] %s in %.1fs" % (name, out["seconds"][name]))
    try:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(SNAPSHOT) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(out, fh, default=str)
        os.replace(tmp, str(SNAPSHOT))
    except Exception:
        traceback.print_exc()
    return out


def _load_snapshot():
    try:
        if SNAPSHOT.exists():
            with open(SNAPSHOT) as fh:
                return json.load(fh)
    except Exception:
        traceback.print_exc()
    return {}


#  ---- the databases searched BESIDES this corpus ----------------------------------------------
#  What each source is FOR, in a sentence an attorney can act on. The engine reports which are
#  live; it does not say what they are worth, and "pqai" on its own tells a reader nothing.
SOURCE_PURPOSE = {
    "uspto": "The USPTO's own record of US filings, including prosecution and office actions.",
    "epo_ops": "The EPO's official register: EP and worldwide bibliographic data, families and "
               "legal status.",
    "pqai": "An open prior-art retrieval service, searched in parallel for art this corpus and "
            "the keyword sources both miss.",
    "serpapi_gpatents": "Google Patents, including its machine translations of CN, JP and KR "
                        "documents.",
    "bigquery_gpatents": "Google's public patent dataset, queried in bulk for classification and "
                         "citation reach.",
    "openalex": "Scholarly literature, for the non-patent prior art a patent search still has to "
                "answer for.",
    "ipaustralia": "IP Australia's national register.",
    "kipris": "The Korean national office: KR patents and utility models.",
    "lens": "Lens.org: worldwide coverage including national offices, families and legal status.",
    "euipo": "EUIPO registered designs.",
    "himmpat": "English full text for CN, JP and KR publications.",
    "gpatents_direct": "Google Patents queried directly, for full text and its translations.",
    "gpatents_scrape": "A fallback reader for Google Patents pages the API route misses.",
    "web_patent_fallback": "A last-resort reader for patent pages the other routes could not "
                           "fetch.",
    "scrapingbee_patents": "A paid fetcher used only when a page refuses the free routes.",
    "firecrawl_patents": "A paid fetcher used only when a page refuses the free routes.",
    "tavily_patents": "A paid fetcher used only when a page refuses the free routes.",
}
_SOURCES_TTL = float(os.environ.get("CORPUS_SOURCES_TTL", "600"))
_sources_cache: dict = {"at": 0.0, "value": None}


def _clean(text):
    """Engine copy, fit to print. The adapters' own notes are written with em dashes and this page
    is customer-facing, so they are turned into ordinary punctuation on the way in rather than
    left for whoever notices."""
    out = str(text or "").replace("—", ",").replace("–", ",")
    return " ".join(out.split()).replace(" ,", ",").replace(",,", ",")


def external_sources(timeout: float = 6.0):
    """Which external databases the search fans out to, and whether each is live right now.

    Read from the retrieval engine's own `/api/health`, which is where the adapters and their
    credentials actually live, and cached for ten minutes: this is a page, not a monitor.

    A source that is off says WHY, because "we searched eleven databases" and "two of them have
    been 401ing for a month" are different statements to make to someone relying on the result. A
    failure to reach the engine returns `None`, so the page can say the list could not be read
    rather than implying nothing is connected.
    """
    now = time.time()
    if _sources_cache["value"] is not None and now - _sources_cache["at"] < _SOURCES_TTL:
        return _sources_cache["value"]
    out = None
    try:
        import requests

        import federation
        #  The RAW endpoint, not `federation.health`, which reduces sources to the names of the
        #  ones that work. The ones that do not are half the point of this section.
        raw = None
        for base in federation._bases():
            try:
                r = requests.get("%s/api/health" % base, headers=federation._headers(),
                                 timeout=timeout)
                if r.status_code == 200:
                    raw = (r.json() or {}).get("sources")
                    break
            except Exception:                                             # noqa: BLE001
                continue
        if isinstance(raw, list):
            out = []
            for s in raw:
                key = str(s.get("name") or "")
                if not key:
                    continue
                out.append({
                    "key": key,
                    "label": federation.source_label(key),
                    "live": bool(s.get("search_available")),
                    "in_report": bool(s.get("in_report_fanout", True)),
                    "purpose": SOURCE_PURPOSE.get(key, ""),
                    "note": _clean(s.get("note")),
                    "reason": _clean(s.get("reason")),
                })
            out.sort(key=lambda s: (not s["live"], s["label"].lower()))
    except Exception:                                                     # noqa: BLE001
        traceback.print_exc()
    _sources_cache["at"] = now
    _sources_cache["value"] = out
    return out


def _gb(n):
    return round(float(n or 0) / 1024 ** 3, 1)


def projections(facts, sizes):
    """What another field would cost, as arithmetic on measured ratios.

    Every number here is derived, and the ratio it was derived from is returned beside it so the
    reader can check the sum rather than trust it.
    """
    pubs = int(facts.get("publications") or 0)
    chunks = int(facts.get("chunks") or 0)
    idx = {i["idx"]: i for i in (sizes.get("indexes") or [])}
    hnsw = idx.get("ix_chunks_hnsw") or {}
    hnsw_bytes = int(hnsw.get("bytes") or 0)
    db_bytes = int((sizes.get("database") or {}).get("bytes") or 0)
    if not (pubs and chunks):
        return {}
    chunks_per_pub = chunks / pubs
    hnsw_per_chunk = (hnsw_bytes / chunks) if chunks else 0
    db_per_pub = (db_bytes / pubs) if pubs else 0
    #  Mean tokens per embedded chunk. The chunker targets a window; this is the figure the
    #  embedding bill is computed from and it is the one input here that is assumed rather than
    #  measured, so it is named and adjustable.
    tokens_per_chunk = float(os.environ.get("CORPUS_TOKENS_PER_CHUNK", "180"))
    per_m = {
        "chunks": round(chunks_per_pub * 1_000_000),
        "hnsw_gb": _gb(hnsw_per_chunk * chunks_per_pub * 1_000_000),
        "disk_gb": _gb(db_per_pub * 1_000_000),
        "embed_tokens": round(chunks_per_pub * 1_000_000 * tokens_per_chunk),
    }
    per_m["embed_usd"] = round(per_m["embed_tokens"] / 1_000_000 * EMBED_USD_PER_MTOK, 2)
    return {
        "chunks_per_pub": round(chunks_per_pub, 2),
        "hnsw_bytes_per_chunk": round(hnsw_per_chunk),
        "db_bytes_per_pub": round(db_per_pub),
        "tokens_per_chunk": tokens_per_chunk,
        "usd_per_mtok": EMBED_USD_PER_MTOK,
        "per_million_publications": per_m,
        #  The fact the whole expansion question turns on.
        "hnsw_gb": _gb(hnsw_bytes),
        "ram_gb": DB_HOST["ram_gb"],
        "index_fits_in_ram": hnsw_bytes < DB_HOST["ram_gb"] * 1024 ** 3,
        "halfvec_hnsw_gb": _gb(hnsw_bytes * 0.5),
        "halfvec_available": (sizes.get("extensions") or {}).get("vector", "") >= "0.7",
    }


def profile():
    """Everything the corpus page shows. Cheap: catalogue reads plus two cached files."""
    try:
        import corpus_facts
        facts = corpus_facts.facts() or {}
    except Exception:
        traceback.print_exc()
        facts = {}
    sizes = _fast_sizes()
    snap = _load_snapshot()
    return {
        "facts": facts,
        "sizes": sizes,
        "snapshot": snap,
        "snapshot_age_days": (round((time.time() - float(snap.get("taken_at") or 0)) / 86400, 1)
                              if snap.get("taken_at") else None),
        "db_host": DB_HOST,
        "app_host": APP_HOST,
        "embedding": EMBEDDING,
        "seed_cpc": [{"code": c, "title": SEED_CPC_TITLES.get(c, "")} for c in SEED_CPC],
        "ingest_cpc": list(INGEST_CPC),
        "ingest_is_seed": list(INGEST_CPC) == list(SEED_CPC),
        "external_sources": external_sources(),
        "projections": projections(facts, sizes),
    }
