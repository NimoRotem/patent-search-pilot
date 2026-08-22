"""How much corpus fits on a shard, and therefore how many shards there have to be.

THIS IS THE ARITHMETIC THE WHOLE REBUILD RESTS ON. V3 exists because a 101 GB HNSW index is being
queried on a 62 GB box, so every dense query pays disk. Rebuilding into shards only fixes that if
each shard's index actually fits that shard's RAM. A shard whose index is larger than its RAM has
reproduced the original defect with more machines and more moving parts.

EVERY CONSTANT BELOW IS MEASURED, NOT ESTIMATED, and the measurement is named beside it. Where a
number is a judgement (how much RAM to leave for the operating system) it says so and is a
parameter, not a literal buried in a formula.

    MEASURED 2026-08-22, live corpus `patents` on 10.128.0.53:5433, from pg_class/pg_relation_size:

        chunks                    27,622,168 rows
        ix_chunks_hnsw           101,210,628,096 B   ->  3,664 B/chunk   vector(768), m=16, efc=64
        ix_chunks_hnsw_half       50,576,965,632 B   ->  1,831 B/chunk   halfvec(768), same params
        ix_chunks_tsv (GIN)        4,424,835,072 B   ->    160 B/chunk   replaced by Tantivy in v3
        chunks_pkey                1,220,747,264 B   ->     44 B/chunk
        ix_chunks_pub                616,538,112 B   ->     22 B/chunk
        ix_chunks_kind               447,692,800 B   ->     16 B/chunk
        chunks heap               39,593,943,040 B   ->  1,433 B/chunk
        chunks toast             112,109,125,632 B   ->  4,059 B/chunk   (the vector, mostly)
        chunks total             311,772,233,728 B   -> 11,287 B/chunk

        chunks_stage_v3            5,971,522 rows, heap 1,441 B/row, toast 4,108 B/row.
        The stage table has NO tsv column, which is the shape `chunks_release` has, and it measures
        the same heap+toast per row as `chunks` does. Dropping the generated tsvector therefore
        saves the 160 B/chunk of the GIN index and essentially nothing else.

The brief's per-chunk index cost of 3,654 B is the same measurement taken slightly earlier; the
index has grown 0.3% since. `INDEX_BYTES_PER_CHUNK` keeps the brief's figure so the arithmetic in
this file can be checked against the arithmetic in the brief, and `MEASURED` carries the current
one.

A FRESHLY BUILT INDEX IS SMALLER THAN THE LIVE ONE. MEASURED 2026-08-22 on release `hot_v1`:
61,513 chunks, `ix_chunks_release_hot_v1_hnsw` 196,952,064 B = **3,202 B/chunk**, 12.6% under the
live index's 3,664. Same m, same ef_construction, same dimension. The difference is that the live
graph was grown by insert while being queried and this one was built once over a static table,
which is the argument for immutable releases stated in bytes. The planning constant stays at the
larger figure: a capacity estimate should not assume the best case.
"""
from __future__ import annotations

import math

GiB = 1024 ** 3

#  The brief's measured figure, kept so this file and the brief can be reconciled line by line.
INDEX_BYTES_PER_CHUNK = 3654

MEASURED = {
    "chunk_rows": 27_622_168,
    "hnsw_fp32_bytes_per_chunk": 3664,
    "hnsw_halfvec_bytes_per_chunk": 1831,
    "tsv_gin_bytes_per_chunk": 160,
    "pkey_bytes_per_chunk": 44,
    "pub_index_bytes_per_chunk": 22,
    "kind_index_bytes_per_chunk": 16,
    "heap_bytes_per_chunk": 1441,       # chunks_stage_v3, the no-tsv shape a release has
    "toast_bytes_per_chunk": 4108,      # ditto: the 768-dim vector plus spilled text
    "avg_chunk_chars": 902,             # chunks TABLESAMPLE SYSTEM (0.05), n=13,695
    "publications": 4_975_809,
    "chunks_per_texted_publication": 56.1,
    "description_paragraphs": 14_379_018,
    "description_paragraphs_chunked": 1_689_243,
    "unclassified_publications": 1_024_320,
    "distinct_families": 3_409_514,          # exact, count(DISTINCT simple_family_id)
    "fresh_hnsw_bytes_per_chunk": 3202,      # release hot_v1, built once over a static table
    "lexical_bytes_per_doc_measured": 314.5,  # hot_v1, 699 chars/doc, text indexed not stored
}

#  MEASURED 2026-08-22 by building a Tantivy 0.26 index over release `hot_v1` on this box, text
#  indexed but NOT stored (the snippet is fetched from the release database by chunk_id, which is
#  one primary-key lookup, rather than storing 902 B/chunk a second time):
#      61,513 documents, 19,347,024 bytes on disk = 314.5 B/doc at 699 chars/doc.
#  The corpus mean is 902 chars/chunk, so scaling by 902/699 gives 406 B/chunk. Scaling linearly
#  overstates it, because a bigger index shares more of its term dictionary, and overstating a
#  capacity input is the safe direction.
LEXICAL_BYTES_PER_CHUNK = 406

#  Judgement calls, all parameters rather than literals in a formula.
#  RAM the operating system, the shard manager, the wake agent and the Tantivy process need before
#  Postgres sees any of it.
DEFAULT_OS_RESERVE_GIB = 6.0
#  Connection working memory: max_connections * work_mem worst case, plus sort and hash space.
DEFAULT_BACKEND_RESERVE_GIB = 4.0
#  Share of the remaining cache that must stay available for heap and toast pages. A dense scan
#  returns tuple ids; the executor then reads the heap for every row it returns and every row it
#  filters. Give that nothing and the index is resident but the query still pays disk.
DEFAULT_HEAP_CACHE_FRACTION = 0.15
#  Disk left for WAL, a checkpoint spike, the snapshot being staged or restored, and the OS.
DEFAULT_DISK_RESERVE_GIB = 20.0

#  The shard the architecture specifies.
SHARD_RAM_GIB = 124.0
SHARD_DISK_GIB = 250.0


def index_bytes_per_chunk(*, halfvec=False, lexical=True, extra_btrees=True):
    """Serving bytes of INDEX per chunk on a shard.

    The generated tsvector and its GIN index are not in the list: v3 replaces
    `to_tsvector('english')` with Tantivy, which returns zero gold families today and is blind to
    the 39.9% of the corpus that is CJK. Tantivy's cost is counted instead.
    """
    n = MEASURED["hnsw_halfvec_bytes_per_chunk"] if halfvec else INDEX_BYTES_PER_CHUNK
    if extra_btrees:
        n += MEASURED["pkey_bytes_per_chunk"] + MEASURED["pub_index_bytes_per_chunk"] \
            + MEASURED["kind_index_bytes_per_chunk"]
    if lexical:
        n += LEXICAL_BYTES_PER_CHUNK
    return n


def disk_bytes_per_chunk(*, halfvec=False, lexical=True, extra_btrees=True):
    """Everything a shard stores per chunk: the row, its toasted vector, and every index."""
    return (MEASURED["heap_bytes_per_chunk"] + MEASURED["toast_bytes_per_chunk"]
            + index_bytes_per_chunk(halfvec=halfvec, lexical=lexical, extra_btrees=extra_btrees))


def resident_index_budget_bytes(ram_gib=SHARD_RAM_GIB, *, os_reserve_gib=DEFAULT_OS_RESERVE_GIB,
                                backend_reserve_gib=DEFAULT_BACKEND_RESERVE_GIB,
                                heap_cache_fraction=DEFAULT_HEAP_CACHE_FRACTION):
    """Bytes of RAM a shard can actually keep index pages in.

    Not `ram_gib`. Handing the whole machine to the index is the assumption that produced the
    current situation, where the index is nominally 101 GB on a 62 GB box and the answer to
    'does it fit' was never written down.
    """
    usable = max(0.0, float(ram_gib) - float(os_reserve_gib) - float(backend_reserve_gib))
    return usable * (1.0 - float(heap_cache_fraction)) * GiB


def usable_disk_bytes(disk_gib=SHARD_DISK_GIB, *, reserve_gib=DEFAULT_DISK_RESERVE_GIB):
    return max(0.0, float(disk_gib) - float(reserve_gib)) * GiB


def chunks_per_shard(ram_gib=SHARD_RAM_GIB, disk_gib=SHARD_DISK_GIB, *, halfvec=False,
                     lexical=True, **kw):
    """-> the per-shard chunk ceiling, and WHICH resource sets it.

    Two ceilings, and the smaller one is the answer. Reporting only the RAM ceiling is how a plan
    that fits in memory and does not fit on the disk gets built.
    """
    ram_kw = {k: v for k, v in kw.items()
              if k in ("os_reserve_gib", "backend_reserve_gib", "heap_cache_fraction")}
    disk_kw = {"reserve_gib": kw["disk_reserve_gib"]} if "disk_reserve_gib" in kw else {}
    ipc = index_bytes_per_chunk(halfvec=halfvec, lexical=lexical)
    dpc = disk_bytes_per_chunk(halfvec=halfvec, lexical=lexical)
    ram_cap = int(resident_index_budget_bytes(ram_gib, **ram_kw) // ipc)
    disk_cap = int(usable_disk_bytes(disk_gib, **disk_kw) // dpc)
    cap = min(ram_cap, disk_cap)
    return {"ram_cap_chunks": ram_cap, "disk_cap_chunks": disk_cap, "cap_chunks": cap,
            "binding": "ram" if ram_cap <= disk_cap else "disk",
            "index_bytes_per_chunk": ipc, "disk_bytes_per_chunk": dpc,
            "resident_index_budget_gib": round(resident_index_budget_bytes(ram_gib, **ram_kw) / GiB, 1),
            "usable_disk_gib": round(usable_disk_bytes(disk_gib, **disk_kw) / GiB, 1),
            "halfvec": bool(halfvec)}


def corpus_scenarios():
    """The chunk counts the eight-way split has to carry, smallest first.

    Each one states what it assumes, because the difference between them is the difference between
    'eight shards is generous' and 'eight shards does not fit'.
    """
    today = MEASURED["chunk_rows"]
    backlog = MEASURED["description_paragraphs"] - MEASURED["description_paragraphs_chunked"]
    full = int(round(MEASURED["publications"] * MEASURED["chunks_per_texted_publication"]))
    return [
        {"name": "today", "chunks": today,
         "assumes": "the corpus exactly as it is on 2026-08-22"},
        {"name": "stored_descriptions",
         "chunks": today + backlog,
         "assumes": "the description backfill finishes the 14,379,018 paragraphs already in "
                    "`paragraphs`, of which 1,689,243 are chunked"},
        {"name": "full_text_corpus", "chunks": full,
         "assumes": "every one of the 4,975,809 publications reaches full text at the measured "
                    "56.1 chunks per fully texted publication"},
    ]


def shards_needed(total_chunks, *, ram_gib=SHARD_RAM_GIB, disk_gib=SHARD_DISK_GIB, halfvec=False,
                  lexical=True, **kw):
    cap = chunks_per_shard(ram_gib, disk_gib, halfvec=halfvec, lexical=lexical, **kw)
    n = int(math.ceil(float(total_chunks) / cap["cap_chunks"])) if cap["cap_chunks"] else 0
    return {"total_chunks": int(total_chunks), "shards_needed": n, **cap}


def plan_verdict(total_chunks, n_shards, *, ram_gib=SHARD_RAM_GIB, disk_gib=SHARD_DISK_GIB,
                 halfvec=False, lexical=True, **kw):
    """Does `n_shards` hold `total_chunks`, assuming a PERFECTLY balanced split?

    Perfectly balanced is the generous case and is deliberate: if the plan fails even when the
    packing is ideal, no packing saves it. `assign.pack_domains` reports the real imbalance.
    """
    cap = chunks_per_shard(ram_gib, disk_gib, halfvec=halfvec, lexical=lexical, **kw)
    per = float(total_chunks) / max(1, int(n_shards))
    return {"total_chunks": int(total_chunks), "n_shards": int(n_shards),
            "chunks_per_shard": int(round(per)),
            "index_gib_per_shard": round(per * cap["index_bytes_per_chunk"] / GiB, 1),
            "disk_gib_per_shard": round(per * cap["disk_bytes_per_chunk"] / GiB, 1),
            "fits": per <= cap["cap_chunks"],
            "headroom_chunks": int(cap["cap_chunks"] - per),
            "shards_needed": int(math.ceil(float(total_chunks) / cap["cap_chunks"]))
                             if cap["cap_chunks"] else 0,
            **cap}


def build_ram_required_gib(chunks, *, halfvec=False):
    """RAM the OFFLINE BUILDER needs to build one shard's HNSW in memory.

    pgvector builds the graph in `maintenance_work_mem` and falls back to a much slower two-phase
    on-disk build when it does not fit. So the builder box has to be at least as big as the shard
    it is building for, which is not obvious and is the reason a 31 GB box can only build small
    releases.
    """
    ipc = MEASURED["hnsw_halfvec_bytes_per_chunk"] if halfvec else INDEX_BYTES_PER_CHUNK
    return round(chunks * ipc / GiB, 1)


def report(n_shards=9, **kw):
    """The whole table, as data. `ops/corpus_sizing.py` prints it."""
    out = {"assumptions": {
        "shard_ram_gib": kw.get("ram_gib", SHARD_RAM_GIB),
        "shard_disk_gib": kw.get("disk_gib", SHARD_DISK_GIB),
        "os_reserve_gib": kw.get("os_reserve_gib", DEFAULT_OS_RESERVE_GIB),
        "backend_reserve_gib": kw.get("backend_reserve_gib", DEFAULT_BACKEND_RESERVE_GIB),
        "heap_cache_fraction": kw.get("heap_cache_fraction", DEFAULT_HEAP_CACHE_FRACTION),
        "disk_reserve_gib": kw.get("disk_reserve_gib", DEFAULT_DISK_RESERVE_GIB),
        "index_bytes_per_chunk": INDEX_BYTES_PER_CHUNK,
        "lexical_bytes_per_chunk": LEXICAL_BYTES_PER_CHUNK,
        "n_shards": n_shards,
    }, "capacity": {}, "scenarios": []}
    for hv in (False, True):
        out["capacity"]["halfvec" if hv else "fp32"] = chunks_per_shard(halfvec=hv, **kw)
    for sc in corpus_scenarios():
        row = {"name": sc["name"], "assumes": sc["assumes"], "chunks": sc["chunks"]}
        for hv in (False, True):
            row["halfvec" if hv else "fp32"] = plan_verdict(sc["chunks"], n_shards, halfvec=hv, **kw)
        row["build_ram_gib_fp32"] = build_ram_required_gib(sc["chunks"] / max(1, n_shards))
        out["scenarios"].append(row)
    #  The number that answers 'how far does eight go': the corpus size at which a perfectly
    #  balanced split of this many shards stops fitting.
    cap = chunks_per_shard(**kw)
    out["break_even"] = {
        "max_total_chunks": cap["cap_chunks"] * n_shards,
        "max_fully_texted_publications":
            int(cap["cap_chunks"] * n_shards / MEASURED["chunks_per_texted_publication"]),
        "corpus_publications": MEASURED["publications"],
    }
    out["break_even"]["coverage_of_corpus"] = round(
        out["break_even"]["max_fully_texted_publications"] / MEASURED["publications"], 4)
    return out
