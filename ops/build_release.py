#!/usr/bin/env python3
"""Build, inspect, activate and roll back immutable corpus releases.

    # once per build: mirror the light tables and place every family
    ops/build_release.py mirror  --id-hi 400000 --refresh
    ops/build_release.py assign
    ops/build_release.py plan    --shards 8

    # one release at a time
    ops/build_release.py build --shard-key hot        --kind hot --niche --max-families 400
    ops/build_release.py build --shard-key domain_01  --domains B65G B25J --max-families 400

    # the switch
    ops/build_release.py verify   hot_v1
    ops/build_release.py activate hot_v1 domain_01_v1 --actor nimo --reason "first cutover"
    ops/build_release.py rollback hot

TWO DSNs, BOTH EXPLICIT.
    CORPUS_RELEASE_DSN   the builder / release database. NEVER production. No default exists.
    CORPUS_SOURCE_DSN    the corpus to read. Falls back to PGHOST/PGPORT/... from .env, which is
                         the live corpus and is opened READ ONLY.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from corpus import builder, demand, release_store, sizing, source as source_mod   # noqa: E402
from corpus import stats as stats_mod                                            # noqa: E402


def _log(ev):
    print(json.dumps(ev, sort_keys=True, default=str), flush=True)


def _dst():
    return release_store.connect()


def _src():
    from dotenv import load_dotenv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))
    return source_mod.connect_source()


def cmd_mirror(a):
    src, dst = _src(), _dst()
    r = source_mod.mirror_light_tables(src, dst, id_lo=a.id_lo, id_hi=a.id_hi, batch=a.batch,
                                       refresh=a.refresh, progress=_log)
    print(json.dumps(r, indent=1))


def cmd_assign(a):
    dst = _dst()
    r = source_mod.assign_family_homes(dst, progress=_log)
    print(json.dumps(r, indent=1))
    if not a.no_counts:
        src = _src()
        c = source_mod.fill_family_chunk_counts(src, dst, batch=a.batch, progress=_log)
        print(json.dumps(c, indent=1))


def cmd_plan(a):
    from config import SEED_CPC
    dst = _dst()
    cap = sizing.chunks_per_shard(halfvec=a.halfvec)["cap_chunks"]
    out = builder.plan_fleet(dst, n_domain_shards=a.shards,
                             niche_patterns=[c + "%" for c in SEED_CPC] if a.niche else (),
                             capacity=cap, unclassified_splits=a.unclassified_splits)
    plan = out["plan"]
    print(f"per-shard capacity: {cap:,} chunks  (halfvec={a.halfvec})")
    print(f"hot/niche: {out['hot']['families']:,} families, {out['hot']['chunks']:,} chunks")
    for k in sorted(plan.shards):
        doms = plan.shards[k]
        print(f"  {k}: {plan.mass[k]:>12,} chunks  {len(doms):>4} domains  "
              f"{'OVER CAPACITY' if k in plan.oversized else ''}  {','.join(sorted(doms)[:6])}"
              f"{'...' if len(doms) > 6 else ''}")
    print(f"fits: {plan.fits}")
    if a.json:
        json.dump({"plan": plan.to_dict(), "hot": {k: v for k, v in out["hot"].items()
                                                   if k != "family_keys"},
                   "domain_mass": out["domain_mass"]}, open(a.json, "w"), indent=1, default=str)


def cmd_build(a):
    from config import SEED_CPC
    src, dst = _src(), _dst()
    family_keys = None
    if a.niche:
        family_keys = [r["family_key"] for r in source_mod.families_matching_symbols(
            dst, [c + "%" for c in SEED_CPC], limit=a.max_families)]
    r = builder.build_release(
        src, dst, shard_key=a.shard_key, kind=a.kind, domains=a.domains or (),
        family_keys=family_keys, max_families=a.max_families, halfvec=a.halfvec, m=a.m,
        ef_construction=a.ef_construction, snapshot_dir=a.snapshot_dir, demand_limit=a.demand,
        note=a.note, maintenance_work_mem=a.maintenance_work_mem,
        parallel_workers=a.parallel_workers, log=_log, snapshot=not a.no_snapshot)
    print(json.dumps({"release_id": r["release_id"],
                      "content_hash": r["manifest"]["content_hash"],
                      "counts": r["counts"], "timings": r["timings"],
                      "snapshot": {k: v for k, v in (r["snapshot"] or {}).items()
                                   if k != "artifacts"}}, indent=1, default=str))
    print(stats_mod.summary_line(r["stats"]))


def cmd_stats(a):
    dst = _dst()
    #  A sealed release's statistics are what it was SEALED with. Recomputing by default would
    #  quietly report a different lexical coverage (this process has no Tantivy document count)
    #  and would be the number an operator compares releases on.
    rel = release_store.get(dst, a.release_id)
    if rel and rel["sealed_at"] and not a.recompute:
        print(json.dumps(rel["stats"], indent=1, default=str))
        return
    print(json.dumps(stats_mod.compute(dst, a.release_id), indent=1, default=str))


def cmd_verify(a):
    dst = _dst()
    rel = release_store.get(dst, a.release_id)
    root = (rel or {}).get("snapshot_path") or None
    v = release_store.verify_serving(dst, a.release_id, artifact_root=a.artifact_root or root)
    print(json.dumps(v.to_dict(), indent=1))
    sys.exit(0 if v.ok else 1)


def cmd_activate(a):
    dst = release_store.connect(autocommit=False)
    try:
        r = release_store.activate_many(dst, a.release_ids, actor=a.actor, reason=a.reason,
                                        force=a.force)
    except release_store.StatsRegression as exc:
        print(json.dumps({"refused": str(exc), "regressions": exc.regressions}, indent=1))
        sys.exit(2)
    print(json.dumps(r, indent=1, default=str))


def cmd_rollback(a):
    dst = release_store.connect(autocommit=False)
    print(json.dumps(release_store.rollback(dst, a.shard_key, actor=a.actor, reason=a.reason),
                     indent=1, default=str))


def cmd_active(a):
    dst = _dst()
    print(json.dumps(release_store.active_set(dst), indent=1, default=str))


def cmd_restore(a):
    dst = _dst()
    print(json.dumps(builder.restore_snapshot(dst, a.release_id, a.snapshot_dir, log=_log),
                     indent=1, default=str))


def cmd_demand(a):
    dst = _dst()
    print(json.dumps({"pending": demand.pending(limit=a.limit),
                      "ledger": demand.ledger_summary(dst)}, indent=1, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mirror", help="copy publication identity and CPC symbols to the builder")
    m.add_argument("--id-lo", type=int, default=0)
    m.add_argument("--id-hi", type=int, default=None)
    m.add_argument("--batch", type=int, default=50_000)
    m.add_argument("--refresh", action="store_true")
    m.set_defaults(fn=cmd_mirror)

    s = sub.add_parser("assign", help="place every family in its home domain")
    s.add_argument("--batch", type=int, default=5000)
    s.add_argument("--no-counts", action="store_true", help="skip the chunk-count pass")
    s.set_defaults(fn=cmd_assign)

    pl = sub.add_parser("plan", help="pack domains into shards and check the capacity")
    pl.add_argument("--shards", type=int, default=8)
    pl.add_argument("--halfvec", action="store_true")
    pl.add_argument("--niche", action="store_true", default=True)
    pl.add_argument("--unclassified-splits", type=int, default=1)
    pl.add_argument("--json", default="")
    pl.set_defaults(fn=cmd_plan)

    b = sub.add_parser("build", help="build and seal one release")
    b.add_argument("--shard-key", required=True)
    b.add_argument("--kind", default="domain", choices=("hot", "domain", "niche"))
    b.add_argument("--domains", nargs="*", default=[])
    b.add_argument("--niche", action="store_true")
    b.add_argument("--max-families", type=int, default=None)
    b.add_argument("--halfvec", action="store_true")
    b.add_argument("--m", type=int, default=16)
    b.add_argument("--ef-construction", type=int, default=64)
    b.add_argument("--snapshot-dir", default=None)
    b.add_argument("--no-snapshot", action="store_true")
    b.add_argument("--demand", type=int, default=0, help="consume N ingest-queue requests")
    b.add_argument("--maintenance-work-mem", default=None)
    b.add_argument("--parallel-workers", type=int, default=None)
    b.add_argument("--note", default="")
    b.set_defaults(fn=cmd_build)

    st = sub.add_parser("stats"); st.add_argument("release_id")
    st.add_argument("--recompute", action="store_true", help="recompute instead of reading the "
                                                             "statistics the release was sealed with")
    st.set_defaults(fn=cmd_stats)
    v = sub.add_parser("verify"); v.add_argument("release_id")
    v.add_argument("--artifact-root", default=""); v.set_defaults(fn=cmd_verify)

    ac = sub.add_parser("activate"); ac.add_argument("release_ids", nargs="+")
    ac.add_argument("--actor", default=os.environ.get("USER", ""))
    ac.add_argument("--reason", default=""); ac.add_argument("--force", action="store_true")
    ac.set_defaults(fn=cmd_activate)

    rb = sub.add_parser("rollback"); rb.add_argument("shard_key")
    rb.add_argument("--actor", default=os.environ.get("USER", ""))
    rb.add_argument("--reason", default=""); rb.set_defaults(fn=cmd_rollback)

    sub.add_parser("active").set_defaults(fn=cmd_active)

    rs = sub.add_parser("restore"); rs.add_argument("release_id")
    rs.add_argument("--snapshot-dir", default=builder.DEFAULT_SNAPSHOT_DIR)
    rs.set_defaults(fn=cmd_restore)

    dm = sub.add_parser("demand"); dm.add_argument("--limit", type=int, default=50)
    dm.set_defaults(fn=cmd_demand)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    main()
