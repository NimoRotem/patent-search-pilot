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

WHY THE ADMISSION REASON IS RE-DERIVED HERE AND NOT READ OFF THEIR RECORD. Their
`LocalDiscoverySource._publication_rows` labels a record `terminology` in an `else` branch, taken
whenever NEITHER a CPC prefix NOR an IPC prefix matched. It never looks at the title or the
XX "unclassified as far as our prefixes go", not "a
niche term appears in this document", and 7,444 of the 16,896 carry it. Worse,
`family_members`, `citations` and `co_classified` all call `_publication_rows(include_all=True)`,
which skips `in_niche()` entirely, and `in_niche()` itself returns True for any record carrying a
graph signal. The union of those two facts is that a graph-reached publication is admitted with no
evidence test of any kind. That is a defensible discovery choice, it is how they reach the 35.9%
of examiner-cited art that carries no CPC, but it means the label cannot be used as evidence and
the evidence has to be measured. `classify_gap()` below does the measuring: the term test is run
against the real text, their prefix set and B's boundary are each evaluated separately, and a
publication with none of the three is called `graph_only` rather than `terminology`.
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


def their_prefixes():
    """Their CPC and IPC prefix sets, normalised the way their own matcher normalises."""
    from corpus.niche.domains import all_cpc_prefixes, all_ipc_prefixes
    return all_cpc_prefixes(), all_ipc_prefixes()


def _prefix_hit(codes, prefixes) -> bool:
    return any(str(code).replace(" ", "").upper().startswith(prefix)
               for code in codes for prefix in prefixes)


def boundary():
    """Workstream B's checked-in boundary predicate. Loaded once, from the same JSON B ships."""
    from corpus_niche import Boundary
    return Boundary.load(os.path.join(ROOT, "config", "niche_boundary.json"))


def record_row(record, terms, cpc_prefixes, ipc_prefixes, bound) -> dict:
    from corpus_niche import family_key
    text = f"{record.title} {record.abstract}".lower()
    cpc = list(record.cpc_codes)
    ipc = list(record.ipc_codes)
    return {
        "publication_number": record.publication_number,
        "family_id": record.family_id,
        #  The DOCDB sentinel, honoured. `corpus_niche.family_key` maps '-1', '0' and '' onto the
        #  publication number, which is what workstream I proved on 21,862 live rows. Their
        #  discovery does not do this and the difference is recorded per record rather than
        #  argued about.
        "family_key": family_key(record.family_id, record.publication_number),
        "authority": record.authority,
        "priority": record.priority,
        "discovery_signals": list(record.discovery_signals),
        "cpc": cpc,
        "ipc": ipc,
        #  The three evidence tests, each run here and each independent of their label.
        #  `term_hit` is the test their 'terminology' signal does not perform.
        "term_hit": any(term in text for term in terms),
        "their_class_hit": _prefix_hit(cpc, cpc_prefixes) or _prefix_hit(ipc, ipc_prefixes),
        #  B's boundary is a tier, not a boolean: 'core' is one of the six subclasses the corpus
        #  already holds completely, 'adjacent' is one of the 22 measured main groups.
        "b_tier": bound.tier_of_symbols(cpc + ipc) or "",
        "classified": bool(cpc or ipc),
        "title": record.title[:200],
        "has_complete_claims": bool(record.has_complete_claims),
        "has_complete_description": bool(record.has_complete_description),
        "in_corpus": bool(record.publication_date or record.title or record.cpc_codes
                          or record.ipc_codes or record.has_claims or record.has_description),
    }


#  ------------------------------------------------------------------------------------------
#  The real classification. One publication, one reason, evaluated in strength order so the
#  tally partitions rather than overlapping. This is the answer to "is their boundary wider
#  than ours, or is their discovery just looser?", and the two are not the same finding.
#
#  b_boundary      B's own rule admits it. B not holding it is a defect in B's enumeration,
#                  not a boundary difference, and it is the only reason worth escalating.
#  cpc_outside_b   a real classification hit under THEIR prefixes, none under B's. A genuine
#                  boundary difference: their set carries F04B, F04C, B01D46, G05B19 and G01L,
#                  and B measured F04B at density 0.0059 and rejected it.
#  terminology     no classification either side reaches it, but a niche term really is in the
#                  title or abstract. B's boundary has no terminology arm at all, so this is
#                  reach B structurally cannot have.
#  unclassified    no CPC and no IPC of any kind. This is the population the citation closure
#                  exists for: 35.9% of examiner-cited art in this field is here.
#  graph_only      classified, but outside both boundaries and with no term. Admitted purely
#                  because `_publication_rows(include_all=True)` skips the niche test on the
#                  family, citation and co-classification expansions. No evidence at all.
#  ------------------------------------------------------------------------------------------
GAP_REASONS = ("b_boundary", "cpc_outside_b", "terminology", "unclassified", "graph_only")

#  Ordered strongest first: a seed band offset, so the evidence a publication actually carries
#  decides where in C's queue it lands. Never used to jump C's own two bands, only to order
#  inside one.
GAP_RANK = {reason: index for index, reason in enumerate(GAP_REASONS)}


def classify_gap(row) -> str:
    if row.get("b_tier"):
        return "b_boundary"
    if row.get("their_class_hit"):
        return "cpc_outside_b"
    if row.get("term_hit"):
        return "terminology"
    if not row.get("classified"):
        return "unclassified"
    return "graph_only"


def cmd_discover(args) -> int:
    t0 = time.time()
    records, watermarks = replay_discovery(args.batch_size, args.batches, args.dsn)
    families = {r.family_id or f"publication:{r.publication_number}" for r in records}
    terms = niche_terms()
    cpc_prefixes, ipc_prefixes = their_prefixes()
    bound = boundary()
    reasons = {}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for record in records:
            row = record_row(record, terms, cpc_prefixes, ipc_prefixes, bound)
            row["gap_reason"] = classify_gap(row)
            reasons[row["gap_reason"]] = reasons.get(row["gap_reason"], 0) + 1
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps({"publications": len(records), "families": len(families),
                      "gap_reasons": dict(sorted(reasons.items())),
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


def scan_manifest(release_dir: str, wanted: set, wanted_families: set):
    """Stream every part named in index.json. One pass, two questions.

    `held` is the subset of `wanted` that appears as a publication of some family record, which is
    B's statement that this corpus holds it inside the niche boundary. `families_present` is the
    subset of `wanted_families` that B has a record for at all, which separates two very different
    misses: a family B never reached, and a family B reached but whose member list does not carry
    this publication.
    """
    index = json.load(open(os.path.join(release_dir, "index.json"), encoding="utf-8"))
    held, family_of, families_present, lines = set(), {}, set(), 0
    #  `corpus_niche.ManifestWriter` writes `json.dumps(rec, separators=(",", ":"))`, so these
    #  two keys appear with no whitespace. That is the contract this prefilter depends on, and a
    #  manifest written any other way would make every lookup miss and report the whole discovery
    #  as genuinely new. `matched` turns that silent zero into an error.
    pub_key = '"publications":['
    fam_key = '"family_id":"'
    matched = 0
    for part in index["parts"]:
        with open(os.path.join(release_dir, part["name"]), encoding="utf-8") as fh:
            for line in fh:
                lines += 1
                #  Cheap prefilter: json.loads on 1.6M records costs minutes, and both the family
                #  id and the publication list are small, unambiguous slices of the line.
                fam_start = line.find(fam_key)
                family_id = ""
                if fam_start >= 0:
                    fam_start += len(fam_key)
                    family_id = line[fam_start:line.find('"', fam_start)]
                    if family_id in wanted_families:
                        families_present.add(family_id)
                start = line.find(pub_key)
                if start < 0:
                    continue
                end = line.find("]", start)
                matched += 1
                for raw in line[start + len(pub_key):end].split(","):
                    pub = canonical(raw.strip().strip('"'))
                    if pub and pub in wanted:
                        held.add(pub)
                        family_of[pub] = family_id
    if lines and not matched:
        raise ValueError(
            f"{release_dir}: {lines} manifest records and not one carried {pub_key!r}. "
            "The release is not in the shape corpus_niche.ManifestWriter writes, and reporting "
            "an overlap of zero from it would be wrong rather than empty.")
    return held, family_of, families_present, lines, index


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
    #  Family counting uses `family_key`, not the raw DOCDB id, so the 21,862 publications
    #  carrying the '-1' sentinel are counted as the separate families they are instead of
    #  collapsing into one. Workstream I proved this on the live corpus; their discovery does
    #  not do it, so their own 6,215 is the raw-id count and this one is not.
    their_family_ids = {row.get("family_id") or "" for row in discovered.values()}
    their_family_ids.discard("")
    held, family_of, families_present, lines, index = scan_manifest(
        args.release, wanted, their_family_ids)
    external = load_external_only(args.release) & wanted
    new = sorted(wanted - held - external)
    pool = pool_state(wanted)
    text = text_held(new)

    def fkey(pub):
        return discovered[pub].get("family_key") or f"publication:{pub}"

    their_families = {fkey(pub) for pub in discovered}
    held_families = {fkey(pub) for pub in held}
    new_families = {fkey(pub) for pub in new}
    no_text = [pub for pub in new if has_no_text(text.get(pub))]
    citation_only = [pub for pub in new if not (text.get(pub) or {}).get("in_corpus")]

    #  The real classification, over every one of the new publications and not a sample.
    reasons = _tally(discovered[pub].get("gap_reason") or classify_gap(discovered[pub])
                     for pub in new)
    reasons_no_text = _tally(discovered[pub].get("gap_reason") or classify_gap(discovered[pub])
                             for pub in no_text)
    #  A publication B misses whose FAMILY B already has is a different defect from a family B
    #  never reached: the first is an enumeration hole inside B's own boundary, the second is a
    #  boundary difference. They are counted apart because the fixes are different.
    in_present_family = sum(1 for pub in new
                            if (discovered[pub].get("family_id") or "") in families_present)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "their_discovery": {
            "publications": len(discovered),
            "families_raw_docdb_id": len(their_family_ids),
            "families_by_family_key": len(their_families),
            "source": args.discovered,
        },
        "our_manifest": {
            "release_id": index.get("release_id"),
            "records_scanned": lines,
            "external_only": len(load_external_only(args.release)),
        },
        "overlap": {
            "publications_b_already_holds": len(held),
            "publications_in_b_external_only": len(external),
            "publications_genuinely_new": len(new),
            "families_b_already_holds": len(held_families),
            "families_genuinely_new": len(new_families),
            "their_family_ids_b_has_a_record_for": len(families_present),
            "new_publications_whose_family_b_already_has": in_present_family,
        },
        "new_publications": {
            "total": len(new),
            "no_text_held": len(no_text),
            "not_in_corpus_at_all": len(citation_only),
            "already_in_c_pool": sum(1 for pub in new if pub in pool),
            "gap_reason": reasons,
            "gap_reason_of_the_no_text": reasons_no_text,
        },
        "c_pool": {
            "their_publications_already_pooled": len(pool),
            "pool_states": _tally(entry["state"] for entry in pool.values()),
        },
        "seconds": round(time.time() - t0, 1),
    }
    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    if args.out_new:
        with open(args.out_new, "w", encoding="utf-8") as fh:
            for pub in new:
                row = dict(discovered[pub])
                row.setdefault("gap_reason", classify_gap(row))
                row["text"] = text.get(pub, {})
                row["family_in_b_manifest"] = (row.get("family_id") or "") in families_present
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
#  C's own two priority bands, imported rather than restated so they cannot drift apart. The
#  band is C's decision and this file never crosses it: a publication with no texted family
#  sibling is the one worth money, and it stays in front of every publication that has one, no
#  matter what evidence either carries.
#
#  INSIDE a band the order is the evidence measured by `classify_gap`, strongest first, then
#  their own discovery priority (1 strongest to 4 weakest) as the tiebreak. The whole offset is
#  at most 4*10 + 4 = 44, and the two bands are 100 apart, so the ordering can never lift a
#  `graph_only` publication above a `b_boundary` one in the other band. That matters because
#  2,864 of the 4,615 have no evidence beyond having been reached: they are worth fetching, they
#  are not worth fetching first.
GAP_BAND_STEP = 10


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
            reason = row.get("gap_reason") or classify_gap(row)
            rows.append({
                "publication_number": row["publication_number"],
                "family_id": row.get("family_id") or "",
                "country": row.get("authority") or row["publication_number"][:2],
                "priority": (base + GAP_BAND_STEP * GAP_RANK.get(reason, len(GAP_REASONS))
                             + max(1, min(4, int(row.get("priority") or 4)))),
                "gap_reason": reason,
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
                      "gap_reasons": _tally(r["gap_reason"] for r in rows),
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
