"""Enumerate the niche and write the manifest, in appendable batches, from the extraction cache.

    python ops/niche_extract.py                       # once, ~4 minutes of sequential DB reads
    python ops/niche_enumerate.py                     # no database access at all
    python ops/niche_enumerate.py --limit-families 5000 --release-id smoke

THE SHAPE, and why it is this shape:

  1  membership   which publications the CPC boundary names       one pass over classifications
  2  family       every publication of every named family         publications.csv only
  3  citation     the X/Y examiner neighbourhood of those          one pass over citations
  4  attributes   symbols, claim and description characters        one pass over each cached file
  5  emit         one record per family, family_id ascending       streamed into 50k-record parts

Step 2 is where the 20.4% of the corpus that carries no classification gets in: family membership
is decided at family level, so an unclassified sibling of a classified publication is a member even
though no CPC rule could ever name it. Step 3 is where the rest gets in. MEASURED 2026-08-22:
53,540 of the 148,942 examiner-cited documents in this field, 35.9%, carry no CPC at all, so these
two steps are not a refinement of the CPC rule, they are most of the reach.

Nothing here writes the corpus. `--emit db` writes ONLY `corpus_niche_release` and
`corpus_niche_family` from `sql/010_corpus_release.sql`, which this workstream owns.
"""
from __future__ import annotations

import argparse
import collections
import csv
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import corpus_niche  # noqa: E402
from corpus_niche import Boundary, ManifestWriter, build_record  # noqa: E402

CACHE = os.path.join(ROOT, "data", "niche_cache")
MANIFESTS = os.path.join(ROOT, "data", "manifests")
CONFIG = os.path.join(ROOT, "config", "niche_boundary.json")

#  A run holds the corpus's family map and the member publications' attributes in memory. MEASURED
#  peak on the live corpus is about 6 GB; the patents VM has 31 GB and shares it with seven other
#  workstreams, so refuse rather than trigger the silent livelock a full box produces.
MIN_AVAILABLE_GB = float(os.environ.get("NICHE_MIN_AVAILABLE_GB", "9"))


def available_gb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1e6
    except Exception:
        pass
    return float("inf")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ step 1: CPC membership
def _stream_symbol_sets(cache):
    """(publication_id, [symbols]) for every classified publication, one group at a time.

    Reads the SORTED copy. The raw copy comes off the heap in insertion order and an incremental
    ingest appends rows for publications that were already there, which makes a publication's rows
    non-contiguous: MEASURED, 1,475,201 ids are revisited, so streaming the unsorted file would
    both miss symbols and inflate every distinct count taken from it (5.44M "classified
    publications" against the true 3.96M).
    """
    path = os.path.join(cache, "classifications.sorted.tsv.gz")
    cur = None
    syms = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            pid, _scheme, sym = line.rstrip("\n").split("\t")
            if pid != cur:
                if cur is not None:
                    yield cur, syms
                cur, syms = pid, []
            s = corpus_niche.normalise_symbol(sym)
            if s:
                syms.append(s)
    if cur is not None:
        yield cur, syms


def cpc_members(cache, boundary):
    """Publication ids the CPC part of the boundary names. Stores nothing else: the symbols come
    back in a second pass once the niche is known, which costs 60 s and saves about 2 GB."""
    members = set()
    n = 0
    for pid, syms in _stream_symbol_sets(cache):
        n += 1
        if boundary.tier_of_symbols(syms):
            members.add(pid)
    log(f"step 1: {n:,} classified publications, {len(members):,} named by the CPC boundary")
    return members


def load_symbols(cache, keep):
    """Every classification symbol of every niche publication. IPC symbols, Y tagging codes and
    2000-series indexing codes are all kept: they are excluded from the BOUNDARY, not from the
    record."""
    out = {}
    pool = {}
    for pid, syms in _stream_symbol_sets(cache):
        if pid in keep and syms:
            #  ~250,000 distinct symbols across 25M rows. Interning turns 25M string objects into
            #  25M pointers to 250,000, which is the difference between a run that fits on this
            #  box and one that does not.
            out[pid] = [pool.setdefault(s, s) for s in syms]
    return out


# ------------------------------------------------------------------ step 2: the family map
def load_publications(cache):
    """pid -> (publication_number, country, family_key, title), plus family_key -> [pid]."""
    meta = {}
    fam_pids = collections.defaultdict(list)
    num_to_pid = {}
    countries = {}
    famkeys = {}
    with gzip.open(os.path.join(cache, "publications.csv.gz"), "rt", newline="") as fh:
        for row in csv.reader(fh):
            pid, num, country, fam, title = row[0], row[1], row[3], row[7], row[11]
            #  One object per family key and per country code, shared by every publication that
            #  carries it. 5M publications makes both worth doing.
            k = corpus_niche.family_key(fam, num)
            key = famkeys.setdefault(k, k)
            meta[pid] = (num, countries.setdefault(country, country), key, title)
            fam_pids[key].append(pid)
            num_to_pid[num] = pid
    log(f"step 2: {len(meta):,} publications in {len(fam_pids):,} families")
    return meta, fam_pids, num_to_pid


def close_families(pids, meta, fam_pids):
    fams = {meta[p][2] for p in pids if p in meta}
    out = set()
    for f in fams:
        out.update(fam_pids[f])
    return fams, out


# ------------------------------------------------------------------ step 3: citation closure
def citation_closure(cache, boundary, member_nums, num_to_pid):
    """Publication ids one X/Y examiner-citation hop from the boundary, and the numbers that hop
    reaches which this corpus does NOT hold. The second set is the external acquisition list."""
    if not boundary.citation_closure:
        return set(), set()
    reach = set()
    with gzip.open(os.path.join(cache, "citations.csv.gz"), "rt", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 4:
                continue
            src, dst, cat, org = row[0], row[1], row[2], row[3]
            if not boundary.citation_admitted(cat, org):
                continue
            if src in member_nums:
                if dst not in member_nums:
                    reach.add(dst)
            elif dst in member_nums:
                reach.add(src)
    local = {num_to_pid[n] for n in reach if n in num_to_pid}
    external = {n for n in reach if n not in num_to_pid}
    log(f"step 3: {len(reach):,} X/Y examiner neighbours outside the boundary "
        f"({len(local):,} held locally, {len(external):,} not held at all)")
    return local, external


# ------------------------------------------------------------------ step 4: text attributes
def load_agg(path, keep):
    out = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            pid, n, chars = line.rstrip("\n").split("\t")
            if pid in keep:
                out[pid] = (int(n), int(chars or 0))
    return out


def load_abstracts(cache, reps, members):
    """Abstract text for the representative publication of each family, and a non-empty flag for
    every member. Only the representatives' text is retained, which is what keeps this bounded:
    carrying 3.1M abstracts instead of 1.6M would cost about 2.5 GB for nothing."""
    text = {}
    nonempty = set()
    csv.field_size_limit(1 << 24)
    with gzip.open(os.path.join(cache, "pubtext.csv.gz"), "rt", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 3:
                continue
            pid, abstract = row[0], row[2]
            if not (abstract or "").strip() or pid not in members:
                continue
            nonempty.add(pid)
            if pid in reps:
                text[pid] = abstract
    return text, nonempty


def choose_representative(pids, claims, paras, meta, has_abs_row):
    """Deterministic: most complete text first, then abstract, then title, then lowest number.

    Chosen BEFORE the abstract text is read, from columns that are already in memory, so the
    expensive pass only has to keep one publication's abstract per family.
    """
    def rank(p):
        cn, cc = claims.get(p, (0, 0))
        pn, pc = paras.get(p, (0, 0))
        c, d = corpus_niche.text_state(cc, pc)
        num, _country, _fam, title = meta[p]
        return (int(c and d), int(d), int(c), int(p in has_abs_row),
                1 if (title or "").strip() else 0, corpus_niche._neg(num))
    return max(pids, key=rank)


# ------------------------------------------------------------------ driver
def emit_db(release_dir, release_id, boundary, summary, batch=5000):
    """Mirror a finished release into `corpus_niche_release` / `corpus_niche_family` / _external.

    Those three tables are workstream B's own, defined in sql/010_corpus_release.sql, and this
    writes nothing else. It refuses if the migration has not been applied, because creating tables
    on the live corpus database is workstream H's decision, taken once, deliberately.
    """
    import db  # imported here so a file-only run needs no database configuration at all

    with db.cursor(autocommit=True) as cur:
        cur.execute("SELECT to_regclass('corpus_niche_family') AS t")
        if not (cur.fetchone() or {}).get("t"):
            raise SystemExit("corpus_niche_family does not exist: sql/010_corpus_release.sql has "
                             "not been applied. Applying it is workstream H's call, not this "
                             "script's. The manifest files are already written.")
        cur.execute(
            "INSERT INTO corpus_niche_release (release_id, boundary_sha256, state, families, "
            "publications, summary) VALUES (%s, %s, 'complete', %s, %s, %s) "
            "ON CONFLICT (release_id) DO UPDATE SET state = EXCLUDED.state, "
            "families = EXCLUDED.families, publications = EXCLUDED.publications, "
            "summary = EXCLUDED.summary, updated_at = now()",
            (release_id, boundary.sha256(), summary["niche_families"],
             summary["niche_publications"], json.dumps(summary)))
        rows = []
        n = 0
        for _part, rec in corpus_niche.read_manifest(release_dir):
            rows.append((release_id, rec["family_id"], rec["publications"], rec["cpc"],
                         rec["title"], rec["abstract"], rec["has_claims"], rec["has_description"],
                         rec["has_complete_text"], rec["best_source"], rec["missing_fields"]))
            if len(rows) >= batch:
                n += _flush_rows(cur, rows)
                rows = []
        n += _flush_rows(cur, rows)
        ext = os.path.join(release_dir, "external_only.txt")
        if os.path.exists(ext):
            pubs = [p for p in open(ext).read().split() if p]
            for i in range(0, len(pubs), batch):
                cur.executemany(
                    "INSERT INTO corpus_niche_external (release_id, publication_number) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    [(release_id, p) for p in pubs[i:i + batch]])
        log(f"emit db: {n:,} families written to corpus_niche_family")


def _flush_rows(cur, rows):
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO corpus_niche_family (release_id, family_id, publications, cpc, title, "
        "abstract, has_claims, has_description, has_complete_text, best_source, missing_fields) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (release_id, family_id) DO NOTHING", rows)
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--out", default=MANIFESTS)
    ap.add_argument("--release-id", default=None)
    ap.add_argument("--batch-size", type=int, default=50000)
    ap.add_argument("--limit-families", type=int, default=0,
                    help="stop after this many families; for a smoke run")
    ap.add_argument("--skip-mem-check", action="store_true")
    ap.add_argument("--emit", choices=["files", "db"], default="files",
                    help="'db' additionally mirrors the finished release into the tables in "
                         "sql/010_corpus_release.sql. Files are always written.")
    args = ap.parse_args(argv)

    avail = available_gb()
    if avail < MIN_AVAILABLE_GB and not args.skip_mem_check:
        raise SystemExit(f"refusing to run: {avail:.1f} GB available, need {MIN_AVAILABLE_GB} GB. "
                         f"This host livelocks silently when it runs out of memory.")

    boundary = Boundary.load(args.config)
    release = args.release_id or f"{boundary.release_prefix}-{time.strftime('%Y-%m-%d')}"
    t0 = time.time()

    members = cpc_members(args.cache, boundary)
    meta, fam_pids, num_to_pid = load_publications(args.cache)
    fams, pubs = close_families(members, meta, fam_pids)
    log(f"step 2: family closure -> {len(fams):,} families, {len(pubs):,} publications")

    member_nums = {meta[p][0] for p in pubs}
    local_reach, external_reach = citation_closure(args.cache, boundary, member_nums, num_to_pid)
    cfams, cpubs = close_families(local_reach, meta, fam_pids)
    all_fams = fams | cfams
    all_pubs = pubs | cpubs
    log(f"niche: {len(all_fams):,} families, {len(all_pubs):,} publications "
        f"({100.0 * len(all_pubs) / max(len(meta), 1):.1f}% of the corpus)")

    symbols = load_symbols(args.cache, all_pubs)
    log(f"step 4: symbols loaded for {len(symbols):,} of {len(all_pubs):,} niche publications")
    claims = load_agg(os.path.join(args.cache, "claims_agg.tsv.gz"), all_pubs)
    paras = load_agg(os.path.join(args.cache, "para_agg.tsv.gz"), all_pubs)
    log(f"step 4: {len(claims):,} members hold claims, {len(paras):,} hold description paragraphs")

    has_abs_row = set()
    with gzip.open(os.path.join(args.cache, "publications.csv.gz"), "rt", newline="") as fh:
        for row in csv.reader(fh):
            if row[0] in all_pubs and row[10] == "t":
                has_abs_row.add(row[0])

    ordered = sorted(all_fams)
    if args.limit_families:
        ordered = ordered[:args.limit_families]
    reps = {}
    for f in ordered:
        pids = [p for p in fam_pids[f] if p in all_pubs]
        if pids:
            reps[f] = choose_representative(pids, claims, paras, meta, has_abs_row)
    abstracts, abs_nonempty = load_abstracts(args.cache, set(reps.values()), all_pubs)
    log(f"step 4: abstracts held for {len(abstracts):,} representatives; "
        f"{len(abs_nonempty):,} niche publications carry a non-empty abstract")

    outdir = os.path.join(args.out, release)
    writer = ManifestWriter(outdir, release, boundary, batch_size=args.batch_size)
    stats = collections.Counter()
    for f in ordered:
        pids = [p for p in fam_pids[f] if p in all_pubs]
        if not pids:
            continue
        rep = reps[f]
        syms = set()
        for p in pids:
            syms.update(symbols.get(p) or ())
        rows = []
        for p in pids:
            num, country, _fam, title = meta[p]
            cn, cc = claims.get(p, (0, 0))
            pn, pc = paras.get(p, (0, 0))
            rows.append({
                "publication_number": num, "country": country, "title": title or "",
                "abstract": abstracts.get(p, "") if p == rep else "",
                "has_abstract": p in abs_nonempty,
                "claims_chars": cc, "desc_chars": pc,
            })
        rec = build_record(f, rows, syms, rep_number=meta[rep][0])
        writer.add(rec)
        stats["families"] += 1
        stats["publications"] += len(pids)
        stats["complete_text"] += int(rec["has_complete_text"])
        stats["has_claims"] += int(rec["has_claims"])
        stats["has_description"] += int(rec["has_description"])
        if rec["best_source"]:
            stats["src:" + rec["best_source"]] += 1
    writer.close()

    with open(os.path.join(args.out, "LATEST"), "w") as fh:
        fh.write(release + "\n")
    summary = {
        "release_id": release,
        "boundary_sha256": boundary.sha256(),
        "wall_seconds": round(time.time() - t0, 1),
        "corpus_publications": len(meta),
        "corpus_families": len(fam_pids),
        "cpc_member_publications": len(members),
        "families_from_cpc": len(fams),
        "publications_after_family_closure": len(pubs),
        "citation_reach_local": len(local_reach),
        "citation_reach_external_only": len(external_reach),
        "niche_families": len(all_fams),
        "niche_publications": len(all_pubs),
        "stats": dict(stats),
    }
    with open(os.path.join(outdir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    with open(os.path.join(outdir, "external_only.txt"), "w") as fh:
        fh.write("\n".join(sorted(external_reach)) + "\n")
    log(json.dumps(summary, indent=1))
    if args.emit == "db":
        emit_db(outdir, release, boundary, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
