"""THE canonical benchmark gold set. One definition, one denominator, every exclusion inspectable.

WHY THIS EXISTS
---------------
Two different denominators were in use and neither was written down:

    83   citations listed across the six subjects
    69   cited FAMILIES the corpus happens to hold

69 was the number being reported. It silently drops the 14 citations whose documents the corpus
does not hold, which are guaranteed misses, so reporting against it flatters the system by
excluding its worst cases. Reaching those documents through the external APIs is a real product
requirement, so they belong in the denominator with `in_corpus=false` recorded against them.

A second inconsistency this surfaces: the gold is not built the same way for every subject.

    suction_chuck, suction_unit, suction_display, robot_gripper
        X/Y-coded search-report citations read from the corpus `citations` table. Relevance code
        known per citation.
    ep3707092
        27 citations transcribed by hand from the EP search report and opposition. The subject IS
        in the corpus but carries ZERO X/Y-coded citation rows, so no code is available.
    schmalz
        10 citations from a third-party preissuance submission. The subject is not in the corpus
        at all, so there is nothing to read a code from.

"X/Y only" therefore cannot be applied uniformly. This records the code where one exists and the
PROVENANCE where one does not, so any analysis can filter to the coded subset and say so, instead
of the rule being silently different per subject.

ELIGIBILITY, applied in this order, first match wins:

    UNRESOLVED           the citation string does not parse as a publication number
    SUBJECT_FAMILY       the citation is a member of the subject's own family
    PUBLISHED_AFTER_EFD  published on or after the subject's effective filing date: not prior art
    NOT_X_OR_Y           a relevance code exists and it is neither X nor Y
    DUPLICATE_FAMILY     an earlier citation already resolved to this family
    (eligible)

`in_corpus` is REPORTED, never an eligibility rule.

    python eval/benchmark_gold.py                 # writes eval/benchmark_gold.csv
    python eval/benchmark_gold.py --check         # verify the file matches a fresh build
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import db  # noqa: E402
import pubnorm  # noqa: E402
import citation_recall as CR  # noqa: E402

SUBJECTS = os.path.join(HERE, "benchmark_subjects.json")
OUT = os.path.join(HERE, "benchmark_gold.csv")
BENCHMARK_VERSION = "2026-08-05.1"

FIELDS = [
    "benchmark_version", "subject_id", "subject_pub", "subject_family_id", "subject_efd",
    "subject_in_corpus", "gold_source",
    "citation_raw", "cited_pub_resolved", "gold_family_id", "citation_code",
    "cited_publication_date", "in_corpus", "eligible", "exclusion_reason", "source_record_id",
]


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def subject_row(cur, url):
    """Canonical subject identity from its URL. -> (pub, family, efd, in_corpus)."""
    m = re.search(r"/patent/([A-Za-z0-9]+)", url or "")
    raw = m.group(1) if m else ""
    keys = [norm(v) for v in (pubnorm.variants(raw) or [raw])]
    cur.execute(
        """SELECT publication_number pn,
                  COALESCE(NULLIF(simple_family_id,''), publication_number) fam,
                  COALESCE(earliest_priority_date, filing_date, publication_date) efd
           FROM publications
           WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) = ANY(%s)
           LIMIT 1""", (keys,))
    r = cur.fetchone()
    if r:
        return r["pn"], r["fam"], r["efd"], True
    return (pubnorm.canonical(raw) or raw), "", None, False


def codes_for(cur, subject_pub):
    """{normalised cited pub -> relevance code} from the corpus citation table, or {}."""
    if not subject_pub:
        return {}
    cur.execute("""SELECT dst_pub, origin, category FROM citations WHERE src_pub = %s""",
                (subject_pub,))
    out = {}
    for r in cur.fetchall():
        code = (r["origin"] or "").strip()
        if not code or code.lower() == "unknown":
            code = f"cat:{(r['category'] or '').strip()}" or ""
        out[norm(r["dst_pub"])] = code
    return out


#  Subjects whose citation list was ALREADY filtered to X/Y at source. For those the per-citation
#  code is not stored (BigQuery's citation.type was applied in the WHERE clause, not carried
#  through), and treating them as uncoded would report "0 coded" beside a gold_source claiming an
#  X/Y filter -- true but unreadable.
XY_FILTERED_AT_SOURCE = {"bigquery_search_report_xy"}


def is_xy(code, gold_source=""):
    """True when the citation is known X/Y, False when known and neither, None when unknown.

    None is deliberately distinct from False: an ABSENT relevance code must never be mistaken for
    a negative one, or the X/Y rule silently excludes every subject that has no codes.
    """
    if gold_source in XY_FILTERED_AT_SOURCE:
        return True
    c = (code or "").upper()
    if not c or c.startswith("CAT:"):
        return None            # no relevance code available: cannot apply the rule
    return ("X" in c) or ("Y" in c)


def build():
    d = json.load(open(SUBJECTS))
    con = db.connect()
    con.autocommit = True
    cur = con.cursor()
    rows = []
    for sub in d["subjects"]:
        spub, sfam, sefd, s_in = subject_row(cur, sub.get("url"))
        codes = codes_for(cur, spub) if s_in else {}
        #  The label must reflect whether an X/Y RELEVANCE CODE is actually available, not merely
        #  whether the corpus has citation rows. ep3707092 has rows but every one of them is
        #  uncoded, and calling that "corpus_citations_xy" would state that an X/Y filter had been
        #  applied to it when none can be.
        has_xy = any(is_xy(c) is not None for c in codes.values())
        gold_source = sub.get("gold_source") or (
            "corpus_citations_xy" if has_xy else
            "corpus_citations_uncoded" if codes else "transcribed_from_document")
        resolved = CR.resolve(cur, sub["citations"])
        seen_fams = set()
        for raw in sub["citations"]:
            r = resolved.get(raw)
            code = codes.get(norm(raw), "")
            if not code and r:
                code = codes.get(norm(r["pub"]), "")
            pub = r["pub"] if r else ""
            fam = r["fam"] if r else ""
            pdate = ""
            if r:
                cur.execute("SELECT publication_date pd FROM publications WHERE id=%s", (r["id"],))
                x = cur.fetchone()
                pdate = str(x["pd"])[:10] if x and x["pd"] else ""

            reason = ""
            if not r and not pubnorm.parse(raw):
                reason = "UNRESOLVED"
            elif fam and sfam and fam == sfam:
                reason = "SUBJECT_FAMILY"
            elif pdate and sefd and pdate >= str(sefd)[:10]:
                reason = "PUBLISHED_AFTER_EFD"
            elif is_xy(code, gold_source) is False:
                reason = "NOT_X_OR_Y"
            elif fam and fam in seen_fams:
                reason = "DUPLICATE_FAMILY"
            if fam and not reason:
                seen_fams.add(fam)

            #  A citation the corpus does not hold still gets a stable family key, so it stays in
            #  the denominator and can be tracked when an external source later supplies it.
            gold_fam = fam or f"ext:{pubnorm.canonical(raw) or norm(raw)}"
            rows.append({
                "benchmark_version": BENCHMARK_VERSION,
                "subject_id": sub["id"], "subject_pub": spub,
                "subject_family_id": sfam, "subject_efd": str(sefd)[:10] if sefd else "",
                "subject_in_corpus": str(bool(s_in)).lower(),
                "gold_source": gold_source,
                "citation_raw": raw,
                "cited_pub_resolved": pub,
                "gold_family_id": gold_fam,
                "citation_code": code,
                "cited_publication_date": pdate,
                "in_corpus": str(bool(r)).lower(),
                "eligible": str(not reason).lower(),
                "exclusion_reason": reason,
                "source_record_id": f"{sub['id']}::{norm(raw)}",
            })
    rows.sort(key=lambda x: (x["subject_id"], x["citation_raw"]))
    return rows


def write(rows, path=OUT):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]


def report(rows):
    n = len(rows)
    elig = [r for r in rows if r["eligible"] == "true"]
    fams = {(r["subject_id"], r["gold_family_id"]) for r in elig}
    print(f"benchmark_version {BENCHMARK_VERSION}")
    print(f"  citations listed            {n}")
    print(f"  ELIGIBLE (the denominator)  {len(elig)}  in {len(fams)} distinct subject-families")
    print(f"    of which in corpus        {sum(1 for r in elig if r['in_corpus'] == 'true')}")
    print(f"    of which NOT in corpus    {sum(1 for r in elig if r['in_corpus'] == 'false')}"
          f"   <- guaranteed misses unless an external source supplies them")
    ex = {}
    for r in rows:
        if r["exclusion_reason"]:
            ex[r["exclusion_reason"]] = ex.get(r["exclusion_reason"], 0) + 1
    print(f"  excluded                    {sum(ex.values())}  {ex or '{}'}")
    print(f"\n{'subject':16s} {'listed':>7s} {'eligible':>9s} {'in-corpus':>10s} "
          f"{'coded':>6s}  gold_source")
    for sid in sorted({r["subject_id"] for r in rows}):
        rs = [r for r in rows if r["subject_id"] == sid]
        e = [r for r in rs if r["eligible"] == "true"]
        coded = sum(1 for r in rs
                    if is_xy(r["citation_code"], r["gold_source"]) is not None)
        print(f"{sid:16s} {len(rs):>7d} {len(e):>9d} "
              f"{sum(1 for r in e if r['in_corpus'] == 'true'):>10d} {coded:>6d}  "
              f"{rs[0]['gold_source']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="rebuild and compare against the committed file")
    args = ap.parse_args()
    rows = build()
    if args.check:
        tmp = OUT + ".check"
        h_new = write(rows, tmp)
        h_old = hashlib.sha256(open(OUT, "rb").read()).hexdigest()[:16]
        os.unlink(tmp)
        print(f"rebuilt {h_new}   committed {h_old}   "
              f"{'MATCH' if h_new == h_old else 'DIFFERS'}")
        raise SystemExit(0 if h_new == h_old else 1)
    h = write(rows)
    report(rows)
    print(f"\nwritten {OUT}  sha256:{h}")


if __name__ == "__main__":
    main()
