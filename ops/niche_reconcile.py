#!/usr/bin/env python
"""Reconcile the `patentdata` niche discovery result against workstream B's manifest and
workstream C's fetch pool, and feed what is genuinely new into C's seeding path.

Why this exists. The `patentdata` session shipped `artifacts/niche_corpus_status.{json,csv}`
reporting 16,896 publications in 6,215 families. **Neither artifact contains a single publication
number.** Both are flattened aggregate reports: `counts_by_cpc`, `counts_by_authority`,
`counts_by_language`, completeness percentages. So the result cannot be reconciled by reading it,
only by replaying the discovery that produced it.

Replaying it is cheap and safe. `corpus.niche.discover.DiscoveryEngine` needs a source and a
manifest sink, and `corpus.niche.manifest.MemoryManifest` is a sink that keeps the records in
process. No staging database, no schema, no provider call, no write of any kind: every source
query runs under `SET TRANSACTION READ ONLY` with a 15 s statement timeout and is bounded by a
primary-key window. That is a genuine strength of their design and this tool depends on it.

    discover    replay the bounded discovery, write one JSON object per publication
    reconcile   classify each of those against B's manifest release and C's pool
    seed-file   write the JSONL that `ops/fulltext_acquire.py seed --manifest` consumes

Nothing here writes to the live database. `seed-file` writes a file; seeding the pool is the
separate, explicit `fulltext_acquire.py seed` command, exactly as C intended.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import config  # noqa: E402,F401  (loads .env)

#  Their discovery run, reproduced exactly: `--batch-size 1000 --max-batches 1` from watermark 0.
#  docs/niche-corpus.md records the watermark it left behind, publication id 1,197.
DEFAULT_BATCH_SIZE = 1000
DEFAULT_BATCHES = 1


# ------------------------------------------------------------------------------------------------
# discover: replay their bounded audit, read only
# ------------------------------------------------------------------------------------------------
def replay_discovery(batch_size=DEFAULT_BATCH_SIZE, batches=DEFAULT_BATCHES, dsn=""):
    """Return (records, watermarks). Read only against the live corpus."""
    from corpus.niche.database import connection_factory
    from corpus.niche.discover import DiscoveryEngine
    from corpus.niche.manifest import MemoryManifest
    from corpus.niche.providers.local import LocalDiscoverySource

    factory = connection_factory(dsn or config.PG_DSN, application_name="niche-reconcile-read")
    source = LocalDiscoverySource(factory)
    sink = MemoryManifest()
    previous = None
    for index in range(max(1, int(batches))):
        summary = DiscoveryEngine(source, sink, batch_size=batch_size).run()
        current = summary.watermarks.get("publication_id")
        if current == previous:
            break
        previous = current
        if index + 1 < max(1, int(batches)):
            time.sleep(1.0)
    return list(sink.rows.values()), dict(sink.watermarks)


def niche_terms() -> tuple:
    from corpus.niche.domains import DOMAIN_GROUPS
    return tuple(term for group in DOMAIN_GROUPS for term in group.terms)


def record_row(record, terms) -> dict:
    from corpus_niche import family_key
    text = f"{record.title} {record.abstract}".lower()
    return {
        "publication_number": record.publication_number,
        "family_id": record.family_id,
        #  The DOCDB sentinel, honoured. `corpus_niche.family_key` maps '-1', '0' and '' onto the
        #  publication number, which is what workstream I proved on 21,862 live rows. Their
        #  discovery does not do this and the difference is the single largest number in the
        #  reconciliation, so it is recorded per record rather than argued about.
        "family_key": family_key(record.family_id, record.publication_number),
        "authority": record.authority,
        "priority": record.priority,
        "discovery_signals": list(record.discovery_signals),
        "cpc": list(record.cpc_codes),
        "ipc": list(record.ipc_codes),
        #  Their `_publication_rows` labels a record 'terminology' whenever NEITHER a CPC nor an
        #  IPC prefix matched. That is a fallback label, not a match, so the term test is run
        #  here against the text and recorded separately.
        "term_hit": any(term in text for term in terms),
        "title": record.title[:200],
        "has_complete_claims": bool(record.has_complete_claims),
        "has_complete_description": bool(record.has_complete_description),
        "in_corpus": bool(record.publication_date or record.title or record.cpc_codes
                          or record.ipc_codes or record.has_claims or record.has_description),
    }


def cmd_discover(args) -> int:
    t0 = time.time()
    records, watermarks = replay_discovery(args.batch_size, args.batches, args.dsn)
    families = {r.family_id or f"publication:{r.publication_number}" for r in records}
    with open(args.out, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record_row(record), sort_keys=True) + "\n")
    print(json.dumps({"publications": len(records), "families": len(families),
                      "watermarks": watermarks, "seconds": round(time.time() - t0, 1),
                      "out": args.out}, indent=2, sort_keys=True))
    return 0


# ------------------------------------------------------------------------------------------------
# reconcile
# ------------------------------------------------------------------------------------------------
def canonical(pub: str) -> str:
    from sources.schema import canonical_pub
    return canonical_pub(pub)


def load_discovered(path: str) -> dict:
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out[canonical(row["publication_number"])] = row
    return out


def scan_manifest(release_dir: str, wanted: set):
    """Stream every part named in index.json. Return (held, family_of).

    `held` is the subset of `wanted` that appears as a publication of some family record, which is
    B's statement that this corpus holds it inside the niche boundary. `family_of` maps those back
    to B's family key so family-level overlap can be counted without a second pass.
    """
    index = json.load(open(os.path.join(release_dir, "index.json"), encoding="utf-8"))
    held, family_of, lines = set(), {}, 0
    for part in index["parts"]:
        with open(os.path.join(release_dir, part["name"]), encoding="utf-8") as fh:
            for line in fh:
                lines += 1
                #  Cheap prefilter: json.loads on 3M records costs minutes, and the publication
                #  list is a small, unambiguous slice of the line.
                start = line.find('"publications":[')
                if start < 0:
                    continue
                end = line.find("]", start)
                for raw in line[start + len('"publications":['):end].split(","):
                    pub = canonical(raw.strip().strip('"'))
                    if pub and pub in wanted:
                        held.add(pub)
                        record = json.loads(line)
                        family_of[pub] = record.get("family_id", "")
    return held, family_of, lines, index


def load_external_only(release_dir: str) -> set:
    path = os.path.join(release_dir, "external_only.txt")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {canonical(line.strip()) for line in fh if line.strip()}


def pool_state(publications) -> dict:
    """What C's pool already knows about these publications."""
    import db
    out = {}
    pubs = list(publications)
    with db.cursor(autocommit=True, readonly=True) as cur:
        for i in range(0, len(pubs), 5000):
            cur.execute("SELECT publication_number, state, manifest FROM fulltext_fetch_task "
                        "WHERE publication_number = ANY(%s)", (pubs[i:i + 5000],))
            for row in cur.fetchall() or []:
                out[row["publication_number"]] = {"state": row["state"],
                                                 "manifest": row["manifest"]}
    return out


def text_held(publications) -> dict:
    """Independent measurement of what text this corpus holds for each publication.

    Deliberately not read off the discovery record: the headline number is "how many of the new
    ones we hold no text for", so it is measured again, from the tables, by a different query.
    """
    import db
    out = {}
    pubs = list(publications)
    with db.cursor(autocommit=True, readonly=True) as cur:
        for i in range(0, len(pubs), 2000):
            batch = pubs[i:i + 2000]
            variants = _variants(batch)
            cur.execute(
                """SELECT p.publication_number,
                          EXISTS (SELECT 1 FROM claims c WHERE c.publication_id = p.id) AS claims,
                          EXISTS (SELECT 1 FROM paragraphs g
                                   WHERE g.publication_id = p.id) AS paragraphs,
                          EXISTS (SELECT 1 FROM publications q
                                    JOIN claims c2 ON c2.publication_id = q.id
                                   WHERE q.simple_family_id = p.simple_family_id
                                     AND p.simple_family_id IS NOT NULL
                                     AND p.simple_family_id NOT IN ('', '-1', '0')
                                     AND q.id <> p.id) AS sibling_text
                     FROM publications p
                    WHERE p.publication_number = ANY(%s)""",
                (variants,))
            for row in cur.fetchall() or []:
                pub = canonical(row["publication_number"])
                prior = out.get(pub, {})
                out[pub] = {
                    "in_corpus": True,
                    "claims": bool(row["claims"]) or bool(prior.get("claims")),
                    "paragraphs": bool(row["paragraphs"]) or bool(prior.get("paragraphs")),
                    "sibling_text": bool(row["sibling_text"]) or bool(prior.get("sibling_text")),
                }
            cur.execute(
                "SELECT publication_number, claims_z IS NOT NULL AS c, "
                "description_z IS NOT NULL AS d FROM sources_docstore "
                "WHERE publication_number = ANY(%s)", (batch,))
            for row in cur.fetchall() or []:
                pub = canonical(row["publication_number"])
                entry = out.setdefault(pub, {"in_corpus": False, "claims": False,
                                             "paragraphs": False, "sibling_text": False})
                entry["fetched_claims"] = bool(row["c"])
                entry["fetched_description"] = bool(row["d"])
    return out


def _variants(publications):
    from corpus.niche.identifiers import source_publication_variants
    out = []
    for pub in publications:
        out.extend(source_publication_variants(pub))
    return list(dict.fromkeys(out))


def has_no_text(entry) -> bool:
    if not entry:
        return True
    return not (entry.get("claims") or entry.get("paragraphs")
                or entry.get("fetched_claims") or entry.get("fetched_description"))


def cmd_reconcile(args) -> int:
    t0 = time.time()
    discovered = load_discovered(args.discovered)
    wanted = set(discovered)
    held, family_of, lines, index = scan_manifest(args.release, wanted)
    external = load_external_only(args.release) & wanted
    new = sorted(wanted - held - external)
    pool = pool_state(wanted)
    text = text_held(new)

    their_families = {row["family_id"] or f"publication:{pub}"
                      for pub, row in discovered.items()}
    held_families = {discovered[pub]["family_id"] or f"publication:{pub}" for pub in held}
    new_families = {discovered[pub]["family_id"] or f"publication:{pub}" for pub in new}
    no_text = [pub for pub in new if has_no_text(text.get(pub))]
    citation_only = [pub for pub in new if not (text.get(pub) or {}).get("in_corpus")]

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "their_discovery": {
            "publications": len(discovered),
            "families": len(their_families),
            "source": args.discovered,
        },
        "our_manifest": {
            "release_id": index.get("release_id"),
            "families": index.get("totals", {}).get("families"),
            "records_scanned": lines,
            "external_only": len(load_external_only(args.release)),
        },
        "overlap": {
            "publications_b_already_holds": len(held),
            "publications_in_b_external_only": len(external),
            "publications_genuinely_new": len(new),
            "families_b_already_holds": len(held_families),
            "families_genuinely_new": len(new_families),
        },
        "new_publications": {
            "total": len(new),
            "no_text_held": len(no_text),
            "not_in_corpus_at_all": len(citation_only),
            "already_in_c_pool": sum(1 for pub in new if pub in pool),
        },
        "c_pool": {
            "their_publications_already_pooled": len(pool),
            "pool_states": _tally(entry["state"] for entry in pool.values()),
        },
        "seconds": round(time.time() - t0, 1),
    }
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    if args.out_new:
        with open(args.out_new, "w", encoding="utf-8") as fh:
            for pub in new:
                row = dict(discovered[pub])
                row["text"] = text.get(pub, {})
                fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _tally(values) -> dict:
    out = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


# ------------------------------------------------------------------------------------------------
# seed-file
# ------------------------------------------------------------------------------------------------
#  C's own two priority bands, imported rather than restated so they cannot drift apart. Their
#  discovery priority (1 strongest to 4 weakest) is a tiebreak INSIDE the band, never a way to
#  jump it: a publication reached through a citation is not more urgent than the 37,248 rows the
#  pool is already working through, it is the same kind of work from a wider boundary.
def seed_rows(new_path: str):
    from acquire.manifest import PRIORITY_HAS_SIBLING_TEXT, PRIORITY_NO_SIBLING_TEXT
    rows = []
    with open(new_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            text = row.get("text") or {}
            if not has_no_text(text):
                continue          # we already hold its text: nothing to acquire
            base = (PRIORITY_HAS_SIBLING_TEXT if text.get("sibling_text")
                    else PRIORITY_NO_SIBLING_TEXT)
            rows.append({
                "publication_number": row["publication_number"],
                "family_id": row.get("family_id") or "",
                "country": row.get("authority") or row["publication_number"][:2],
                "priority": base + max(1, min(4, int(row.get("priority") or 4))),
            })
    rows.sort(key=lambda r: (r["priority"], r["publication_number"]))
    return rows


def cmd_seed_file(args) -> int:
    rows = seed_rows(args.new)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps({"entries": len(rows), "out": args.out,
                      "priorities": _tally(str(r["priority"]) for r in rows)},
                     indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="replay the patentdata bounded discovery, read only")
    d.add_argument("--out", default="data/niche_reconcile/discovered.jsonl")
    d.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    d.add_argument("--batches", type=int, default=DEFAULT_BATCHES)
    d.add_argument("--dsn", default="")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("reconcile", help="classify their publications against B and C")
    r.add_argument("--discovered", default="data/niche_reconcile/discovered.jsonl")
    r.add_argument("--release", required=True, help="a niche manifest release directory")
    r.add_argument("--report", default="data/niche_reconcile/report.json")
    r.add_argument("--out-new", default="data/niche_reconcile/new.jsonl")
    r.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("seed-file", help="write the JSONL that fulltext_acquire.py seed consumes")
    s.add_argument("--new", default="data/niche_reconcile/new.jsonl")
    s.add_argument("--out", default="data/manifests/patentdata-gap/part-00000.jsonl")
    s.set_defaults(func=cmd_seed_file)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
