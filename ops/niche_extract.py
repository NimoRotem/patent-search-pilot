"""Pull everything the niche enumeration needs out of the live corpus, ONCE, and cache it.

WHY A CACHE AND NOT A LIVE QUERY PER QUESTION
---------------------------------------------
The corpus database is a 62 GB box holding a 94 GB HNSW index while it serves production searches.
The boundary study asks a few hundred aggregate questions of `classifications`, and the index on
`classifications.symbol` is a btree in the `en_US.utf8` collation, so a `LIKE 'B66C1/02%'` prefix
predicate CANNOT use it: MEASURED, that query is a parallel sequential scan reading 2.7 GB and
taking 8.6 s, every time it is asked. Asking it two hundred times is half an hour of production
IO for an answer that never changes during the study.

So each table is read exactly once, sequentially, with `COPY ... TO STDOUT`, and every later
question is answered from the local file. MEASURED on 2026-08-22, whole extraction:

    classifications  51,473,700 rows   29 s   263 MB gz
    publications      4,984,254 rows   13 s   214 MB gz
    citations        31,961,042 rows   45 s   254 MB gz
    claims agg        2,4xx,xxx rows   42 s     5 MB gz   (parallel INDEX ONLY scan)
    paragraphs agg    1,6xx,xxx rows   60 s     3 MB gz   (parallel INDEX ONLY scan)
    title + abstract  4,984,254 rows   56 s   1.3 GB gz

    total 245 s of sequential reads.

A sequential scan uses Postgres' bulk-read ring buffer (256 kB), so it does NOT evict the 16 GB
shared buffer pool the live searches are using. That is why this is a safe shape and a per-symbol
LIKE loop is not. `classifications` had already taken 4,651 sequential scans in this database's
lifetime before this script existed, because `retrieval.cpc.channel_cpc` does one on every call,
and it took 155 more in the hour this extraction ran, of which 2 were this script's.

THE TWO PASSES OVER `publications` ARE WINDOWED. The rebuild brief names that table as one that
must never take an unbounded scan, so both passes are cut into `--range-rows` primary-key windows
with `--window-sleep` between them. MEASURED: 13 windows of 500,000 ids, 40.3 s, 4,984,254 rows,
byte-for-byte the same content as the single scan it replaces. A window is an index scan in
physical order over an append-only table, so it reads the same pages without holding the IO queue
for a minute at a time, and it comes out in id order, which compresses 4% better as a bonus.

NOTHING HERE WRITES. Every statement is a `COPY (SELECT ...) TO STDOUT`.

    python ops/niche_extract.py --out data/niche_cache
    python ops/niche_extract.py --out data/niche_cache --only classifications
    python ops/niche_extract.py --out data/niche_cache --force        # re-read what is cached
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from config import PG  # noqa: E402

#  `publications` is the table the rebuild brief names as one that must never take an unbounded
#  scan, so the two passes over it are cut into `--range-rows` windows on the primary key with a
#  sleep between, which gives a live search the disk back every few seconds. A window is an index
#  scan in physical order over an append-only table, so it reads the same bytes as one scan without
#  holding the IO queue for a minute at a time. `{RANGE}` is where the window predicate goes.
RANGE_TOKEN = "{RANGE}"

#  name -> (output file, the single COPY statement, why it is shaped that way)
PASSES = {
    "classifications": (
        "classifications.tsv.gz",
        "COPY (SELECT publication_id, scheme, symbol FROM classifications "
        "WHERE symbol IS NOT NULL) TO STDOUT",
        "the whole classification table; sorted by publication after the copy",
    ),
    "publications": (
        "publications.csv.gz",
        "COPY (SELECT id, publication_number, kind_code, country, publication_date, filing_date, "
        "earliest_priority_date, simple_family_id, extended_family_id, tier, "
        "(abstract IS NOT NULL) AS has_abstract_row, title FROM publications "
        "WHERE " + RANGE_TOKEN + ") TO STDOUT (FORMAT csv)",
        "metadata only; the abstract TEXT is a separate pass so this one never detoasts",
    ),
    "citations": (
        "citations.csv.gz",
        "COPY (SELECT src_pub, dst_pub, category, origin FROM citations) TO STDOUT (FORMAT csv)",
        "category is who supplied it (SEA/EXA/ISR/APP), origin is the X/Y/A relevance code",
    ),
    "claims_agg": (
        "claims_agg.tsv.gz",
        "COPY (SELECT publication_id, count(*), sum(length(text)) FROM claims GROUP BY 1) "
        "TO STDOUT",
        "parallel INDEX ONLY scan on ix_claims_pub; the length() is what forces the heap read",
    ),
    "para_agg": (
        "para_agg.tsv.gz",
        "COPY (SELECT publication_id, count(*), sum(length(text)) FROM paragraphs GROUP BY 1) "
        "TO STDOUT",
        "same shape on ix_para_pub",
    ),
    "pubtext": (
        "pubtext.csv.gz",
        "COPY (SELECT id, title, abstract FROM publications WHERE " + RANGE_TOKEN + ") "
        "TO STDOUT (FORMAT csv)",
        "the only pass that detoasts; kept last so a failure here costs nothing else",
    ),
}

SORTED_CLASSIFICATIONS = "classifications.sorted.tsv.gz"


def _psql_argv():
    return ["psql", "-h", str(PG["host"]), "-p", str(PG["port"]), "-U", str(PG["user"]),
            "-d", str(PG["dbname"]), "-q", "-v", "ON_ERROR_STOP=1"]


def _env():
    e = dict(os.environ)
    e["PGPASSWORD"] = str(PG["password"])
    return e


def db_counters():
    """A cheap snapshot of what this database has done, so the cost of an extraction can be
    stated as a number rather than asserted."""
    sql = ("SELECT blks_read, blks_hit, tup_returned, xact_commit FROM pg_stat_database "
           "WHERE datname = current_database()")
    out = subprocess.run(_psql_argv() + ["-At", "-F", "\t", "-c", sql],
                         env=_env(), capture_output=True, text=True, check=True).stdout.strip()
    keys = ("blks_read", "blks_hit", "tup_returned", "xact_commit")
    return dict(zip(keys, (int(x) for x in out.split("\t"))))


def max_publication_id():
    out = subprocess.run(_psql_argv() + ["-At", "-c", "SELECT max(id) FROM publications"],
                         env=_env(), capture_output=True, text=True, check=True).stdout.strip()
    return int(out or 0)


def _windows(sql, range_rows):
    """[(sql, label)] for one pass. A statement with no {RANGE} token is a single scan."""
    if RANGE_TOKEN not in sql:
        return [(sql, "")]
    top = max_publication_id()
    out = []
    lo = 0
    while lo <= top:
        hi = lo + range_rows
        out.append((sql.replace(RANGE_TOKEN, f"id >= {lo} AND id < {hi}"), f"{lo}-{hi}"))
        lo = hi
    return out


def run_pass(name, outdir, force=False, gzip_level=1, range_rows=500000, window_sleep=2.0):
    fname, sql, _why = PASSES[name]
    path = os.path.join(outdir, fname)
    if os.path.exists(path) and not force:
        print(f"[skip] {name}: {path} exists")
        return None
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    t0 = time.time()
    windows = _windows(sql, range_rows)
    for i, (wsql, label) in enumerate(windows):
        cmd = " ".join(shlex.quote(a) for a in _psql_argv() + ["-c", wsql])
        cmd += f" | gzip -{gzip_level} >> {shlex.quote(tmp)}"
        proc = subprocess.run(["bash", "-o", "pipefail", "-c", cmd], env=_env())
        if proc.returncode != 0:
            raise SystemExit(f"[fail] {name} window {label}: psql exited {proc.returncode}")
        if len(windows) > 1:
            print(f"       {name} window {i + 1}/{len(windows)} {label}", flush=True)
            if i + 1 < len(windows) and window_sleep:
                time.sleep(window_sleep)
    os.replace(tmp, path)
    dt = time.time() - t0
    print(f"[ok]   {name}: {os.path.getsize(path)/1e6:,.1f} MB in {dt:,.1f} s "
          f"({len(windows)} window{'s' if len(windows) != 1 else ''})")
    return dt


def sort_classifications(outdir, force=False):
    """`classifications` comes off the heap in insertion order, and an incremental ingest appends
    rows for a publication that was already there. MEASURED: the raw copy revisits 1,475,201
    publication ids, which silently inflates every distinct-publication count taken from it (5.44M
    'classified publications' instead of the true 3.96M). Sorting by publication id costs 26 s and
    removes the whole class of error."""
    src = os.path.join(outdir, PASSES["classifications"][0])
    dst = os.path.join(outdir, SORTED_CLASSIFICATIONS)
    if os.path.exists(dst) and not force:
        print(f"[skip] sort: {dst} exists")
        return
    tmp = dst + ".tmp"
    cmd = (f"zcat {shlex.quote(src)} | LC_ALL=C sort -k1,1n -S 3G --parallel=4 "
           f"-T {shlex.quote(outdir)} | gzip -1 > {shlex.quote(tmp)}")
    t0 = time.time()
    proc = subprocess.run(["bash", "-o", "pipefail", "-c", cmd])
    if proc.returncode != 0:
        raise SystemExit(f"[fail] sort exited {proc.returncode}")
    os.replace(tmp, dst)
    print(f"[ok]   sort: {os.path.getsize(dst)/1e6:,.1f} MB in {time.time()-t0:,.1f} s")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "niche_cache"))
    ap.add_argument("--only", nargs="*", choices=sorted(PASSES), default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=15.0,
                    help="seconds between passes, so a live search gets the disk back")
    ap.add_argument("--range-rows", type=int, default=500000,
                    help="primary-key window size for the two passes over `publications`")
    ap.add_argument("--window-sleep", type=float, default=2.0,
                    help="seconds between windows within one of those passes")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    before = db_counters()
    t0 = time.time()
    names = args.only or list(PASSES)
    for i, name in enumerate(names):
        run_pass(name, args.out, force=args.force, range_rows=args.range_rows,
                 window_sleep=args.window_sleep)
        if i + 1 < len(names) and args.sleep:
            time.sleep(args.sleep)
    if not args.only or "classifications" in names:
        sort_classifications(args.out, force=args.force)
    after = db_counters()
    cost = {k: after[k] - before[k] for k in before}
    cost["wall_seconds"] = round(time.time() - t0, 1)
    cost["extracted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(args.out, "extract_cost.json"), "w") as fh:
        json.dump(cost, fh, indent=1)
    print("\ndatabase cost of this extraction:")
    for k, v in cost.items():
        print(f"  {k:16} {v:,}" if isinstance(v, int) else f"  {k:16} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
