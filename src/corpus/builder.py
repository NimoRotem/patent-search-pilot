"""The offline release builder, end to end.

    staged chunks -> family home-shard assignment -> pgvector load -> HNSW build
                  -> Tantivy build -> completeness stats -> disk snapshot -> release manifest

NOTHING HERE MAY RUN AGAINST A SERVING DATABASE. The dense index build is the reason: pgvector
holds the whole HNSW graph in `maintenance_work_mem` while it builds, and building one on the live
corpus is the outage this rebuild exists to prevent. `release_store.dsn()` has no default for the
same reason: a default that happens to be production is how that happens by accident.

WHAT "STAGED" MEANS HERE. Three inputs, none of which is a live index:
    * `chunks` on the source corpus, read only, batched by key range;
    * `chunks_stage_v3`, the description backfill's output, which has no vector index at all;
    * `corpus_ingest_queue` plus `sources_docstore`, the demand loop, chunked and embedded here.

WHAT A BUILD PRODUCES. A sealed row in `corpus_release`, its partition of `chunks_release` with a
dense index, a Tantivy directory, a completeness statistics block, a snapshot directory with
checksums, and a manifest whose content hash covers all of it. Activation is a separate, deliberate
step: `release_store.activate`.
"""
from __future__ import annotations

import os
import gzip
import shutil
import subprocess
import tarfile
import time

from corpus import assign, demand as demand_mod, lexical_build, manifest as manifest_mod
from corpus import release_store, sizing, source as source_mod

DEFAULT_SNAPSHOT_DIR = os.environ.get(
    "CORPUS_SNAPSHOT_DIR", os.path.join(os.path.expanduser("~"), "v3-releases"))

#  The corpus was embedded with this before the stage table started recording it per row. Stamped
#  onto rows sourced from `chunks`, which has no model column, so a release never claims a
#  provenance it does not have and never silently mixes two models in one index.
LEGACY_EMBED_MODEL = os.environ.get("CORPUS_LEGACY_EMBED_MODEL", "gemini-embedding-001")
LEGACY_EMBED_DIM = 768

RELEASE_COLS = ("release_id", "chunk_id", "family_key", "publication_id", "publication_number",
                "home_domain", "kind", "ref_id", "coord", "lang", "text", "token_count",
                "embedding", "embed_model", "embed_dim", "task_type", "chunker_version", "source")


class BuildError(RuntimeError):
    pass


def _vec(v):
    """pgvector's text form. The source hands vectors back as strings already; a demand chunk
    arrives as a list of floats."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"


def _log(fn, event, **kv):
    if fn:
        fn({"event": event, **kv})


# ---------------------------------------------------------------------------------------------
# the fleet plan
# ---------------------------------------------------------------------------------------------
def plan_fleet(dst, *, n_domain_shards=8, niche_patterns=(), capacity=None,
               unclassified_splits=1, projected=False):
    """Which domains each cold shard holds, and what the niche takes out first.

    The niche comes out first because it is defined at SUBGROUP granularity while shards split at
    subclass granularity: `B65G47/91` is the niche, `B65G` is 12.7% of every classification row in
    the corpus and is not. Taking the niche out by symbol and then packing what is left by domain
    is the only order in which both statements stay true.
    """
    mass = source_mod.domain_mass(dst, projected=projected)
    hot_keys = set()
    if niche_patterns:
        hot_keys = {r["family_key"] for r in
                    source_mod.families_matching_symbols(dst, list(niche_patterns))}
    hot_mass = {"families": 0, "publications": 0, "chunks": 0}
    if hot_keys:
        with dst.cursor() as cur:
            col = "n_chunks + n_backlog" if projected else "n_chunks"
            cur.execute(f"""SELECT home_domain d, count(*) f, COALESCE(sum(n_publications),0) p,
                                   COALESCE(sum({col}),0) c
                            FROM src_family_home WHERE family_key = ANY(%s) GROUP BY 1""",
                        (list(hot_keys),))
            for r in cur.fetchall():
                d = r["d"]
                hot_mass["families"] += int(r["f"])
                hot_mass["publications"] += int(r["p"])
                hot_mass["chunks"] += int(r["c"])
                if d in mass:
                    mass[d] = {"families": mass[d]["families"] - int(r["f"]),
                               "publications": mass[d]["publications"] - int(r["p"]),
                               "chunks": mass[d]["chunks"] - int(r["c"])}
    chunk_mass = {d: max(0, v["chunks"]) for d, v in mass.items()}
    plan = assign.pack_domains(chunk_mass, n_domain_shards, capacity=capacity,
                               unclassified_splits=unclassified_splits)
    return {"plan": plan, "domain_mass": mass, "hot": {**hot_mass, "family_keys": sorted(hot_keys)},
            "capacity": capacity, "projected": bool(projected)}


# ---------------------------------------------------------------------------------------------
# one release
# ---------------------------------------------------------------------------------------------
def build_release(src, dst, *, shard_key, kind="domain", domains=(), family_keys=None,
                  max_families=None, version=None, halfvec=False, m=16, ef_construction=64,
                  snapshot_dir=None, store_text=False, demand_limit=0, note="",
                  maintenance_work_mem=None, parallel_workers=None, log=None,
                  embed_module=None, docstore=None, runstore=None, snapshot=True):
    """Build ONE release and seal it. Does not activate: that is a separate decision.

    `domains`      the CPC domains this shard holds. Families are selected by HOME domain, so a
                   family that merely mentions the domain is not pulled in and no family is split.
    `family_keys`  an explicit family set, used for the niche, which is defined by symbol rather
                   than by domain.
    `demand_limit` how many `corpus_ingest_queue` requests to consume into this release, highest
                   demand first.
    """
    t_all = time.time()
    timings = {}
    snapshot_dir = snapshot_dir or DEFAULT_SNAPSHOT_DIR

    # ---- 1. selection -------------------------------------------------------------------
    t0 = time.time()
    if family_keys is None:
        rows = source_mod.families_in_domains(dst, list(domains), limit=max_families)
    else:
        keys = list(family_keys)[:max_families] if max_families else list(family_keys)
        with dst.cursor() as cur:
            cur.execute("SELECT * FROM src_family_home WHERE family_key = ANY(%s) "
                        "ORDER BY n_chunks DESC, family_key", (keys,))
            rows = cur.fetchall()
    if not rows:
        raise BuildError(f"{shard_key}: no families selected; nothing to build")
    fam_by_key = {r["family_key"]: r for r in rows}
    pubs = source_mod.publication_ids_for_families(dst, list(fam_by_key))
    pub_family = {int(p["id"]): p["family_key"] for p in pubs}
    pub_number = {int(p["id"]): p["publication_number"] for p in pubs}
    timings["select_s"] = round(time.time() - t0, 2)
    _log(log, "selected", families=len(fam_by_key), publications=len(pubs), **timings)

    # ---- 2. the release row and its partition -------------------------------------------
    rel = release_store.create(dst, shard_key=shard_key, version=version, kind=kind,
                               builder=manifest_mod.builder_identity().get("host", ""), note=note)
    release_id = rel["release_id"]
    release_store.ensure_partition(dst, release_id)
    dst.commit()
    _log(log, "release_open", release_id=release_id)

    # ---- 3. the demand loop -------------------------------------------------------------
    demand_report = {"requested": 0, "chunks": 0}
    demand_chunks = []
    if demand_limit:
        t0 = time.time()
        claimed = demand_mod.claim(limit=int(demand_limit), runstore=runstore)
        demand_mod.record(dst, claimed, release_id="", state="fetched")
        demand_chunks, demand_report = demand_mod.materialise(
            claimed, embed_module=embed_module, docstore=docstore, model=LEGACY_EMBED_MODEL)
        timings["demand_s"] = round(time.time() - t0, 2)
        _log(log, "demand", **{k: v for k, v in demand_report.items() if k != "missing"})

    # ---- 4. the pgvector load -----------------------------------------------------------
    t0 = time.time()
    chunk_stats = {}
    n = 0
    kinds = {}
    with dst.cursor() as cur:
        with cur.copy(f"COPY chunks_release ({', '.join(RELEASE_COLS)}) FROM STDIN") as cp:
            for row in source_mod.iter_chunks(src, list(pub_family), stats=chunk_stats,
                                              legacy_model=LEGACY_EMBED_MODEL,
                                              legacy_dim=LEGACY_EMBED_DIM):
                pid = int(row["publication_id"])
                fk = pub_family.get(pid)
                if fk is None:
                    continue
                n += 1
                kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
                cp.write_row((release_id, n, fk, pid, pub_number.get(pid, ""),
                              fam_by_key[fk]["home_domain"], row["kind"], row["ref_id"],
                              _json(row["coord"]), row["lang"] or "", row["text"],
                              row["token_count"], _vec(row["embedding"]),
                              row.get("embed_model") or LEGACY_EMBED_MODEL,
                              int(row.get("embed_dim") or LEGACY_EMBED_DIM),
                              row.get("task_type") or "", row.get("chunker_version") or "",
                              row.get("source") or "corpus"))
            included_demand = set()
            for row in demand_chunks:
                pn = row["publication_number"]
                fk = f"demand:{pn}"
                n += 1
                kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
                included_demand.add(pn)
                cp.write_row((release_id, n, fk, 0, pn, assign.UNCLASSIFIED, row["kind"], None,
                              _json(row["coord"]), row["lang"] or "", row["text"],
                              row["token_count"], _vec(row["embedding"]), row["embed_model"],
                              int(row["embed_dim"]), row["task_type"], row["chunker_version"],
                              "demand"))
    dst.commit()
    timings["load_s"] = round(time.time() - t0, 2)
    _log(log, "loaded", chunks=n, **chunk_stats, **{"load_s": timings["load_s"]})
    if not n:
        raise BuildError(f"{release_id}: selected {len(fam_by_key)} families but loaded 0 chunks")

    # ---- 5. members and per-domain rows -------------------------------------------------
    t0 = time.time()
    with dst.cursor() as cur:
        cur.execute("""SELECT family_key, count(*) c, count(DISTINCT publication_id) p
                       FROM chunks_release WHERE release_id=%s GROUP BY 1""", (release_id,))
        loaded = {r["family_key"]: (int(r["c"]), int(r["p"])) for r in cur.fetchall()}
    members = []
    for fk, fam in fam_by_key.items():
        c, p = loaded.get(fk, (0, 0))
        if not c:
            continue                      # a family whose every chunk lacked an embedding
        members.append({"family_key": fk, "home_domain": fam["home_domain"],
                        "secondary_domains": list(fam["secondary_domains"] or []),
                        "n_publications": p, "n_chunks": c, "source": "corpus"})
    for pn in sorted(included_demand):
        fk = f"demand:{pn}"
        c, p = loaded.get(fk, (0, 0))
        if c:
            members.append({"family_key": fk, "home_domain": assign.UNCLASSIFIED,
                            "secondary_domains": [], "n_publications": 1, "n_chunks": c,
                            "source": "demand"})
    release_store.add_members(dst, release_id, members)
    held_domains = sorted({mrow["home_domain"] for mrow in members} | set(domains or []))
    dst.commit()
    timings["members_s"] = round(time.time() - t0, 2)

    # ---- 6. the dense index -------------------------------------------------------------
    t0 = time.time()
    index_params = release_store.build_indexes(
        dst, release_id, m=m, ef_construction=ef_construction, halfvec=halfvec,
        maintenance_work_mem=maintenance_work_mem, parallel_workers=parallel_workers)
    dst.commit()
    timings["dense_index_s"] = round(time.time() - t0, 2)
    _log(log, "dense_index", **index_params, seconds=timings["dense_index_s"])

    # ---- 7. the Tantivy index -----------------------------------------------------------
    t0 = time.time()
    lex_dir = os.path.join(snapshot_dir, release_id, "lexical")
    lexical = {"docs": 0, "bytes": 0, "available": lexical_build.available()}
    if lexical_build.available():
        shutil.rmtree(lex_dir, ignore_errors=True)
        os.makedirs(lex_dir, exist_ok=True)
        lexical = lexical_build.build(lex_dir, _iter_lexical_docs(dst, release_id),
                                      store_text=store_text)
        lexical["available"] = True
    else:
        _log(log, "lexical_skipped", reason=lexical_build.TANTIVY_ERROR)
    timings["lexical_s"] = round(time.time() - t0, 2)
    _log(log, "lexical", docs=lexical.get("docs"), bytes=lexical.get("bytes"),
         seconds=timings["lexical_s"])

    # ---- 8. per-domain counts and completeness stats -------------------------------------
    t0 = time.time()
    _write_domain_rows(dst, release_id, held_domains)
    if included_demand:
        demand_mod.set_state(dst, sorted(included_demand), release_id=release_id,
                             state="included")
        missing = [p for p in (demand_report.get("missing") or [])]
        if missing:
            demand_mod.set_state(dst, missing, release_id=release_id, state="missing",
                                 note="scratch store held no usable text")
    from corpus import stats as stats_mod
    stat_block = stats_mod.compute(dst, release_id,
                                   lexical_docs=lexical.get("docs") if lexical.get("available")
                                   else None)
    timings["stats_s"] = round(time.time() - t0, 2)
    _log(log, "stats", line=stats_mod.summary_line(stat_block))

    # ---- 9. the snapshot ----------------------------------------------------------------
    artifacts, snap = [], {}
    if snapshot:
        t0 = time.time()
        snap = write_snapshot(dst, release_id, snapshot_dir, lexical_dir=lex_dir if
                              lexical.get("available") else None)
        artifacts = snap["artifacts"]
        timings["snapshot_s"] = round(time.time() - t0, 2)
        _log(log, "snapshot", **{k: v for k, v in snap.items() if k != "artifacts"})

    # ---- 10. the manifest, and the seal --------------------------------------------------
    built_from = source_mod.source_watermarks(src)
    built_from["selection"] = {"domains": sorted(domains or []),
                               "family_keys_explicit": family_keys is not None,
                               "max_families": max_families,
                               "families_selected": len(fam_by_key),
                               "publications_selected": len(pub_family)}
    built_from["demand"] = {k: v for k, v in demand_report.items() if k != "missing"}
    #  The counts a shard can RECHECK, read back out of the database rather than accumulated in
    #  this process. `len(pub_family)` is the number of publications SELECTED, and a publication
    #  whose every chunk lacked an embedding contributes none, so recording the selection here
    #  made `verify_serving` fail on every release ever built: hot_v1 recorded 4,136 publications
    #  and held 3,909. What was selected is a property of the build and belongs in `built_from`.
    counts = {**release_store.observed_counts(dst, release_id),
              "chunks_by_kind": kinds, "chunks_from_live": chunk_stats.get("live", 0),
              "chunks_from_stage": chunk_stats.get("stage", 0),
              "chunks_from_demand": len(demand_chunks)}
    if counts["chunks"] != n:
        raise BuildError(f"{release_id}: wrote {n} chunks but the partition holds "
                         f"{counts['chunks']}")
    idx = release_store.index_bytes(dst, release_id)
    manifest = manifest_mod.build(
        release_id=release_id, shard_key=shard_key, version=rel["version"], kind=kind,
        domains=held_domains, counts=counts, built_from=built_from,
        index_params={"dense": index_params, "lexical": lexical.get("analyzer") or
                      {"engine": "none"},
                      "bytes": {**idx, "lexical_bytes": lexical.get("bytes", 0)}},
        artifacts=artifacts, stats=stat_block, timings=timings,
        root=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        note=note)
    release_store.seal(dst, release_id, manifest,
                       snapshot_path=snap.get("dir", ""),
                       snapshot_sha256=snap.get("sha256", ""),
                       snapshot_bytes=snap.get("bytes", 0))
    dst.commit()
    if snap.get("dir"):
        manifest_mod.write(manifest, os.path.join(snap["dir"], "manifest.json"))
    timings["total_s"] = round(time.time() - t_all, 2)
    _log(log, "sealed", release_id=release_id, content_hash=manifest["content_hash"],
         **{"total_s": timings["total_s"]})
    return {"release_id": release_id, "manifest": manifest, "stats": stat_block,
            "counts": counts, "timings": timings, "snapshot": snap, "lexical": lexical,
            "demand": demand_report}


def _json(v):
    import json
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, default=str)


def _iter_lexical_docs(dst, release_id, batch=20_000):
    """The release's chunks, in chunk_id order, for the Tantivy build.

    Key-range batched on the primary key rather than held in memory or streamed through a server
    side cursor: the build can be 25M documents and the writer is already holding a heap.
    """
    last = 0
    while True:
        with dst.cursor() as cur:
            cur.execute("""SELECT chunk_id, publication_id, family_key, kind, lang, text
                           FROM chunks_release WHERE release_id=%s AND chunk_id > %s
                           ORDER BY chunk_id LIMIT %s""", (release_id, last, batch))
            rows = cur.fetchall()
        if not rows:
            return
        for r in rows:
            yield r
        last = int(rows[-1]["chunk_id"])


def _write_domain_rows(dst, release_id, domains):
    with dst.cursor() as cur:
        cur.execute("""SELECT home_domain d, count(*) f, COALESCE(sum(n_publications),0) p,
                              COALESCE(sum(n_chunks),0) c
                       FROM corpus_release_member WHERE release_id=%s GROUP BY 1""", (release_id,))
        by_domain = {r["d"]: r for r in cur.fetchall()}
    rows = []
    ipc = sizing.index_bytes_per_chunk()
    dpc = sizing.disk_bytes_per_chunk()
    for d in sorted(set(domains) | set(by_domain)):
        r = by_domain.get(d)
        c = int(r["c"]) if r else 0
        rows.append({"domain": d, "n_families": int(r["f"]) if r else 0,
                     "n_publications": int(r["p"]) if r else 0, "n_chunks": c,
                     "index_bytes": c * ipc, "disk_bytes": c * dpc})
    release_store.set_shard_rows(dst, release_id, rows)


# ---------------------------------------------------------------------------------------------
# the snapshot
# ---------------------------------------------------------------------------------------------
MEMBER_COLS = ("release_id", "family_key", "home_domain", "secondary_domains",
               "n_publications", "n_chunks", "source")
SHARD_COLS = ("release_id", "domain", "n_families", "n_publications", "n_chunks", "index_bytes",
              "disk_bytes")


def _copy_out(dst, sql, params, path):
    """One release's rows, gzipped, as a snapshot artifact. COPY rather than JSON because a full
    domain shard has hundreds of thousands of member rows and the restore is a COPY back in."""
    with gzip.open(path, "wb") as fh:
        with dst.cursor() as cur:
            with cur.copy(sql, params) as cp:
                for block in cp:
                    fh.write(block)
    return path


def _copy_in(dst, table, cols, path):
    n = 0
    with gzip.open(path, "rb") as fh:
        with dst.cursor() as cur:
            with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    cp.write(block)
            n = cur.rowcount
    return max(0, n)


def write_snapshot(dst, release_id, snapshot_dir, *, lexical_dir=None, pg_dump="pg_dump"):
    """A release on disk: the partition's data, its members, its domain rows, the Tantivy index,
    and a checksum for each.

    THE MEMBER AND DOMAIN ROWS ARE PART OF THE RELEASE, not bookkeeping left behind on the
    builder. A snapshot of the chunk partition alone restores a shard that holds every chunk,
    reports zero families, cannot answer `releases_for_domain` (which is the lookup
    `shard_router.route()` output needs), and fails its own `verify_serving` on `count:families`.
    That is a shard that would join the fan-out while unable to prove what it is serving.

    LOGICAL, NOT PHYSICAL, and that is a real limitation worth naming. `pg_dump` carries rows, not
    index pages, so restoring a release rebuilds its HNSW on the target. For a full-size shard that
    is hours, and the index-preserving alternative is a file-level snapshot of a per-release
    tablespace or of a whole per-shard cluster, which is not portable between clusters. The
    manifest records `restore.rebuilds_dense_index` so nobody discovers it during a cutover.
    """
    out = os.path.join(snapshot_dir, release_id)
    os.makedirs(out, exist_ok=True)
    part = release_store.partition_name(release_id)
    dump_path = os.path.join(out, "chunks.dump")
    env = dict(os.environ)
    info = dst.info
    if info.password:
        env["PGPASSWORD"] = info.password
    cmd = [pg_dump, "-h", info.host or "127.0.0.1", "-p", str(info.port or 5432),
           "-U", info.user, "-d", info.dbname, "--format=custom", "--data-only",
           "--no-owner", "--no-privileges", "-t", f"public.{part}", "-f", dump_path]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise BuildError(f"pg_dump failed for {release_id}: {res.stderr.strip()[:400]}")

    members_path = _copy_out(
        dst, f"COPY (SELECT {', '.join(MEMBER_COLS)} FROM corpus_release_member "
             f"WHERE release_id = %s ORDER BY family_key) TO STDOUT", (release_id,),
        os.path.join(out, "members.copy.gz"))
    domains_path = _copy_out(
        dst, f"COPY (SELECT {', '.join(SHARD_COLS)} FROM corpus_release_shard "
             f"WHERE release_id = %s ORDER BY domain) TO STDOUT", (release_id,),
        os.path.join(out, "domains.copy.gz"))

    artifacts = [manifest_mod.artifact(dump_path, "chunks.dump"),
                 manifest_mod.artifact(members_path, "members.copy.gz"),
                 manifest_mod.artifact(domains_path, "domains.copy.gz")]
    lex_tar = ""
    if lexical_dir and os.path.isdir(lexical_dir):
        lex_tar = os.path.join(out, "lexical.tar.gz")
        with tarfile.open(lex_tar, "w:gz") as tf:
            tf.add(lexical_dir, arcname="lexical")
        artifacts.append(manifest_mod.artifact(lex_tar, "lexical.tar.gz"))

    sums = os.path.join(out, "SHA256SUMS")
    with open(sums, "w", encoding="utf-8") as fh:
        for a in artifacts:
            fh.write(f"{a['sha256']}  {a['name']}\n")
    total = sum(a["bytes"] for a in artifacts)
    return {"dir": out, "artifacts": artifacts, "bytes": total,
            "sha256": manifest_mod.sha256_file(sums), "dump": dump_path, "lexical_tar": lex_tar,
            "restore": {"rebuilds_dense_index": True,
                        "note": "pg_dump carries rows, not index pages; restore_snapshot rebuilds "
                                "the HNSW on the target"}}


def restore_snapshot(dst, release_id, snapshot_dir, *, m=16, ef_construction=64, halfvec=False,
                     pg_restore="pg_restore", rebuild_indexes=True, log=None):
    """Restore a release into a target database and rebuild its dense index.

    This is what a cold shard does when it wakes onto a new release, and it is the only proof that
    a snapshot is a snapshot: a checksum that verifies proves the bytes are intact, not that they
    reconstitute a searchable corpus.
    """
    out = os.path.join(snapshot_dir, release_id)
    manifest = manifest_mod.read(os.path.join(out, "manifest.json"))
    v = manifest_mod.verify(manifest, artifact_root=out)
    if not v.ok:
        raise BuildError(f"snapshot for {release_id} does not verify: {v.failures}")

    #  The release row first: a target that does not know the release exists cannot hold its
    #  partition, its members or its verdict. Sealed on arrival, because what is being restored is
    #  a sealed release and a window in which it is editable on the target is a window in which it
    #  can be edited.
    _ensure_release_row(dst, manifest, snapshot_path=out)
    part = release_store.ensure_partition(dst, release_id)
    #  A restore has to be repeatable. pg_restore --data-only appends, so a second run over a
    #  partition that already has rows is a primary key violation, or worse, a doubled release.
    with dst.cursor() as cur:
        cur.execute(f'TRUNCATE "{part}"')
    dst.commit()
    info = dst.info
    env = dict(os.environ)
    if info.password:
        env["PGPASSWORD"] = info.password
    cmd = [pg_restore, "-h", info.host or "127.0.0.1", "-p", str(info.port or 5432),
           "-U", info.user, "-d", info.dbname, "--data-only", "--no-owner",
           os.path.join(out, "chunks.dump")]
    t0 = time.time()
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise BuildError(f"pg_restore failed for {release_id}: {res.stderr.strip()[:400]}")
    restore_s = round(time.time() - t0, 2)

    lex_tar = os.path.join(out, "lexical.tar.gz")
    lex_dir = ""
    if os.path.exists(lex_tar):
        lex_dir = os.path.join(out, "restored_lexical")
        shutil.rmtree(lex_dir, ignore_errors=True)
        os.makedirs(lex_dir, exist_ok=True)
        with tarfile.open(lex_tar, "r:gz") as tf:
            tf.extractall(lex_dir)
        lex_dir = os.path.join(lex_dir, "lexical")

    #  Members and domain rows travel with the release. Without them the target holds every
    #  chunk, reports zero families and fails count:families in its own verification.
    counts = {"members": 0, "domains": 0}
    with dst.cursor() as cur:
        cur.execute("DELETE FROM corpus_release_member WHERE release_id=%s", (release_id,))
        cur.execute("DELETE FROM corpus_release_shard WHERE release_id=%s", (release_id,))
    for table, cols, name in (("corpus_release_member", MEMBER_COLS, "members.copy.gz"),
                              ("corpus_release_shard", SHARD_COLS, "domains.copy.gz")):
        path = os.path.join(out, name)
        if os.path.exists(path):
            counts["members" if "member" in table else "domains"] = _copy_in(dst, table, cols,
                                                                             path)
    _seal_restored(dst, release_id, manifest)
    dst.commit()

    index_s = 0.0
    if rebuild_indexes:
        t0 = time.time()
        release_store.build_indexes(dst, release_id, m=m, ef_construction=ef_construction,
                                    halfvec=halfvec)
        dst.commit()
        index_s = round(time.time() - t0, 2)
    _log(log, "restored", release_id=release_id, restore_s=restore_s, index_s=index_s, **counts)
    return {"release_id": release_id, "restore_s": restore_s, "dense_index_s": index_s,
            "lexical_dir": lex_dir, "manifest": manifest, "rows": counts}


def _ensure_release_row(dst, manifest, *, snapshot_path=""):
    """Put the release on the target if it is not there. Unsealed for the length of the restore,
    because the immutability triggers refuse to let a sealed release take rows away again if a
    restore has to be repeated."""
    rid = manifest["release_id"]
    with dst.cursor() as cur:
        cur.execute("SELECT sealed_at FROM corpus_release WHERE release_id=%s", (rid,))
        row = cur.fetchone()
        if row is None:
            cur.execute("""INSERT INTO corpus_release (release_id, shard_key, version, kind,
                               state, builder, note, manifest, content_hash, snapshot_path, stats)
                           VALUES (%s,%s,%s,%s,'building',%s,%s,%s,%s,%s,%s)""",
                        (rid, manifest["shard_key"], manifest["version"], manifest["kind"],
                         "restore", manifest.get("note") or "",
                         _json(manifest), manifest["content_hash"], snapshot_path,
                         _json(manifest.get("stats") or {})))
        elif row["sealed_at"] is not None:
            cur.execute("UPDATE corpus_release SET sealed_at=NULL WHERE release_id=%s", (rid,))


def _seal_restored(dst, release_id, manifest):
    with dst.cursor() as cur:
        cur.execute("""UPDATE corpus_release SET sealed_at=now(), state='built',
                           content_hash=%s, manifest=%s
                        WHERE release_id=%s AND sealed_at IS NULL""",
                    (manifest["content_hash"], _json(manifest), release_id))
