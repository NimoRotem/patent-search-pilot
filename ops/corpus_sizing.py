#!/usr/bin/env python3
"""Print the shard sizing arithmetic. No database, no arguments, no surprises.

    ops/corpus_sizing.py                 the table
    ops/corpus_sizing.py --json          the same as data
    ops/corpus_sizing.py --shards 11     what a different fleet size would cost
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from corpus import sizing            # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--shards", type=int, default=9,
                   help="total shards including the hot one (default 9: hot + 8 domains)")
    p.add_argument("--ram-gib", type=float, default=sizing.SHARD_RAM_GIB)
    p.add_argument("--disk-gib", type=float, default=sizing.SHARD_DISK_GIB)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    r = sizing.report(a.shards, ram_gib=a.ram_gib, disk_gib=a.disk_gib)
    if a.json:
        print(json.dumps(r, indent=1))
        return 0

    asm = r["assumptions"]
    print("ASSUMPTIONS")
    for k, v in asm.items():
        print(f"  {k:28s} {v}")
    print("\nPER-SHARD CAPACITY")
    for name, cap in r["capacity"].items():
        print(f"  {name:8s} index {cap['index_bytes_per_chunk']:>6,} B/chunk   "
              f"disk {cap['disk_bytes_per_chunk']:>6,} B/chunk   "
              f"ram cap {cap['ram_cap_chunks']:>12,}   disk cap {cap['disk_cap_chunks']:>12,}   "
              f"binding: {cap['binding']}")
    print("\nSCENARIOS  (perfectly balanced split across "
          f"{asm['n_shards']} shards, which is the generous case)")
    for sc in r["scenarios"]:
        for kind in ("fp32", "halfvec"):
            v = sc[kind]
            print(f"  {sc['name']:22s} {kind:8s} {v['chunks_per_shard']:>12,} chunks/shard  "
                  f"index {v['index_gib_per_shard']:>6.1f} GiB  disk {v['disk_gib_per_shard']:>6.1f} GiB  "
                  f"{'FITS' if v['fits'] else 'DOES NOT FIT'}  needs {v['shards_needed']} shards")
        print(f"  {'':22s} assumes: {sc['assumes']}")
        print(f"  {'':22s} builder maintenance_work_mem for one shard's HNSW: "
              f"{sc['build_ram_gib_fp32']} GiB")
    be = r["break_even"]
    print("\nBREAK EVEN")
    print(f"  {asm['n_shards']} shards hold at most {be['max_total_chunks']:,} chunks, which is "
          f"{be['max_fully_texted_publications']:,} fully texted publications")
    print(f"  the corpus has {be['corpus_publications']:,} publications, so that is "
          f"{be['coverage_of_corpus']:.1%} description coverage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
