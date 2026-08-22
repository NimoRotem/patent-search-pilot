#!/usr/bin/env python3
"""Prove that a shard's publication ids ARE the hot corpus's publication ids.

WHY THIS IS A GATE AND NOT A REPORT. `retrieval.cold` hydrates the family key of every cold hit
from the shard and writes it into the retriever's family map, filling gaps and never overwriting.
If a shard renumbered its publications, id 4711 on the shard and id 4711 in the hot corpus are two
different disclosures, and the hot one's family would be silently attributed to the cold one. That
is not a crash and not an empty result. It is a wrong answer that looks like a right one, and
nothing downstream can detect it.

So the check runs BEFORE a shard is allowed to say it is serving. `shardctl.sh ready` calls it and
refuses to flip `shard_status.state` to `ready` on a mismatch, which means the agent keeps saying
`waking`, `ensure` never returns `hot`, and the shard is never queried. The gate is upstream of
the query rather than a fallback inside it.

    ./verify_ids.py domain_03                     sample 500 ids, compare against the hot corpus
    ./verify_ids.py domain_03 --sample 5000       a deeper sample
    ./verify_ids.py domain_03 --json

EXIT CODES
    0   every sampled id that the hot corpus also holds names the same publication
    1   at least one MISMATCH: the same id names two different publications. Fatal.
    2   could not run the check at all (no shard, not reachable, no publications table)

An id the hot corpus does NOT hold is not a failure: reaching art the hot corpus does not have is
the entire point of a cold shard. It is only reported, and the one case worth shouting about is a
sample where NOTHING overlaps, which is what a shard numbered in its own private space looks like.
Such a shard cannot misattribute a family, but it also cannot dedup against a hot hit by id, so it
is a warning rather than a pass.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))


def _shard_dsn(shard_id, table=None):
    from retrieval import shard_backend
    backend = shard_backend.ShardBackend(table=shard_backend.load_table(table) if table else None)
    shard = backend.shard_for(shard_id)
    if shard is None:
        raise SystemExit(f"no shard for {shard_id!r}")
    dsn = backend.dsn(shard)
    if not dsn:
        raise SystemExit(f"{shard.vm} has no address; is it running?")
    return shard, dsn


def sample_shard(conn, n):
    """[(id, publication_number)] from the shard, spread across the table, not the first n.

    `ORDER BY id LIMIT n` would sample only the low end, which is exactly the range a renumbered
    shard and the hot corpus are most likely to agree on by accident. TABLESAMPLE SYSTEM reads
    random blocks instead, so it costs a handful of page reads rather than a scan and a sort of
    however many million rows the shard ends up holding, and it tops up from an ordinary LIMIT
    when a small table gives it too few blocks to work with.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM publications")
        total = int(_first(cur.fetchone()))
        if not total:
            return [], 0
        rows = {}
        if total > n * 4:
            pct = min(100.0, max(0.01, 400.0 * n / total))
            cur.execute("SELECT id, publication_number FROM publications "
                        f"TABLESAMPLE SYSTEM ({pct:.4f}) LIMIT %s", (n,))
            for r in cur.fetchall():
                rows[_get(r, "id", 0)] = _get(r, "publication_number", 1)
        if len(rows) < n:
            cur.execute("SELECT id, publication_number FROM publications LIMIT %s", (n,))
            for r in cur.fetchall():
                rows.setdefault(_get(r, "id", 0), _get(r, "publication_number", 1))
        return list(rows.items())[:n], total


def _get(row, name, index):
    """One column out of a row, whether the connection hands back tuples or dicts."""
    try:
        return row[name]
    except Exception:
        return row[index]


def _first(row):
    return _get(row, "count", 0)


def compare(hot_conn, rows):
    """-> (matched, mismatched, absent). Mismatched is [(id, shard_number, hot_number)]."""
    ids = [r[0] for r in rows]
    by_id = {}
    with hot_conn.cursor() as cur:
        #  A primary key lookup on a few hundred ids. Deliberately not a join or a scan: the hot
        #  corpus is a live 5M row production database and BRIEF.md forbids an unbounded scan.
        cur.execute("SELECT id, publication_number FROM publications WHERE id = ANY(%s)", (ids,))
        for r in cur.fetchall():
            by_id[_get(r, "id", 0)] = _get(r, "publication_number", 1)
    matched, mismatched, absent = 0, [], 0
    for pid, number in rows:
        hot = by_id.get(pid)
        if hot is None:
            absent += 1
        elif str(hot) == str(number):
            matched += 1
        else:
            mismatched.append((pid, number, hot))
    return matched, mismatched, absent


def verify(shard_id, sample=500, table=None):
    import psycopg

    import db
    shard, dsn = _shard_dsn(shard_id, table)
    out = {"shard": shard.shard, "vm": shard.vm, "sample_requested": sample}
    with psycopg.connect(dsn, autocommit=True) as sconn:
        rows, total = sample_shard(sconn, sample)
    out["shard_publications"] = total
    out["sampled"] = len(rows)
    if not rows:
        out["verdict"] = "empty"
        out["note"] = "the shard holds no publications; there is nothing to renumber yet"
        return 0, out
    with db.connect(readonly=True, autocommit=True) as hot:
        matched, mismatched, absent = compare(hot, rows)
    out.update(matched=matched, absent=absent,
               mismatched=[{"id": p, "on_shard": a, "in_hot_corpus": b}
                           for p, a, b in mismatched[:20]],
               n_mismatched=len(mismatched))
    if mismatched:
        out["verdict"] = "MISMATCH"
        out["note"] = ("the same publication id names different publications on the shard and in "
                       "the hot corpus. Family hydration would misattribute. Do not serve.")
        return 1, out
    if matched == 0:
        out["verdict"] = "disjoint"
        out["note"] = ("no sampled id exists in the hot corpus at all. Nothing can be "
                       "misattributed, but a cold hit cannot dedup against a hot hit by id "
                       "either, so family collapse rests entirely on the hydrated family key.")
        return 0, out
    out["verdict"] = "ok"
    return 0, out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("shard")
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--table", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        rc, out = verify(a.shard, sample=a.sample, table=a.table)
    except SystemExit:
        raise
    except Exception as e:                                                # noqa: BLE001
        print(f"verify_ids: could not run the check: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"{out['shard']}: {out['verdict']}  "
              f"sampled {out.get('sampled', 0)} of {out.get('shard_publications', 0)}  "
              f"matched {out.get('matched', 0)}  mismatched {out.get('n_mismatched', 0)}  "
              f"absent from hot {out.get('absent', 0)}")
        if out.get("note"):
            print("  " + out["note"])
        for m in out.get("mismatched", []):
            print(f"  id {m['id']}: shard says {m['on_shard']}, hot corpus says "
                  f"{m['in_hot_corpus']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
