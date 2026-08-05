"""Freeze each benchmark subject's disclosure list, once, with fixed evaluation weights.

WHY, and it invalidates every coverage number reported before this
------------------------------------------------------------------
The primary metric is weighted disclosure coverage. For that to compare two runs, both the
DISCLOSURE LIST and the WEIGHTS have to be properties of the subject, fixed before the run. Two
things broke that:

  1. WEIGHTS WERE DERIVED FROM THE CANDIDATE SET. deep_rank.rarity() computes each disclosure's
     weight as log(N/df) over the references the search happened to chart. Change retrieval and
     the weights change, so the metric moves even when the output does not. A retrieval change
     that finds MORE art covering a disclosure makes that disclosure cheaper, and coverage can
     fall while the report improves.
  2. THE LIST WAS GENERATED AT SEARCH TIME. disclosures.extract() is an LLM call made during the
     run, so two runs of the same subject were scored against two different denominators.

This module generates each benchmark subject's list ONCE and writes it to disk with a version and
a content hash. Runs in benchmark mode load it instead of generating; the metric reads it rather
than the report.

RANKING may still use candidate-derived rarity: knowing that a disclosure is unusual among the art
this search found is a genuine ranking signal and it is measured, not declared. What it may not do
is decide the score card. The two are now separate:

    deep_rank.rarity()      candidate-derived, for RANKING
    disclosure["weight"]    fixed by kind, for EVALUATION

    python eval/freeze_disclosures.py                    # freeze any subject not yet frozen
    python eval/freeze_disclosures.py --subject schmalz --force
    python eval/freeze_disclosures.py --verify           # hashes still match what is on disk
"""
from __future__ import annotations

import argparse
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
import disclosures  # noqa: E402
import enrich_display  # noqa: E402
import pubnorm  # noqa: E402

FROZEN_DIR = os.path.join(HERE, "disclosures_frozen")
DISCLOSURE_LIST_VERSION = "2026-08-05.1"


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def subject_pub(url):
    m = re.search(r"/patent/([A-Za-z0-9]+)", url or "")
    return pubnorm.canonical(m.group(1)) if m else ""


def description_for(pub):
    """Description text: corpus paragraphs first, then the display record.

    A subject the corpus holds claims-only yields no potential claims, which silently changes the
    SHAPE of its disclosure list. That is recorded per subject rather than hidden.
    """
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT ch.text FROM chunks ch JOIN publications p
                             ON p.id = ch.publication_id
                           WHERE p.publication_number = %s AND ch.kind = 'paragraph'
                             AND ch.text IS NOT NULL ORDER BY ch.id LIMIT 150""", (pub,))
            got = "\n".join(r["text"] for r in cur.fetchall())
        if len(got) > 400:
            return got, "corpus_paragraphs"
    except Exception:
        pass
    d = enrich_display.enrich_for_display(pub) or {}
    desc = d.get("description") or ""
    if isinstance(desc, list):
        desc = "\n".join(str(x) for x in desc)
    return (desc, "display_record") if len(desc) > 400 else ("", "none")


def claims_for(pub):
    """Claims: the corpus first, then the display record.

    Measured: enrich_display returned ZERO claims for EP-2386771-A3 while the corpus holds 12.
    Freezing from the weaker source produced a disclosure list with no claim limitations at all,
    87 potential claims and nothing to measure novelty against -- and, because no claims had been
    supplied, validate() saw a structurally consistent list and passed it.
    """
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT c.claim_no, c.text FROM claims c
                           JOIN publications p ON p.id = c.publication_id
                           WHERE p.publication_number = %s AND c.text IS NOT NULL
                           ORDER BY c.claim_no""", (pub,))
            got = [{"claim_no": r["claim_no"], "text": r["text"]} for r in cur.fetchall()]
        if got:
            return got, "corpus_claims"
    except Exception:
        pass
    d = enrich_display.enrich_for_display(pub) or {}
    cl = d.get("claims") or []
    return (cl, "display_record") if cl else ([], "none")


def path_for(subject_id):
    return os.path.join(FROZEN_DIR, f"{subject_id}.json")


def digest(items):
    payload = json.dumps([{"text": i["text"], "kind": i["kind"], "weight": i["weight"]}
                          for i in items], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def freeze(sub, force=False):
    out = path_for(sub["id"])
    if os.path.exists(out) and not force:
        return json.load(open(out)), False
    pub = subject_pub(sub["url"])
    d = enrich_display.enrich_for_display(pub) or {}
    claims, claims_source = claims_for(pub)
    desc, desc_source = description_for(pub)
    #  FAIL CLOSED. A frozen list is the metric's denominator for every future run, so freezing a
    #  structurally broken one poisons everything downstream and still produces a number. strict
    #  raises rather than returning a list with known problems.
    problems = []
    try:
        items = disclosures.extract(claims=claims, description=desc,
                                    title=d.get("title") or "", retries=2, strict=True)
    except ValueError as e:
        items, problems = [], str(e).split("; ")
    if not claims:
        #  A patent always HAS claims. Getting none means the source failed, not that the subject
        #  discloses nothing claimable, and a list built without them cannot measure novelty.
        problems = problems + ["NO_CLAIMS_AVAILABLE: every source returned zero claims for a "
                               "published patent; this is a source failure"]
    rec = {
        "disclosure_list_version": DISCLOSURE_LIST_VERSION,
        "subject_id": sub["id"],
        "subject_pub": pub,
        "n_claims_source": len(claims),
        "claims_source": claims_source,
        "description_chars": len(desc),
        "description_source": desc_source,
        "summary": disclosures.summary(items),
        "problems": problems,
        "usable": not problems and bool(items),
        #  The EVALUATION weight. Fixed by kind, never by how much art the search found.
        "weights_are": "fixed_by_kind",
        "kind_weights": dict(disclosures.KIND_WEIGHT),
        "disclosures": items,
        "content_hash": digest(items),
    }
    os.makedirs(FROZEN_DIR, exist_ok=True)
    with open(out, "w") as fh:
        json.dump(rec, fh, indent=1)
    return rec, True


def usable(rec) -> bool:
    """A frozen list may be used as a metric denominator only if it is structurally sound."""
    return bool(rec) and bool(rec.get("usable")) and bool(rec.get("disclosures"))


def load(subject_id):
    """The frozen list, or None. Callers must NOT silently regenerate in benchmark mode."""
    p = path_for(subject_id)
    if not os.path.exists(p):
        return None
    return json.load(open(p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    subs = json.load(open(os.path.join(HERE, "benchmark_subjects.json")))["subjects"]
    if args.subject:
        subs = [s for s in subs if s["id"] == args.subject]

    if args.verify:
        bad = 0
        for s in subs:
            rec = load(s["id"])
            if not rec:
                print(f"{s['id']:16s} NOT FROZEN")
                bad += 1
                continue
            h = digest(rec["disclosures"])
            ok = h == rec.get("content_hash")
            print(f"{s['id']:16s} {rec['summary']['n']:>3d} disclosures  "
                  f"{'ok' if ok else 'HASH MISMATCH'}  {rec['content_hash']}")
            bad += 0 if ok else 1
        raise SystemExit(1 if bad else 0)

    print(f"{'subject':16s} {'n':>4s} {'ind':>4s} {'comb':>5s} {'dep':>4s} {'pot':>4s} "
          f"{'desc':>7s} {'usable':>7s}  problems")
    bad = 0
    for s in subs:
        rec, created = freeze(s, force=args.force)
        by = rec["summary"]["by_kind"]
        ok = usable(rec)
        bad += 0 if ok else 1
        print(f"{s['id']:16s} {rec['summary']['n']:>4d} "
              f"{by.get('independent_limitation', 0):>4d} {by.get('combination', 0):>5d} "
              f"{by.get('dependent_limitation', 0):>4d} {by.get('potential_claim', 0):>4d} "
              f"{rec['description_chars']:>7,} {('yes' if ok else 'NO'):>7s}  "
              f"{'; '.join(rec.get('problems') or [])[:80]}")
    if bad:
        print(f"\n{bad} subject(s) have NO usable disclosure list. They cannot be scored on "
              f"coverage until fixed; excluding them silently would be the same defect as the "
              f"69-vs-83 denominator.")
    print(f"\ndisclosure_list_version {DISCLOSURE_LIST_VERSION}, weights fixed by kind: "
          f"{disclosures.KIND_WEIGHT}")


if __name__ == "__main__":
    main()
