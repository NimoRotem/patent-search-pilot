"""Retrieval-only A/B over two channel sets, on the standing benchmark subjects.

WHY THIS AND NOT `eval/benchmark.py`. The full benchmark generates a report, which is hours of LLM
work, and it measures the whole funnel. A change to WHICH CHANNELS RUN is answerable much earlier
and much more cheaply: run the retrieval cascade twice on the same subject, same query text, same
seeds, and compare the fused family list against the subject's own citation list. If a channel
does not move the candidate pool it cannot move anything downstream, and if it does, the number
here is the ceiling on what the rest of the pipeline could deliver from it.

    PYTHONPATH=src .venv/bin/python eval/channel_ab.py --subject ep3707092 \
        --a dense,brief_dense,cpc,citation,qbe \
        --b dense,brief_dense,exact,cpc,citation,qbe

Both arms run in the SAME process against the SAME corpus, alternating, so a change in the
database's cache state or in what else is running on the box lands on both. It reads only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import db                                                              # noqa: E402
from retrieval import Retriever                                        # noqa: E402
from search_modes import Mode, Subject                                 # noqa: E402

SUBJECTS = os.path.join(HERE, "benchmark_subjects.json")


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def subject_rows(number):
    """(Subject, publication_id, family_key) for a publication number in any spelling."""
    with db.cursor() as c:
        c.execute("SELECT id, publication_number, publication_date, filing_date, "
                  "       earliest_priority_date, "
                  "       COALESCE(NULLIF(simple_family_id,''), publication_number) fam "
                  "FROM publications "
                  "WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) = %s "
                  "LIMIT 1", (_norm(number),))
        r = c.fetchone()
    if not r:
        raise SystemExit(f"{number} is not in this corpus")
    efd = r["earliest_priority_date"] or r["filing_date"] or r["publication_date"]
    s = Subject(number=r["publication_number"], efd=efd, filing_date=r["filing_date"],
                publication_date=r["publication_date"],
                jurisdiction=(r["publication_number"] or "")[:2])
    return s, r["id"], r["fam"]


def query_text(pid, limit=6000):
    """The subject's own text, as the corpus holds it. The same input for both arms."""
    with db.cursor() as c:
        c.execute("SELECT kind, text FROM chunks WHERE publication_id=%s "
                  "AND kind IN ('abstract','claim_own','whole') "
                  "ORDER BY CASE kind WHEN 'abstract' THEN 0 WHEN 'claim_own' THEN 1 ELSE 2 END, "
                  "id LIMIT 12", (pid,))
        parts = [r["text"] for r in c.fetchall() if r["text"]]
    return ("\n".join(parts))[:limit]


def gold_families(numbers):
    keys = [_norm(n) for n in numbers if n]
    with db.cursor() as c:
        c.execute("SELECT COALESCE(NULLIF(simple_family_id,''), publication_number) fam "
                  "FROM publications "
                  "WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) "
                  "      = ANY(%s)", (keys,))
        return {r["fam"] for r in c.fetchall()}


def phrases_for(text, n=4):
    """Exact phrases, the way the agent produces them. Falls back to the longest noun-ish
    trigrams of the subject's own text when no model is reachable, so the arm is never skipped
    for a reason that has nothing to do with retrieval."""
    try:
        import llm
        out = llm.chat_json(
            'You expand a patent search. Return JSON {"phrases": [...]} with 3-5 exact multiword '
            'phrases (2-4 words each) that a prior-art document in this field would literally '
            'contain. Technical noun phrases only.', text[:3000]) or {}
        got = [p for p in (out.get("phrases") or []) if isinstance(p, str) and len(p.split()) > 1]
        if got:
            return got[:n]
    except Exception as e:                                             # noqa: BLE001
        print(f"  (no model for phrases: {type(e).__name__}: {e})")
    words = re.findall(r"[A-Za-z]{4,}", text.lower())
    seen, out = set(), []
    for i in range(len(words) - 2):
        p = " ".join(words[i:i + 3])
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    out.sort(key=len, reverse=True)
    return out[:n]


def one_arm(R, label, channels, query, subject, mode, phrases, wide, depths):
    t0 = time.monotonic()
    res = R.search(query, subject=subject, mode=mode, config=list(channels), phrases=phrases,
                   wide=wide, do_rerank=False, topk=5000)
    dt = time.monotonic() - t0
    fams = [fk for fk, *_ in res.family_ranked]
    row = {"arm": label, "channels": list(channels), "seconds": round(dt, 2),
           "families": len(fams),
           "per_channel": {k: len(v) for k, v in res.channel_hits.items()},
           "ranks": {}, "recall": {}}
    return res, row, fams


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default="ep3707092")
    ap.add_argument("--a", required=True, help="comma-separated channel list, arm A")
    ap.add_argument("--b", required=True, help="comma-separated channel list, arm B")
    ap.add_argument("--phrases", default="",
                    help="pipe-separated exact phrases. Freeze them when comparing two runs: the "
                         "model does not return the same list twice and two arms with different "
                         "phrases are not two arms.")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--wide", action="store_true")
    ap.add_argument("--depths", default="100,500,2500")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    depths = [int(x) for x in args.depths.split(",") if x.strip()]
    spec = next((s for s in json.load(open(SUBJECTS))["subjects"] if s["id"] == args.subject),
                None)
    if spec is None:
        raise SystemExit(f"unknown subject {args.subject}")

    number = re.search(r"patent/([A-Z0-9]+)", spec["url"]).group(1)
    subject, pid, own_fam = subject_rows(number)
    mode = Mode(spec.get("mode") or "novelty")
    query = query_text(pid)
    gold = gold_families(spec["citations"]) - {own_fam}
    phrases = ([p.strip() for p in args.phrases.split("|") if p.strip()]
               if args.phrases else phrases_for(query))

    print(f"subject   {subject.number}  efd={subject.efd}  mode={mode.value}")
    print(f"query     {len(query)} chars from the corpus's own chunks")
    print(f"phrases   {phrases}")
    print(f"gold      {len(gold)} of {len(spec['citations'])} cited documents are in this corpus")

    R = Retriever()
    arms = [("A", [c.strip() for c in args.a.split(",") if c.strip()]),
            ("B", [c.strip() for c in args.b.split(",") if c.strip()])]
    rows = []
    for i in range(args.repeats):
        for label, chans in arms:                       # alternating, so cache state lands on both
            _res, row, fams = one_arm(R, label, chans, query, subject, mode, phrases,
                                      args.wide, depths)
            row["repeat"] = i
            for k in depths:
                row["recall"][k] = round(len(set(fams[:k]) & gold) / len(gold), 4) if gold else None
            row["ranks"] = {f: fams.index(f) + 1 for f in gold if f in fams}
            rows.append(row)
            print(f"[{label}#{i}] {row['seconds']:>7.2f}s  {row['families']:>5} families  "
                  f"gold " + "  ".join(f"@{k}={row['recall'][k]}" for k in depths))
            print(f"        channels {row['per_channel']}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"subject": args.subject, "gold": sorted(gold), "rows": rows}, f, indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
