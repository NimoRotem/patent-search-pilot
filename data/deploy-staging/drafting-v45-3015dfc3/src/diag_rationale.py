"""Is the 26.3% rationale-unfaithfulness a REAL regression, or a judge/generator text mismatch?

The generator (webapp._rationale) is shown: title + abstract + the single BEST-MATCHING passage
(which, after the OPS backfill, is often a newly-added deep paragraph).
The judge (audit.ref_text) is shown: title + abstract + claim 1, and only falls back to body
chunks when abstract AND claim1 are both missing -- which the OPS backfill just made much rarer,
because it populated `claims`.

So deepening can mechanically DESYNC the two: the model grounds a statement in a paragraph the
judge is never shown, and the judge correctly reports "not in the provided text".

Test: for every flagged card, check whether the rationale's grounded evidence quotes appear in
(a) the judge's snippet, and (b) the full document text. If they are absent from (a) but present
in (b), the verdict is a harness artifact, not a model hallucination.
"""
import json, re, sys
sys.path.insert(0, 'src')
import db, audit, webview, enrich_display, webapp

WORD = re.compile(r"[a-z0-9]+")


def words(s):
    return set(WORD.findall((s or "").lower()))


def overlap(quote, hay):
    q = words(quote)
    if not q:
        return 0.0
    return len(q & words(hay)) / len(q)


def full_doc_text(pub):
    with db.cursor() as cur:
        cur.execute("SELECT id FROM publications WHERE publication_number=%s", (pub,))
        r = cur.fetchone()
        if not r:
            return ""
        cur.execute("SELECT text FROM chunks WHERE publication_id=%s AND text IS NOT NULL", (r["id"],))
        return " ".join(x["text"] for x in cur.fetchall())


d = json.load(open('data/reports/_audit_rationale_POST_OPS.json'))
flagged = [r for r in d["rows"] if r["verdict"] in ("overclaims", "hallucinates")]

artifact = real = 0
print(f"{len(flagged)} flagged cards\n")
for r in flagged:
    pub, slug = r["pub"], r["slug"]
    judge_snip = audit.ref_text(pub)["snippet"]          # exactly what the judge saw
    doc = full_doc_text(pub)
    # the grounded evidence the generator kept
    cache = json.load(open(f"data/rationale/{slug}__{pub}.json"))
    quotes = []
    for it in cache.get("reads_on", []):
        if isinstance(it, dict) and it.get("evidence"):
            quotes.append(it["evidence"])
    if not quotes:
        quotes = [r["why"]]
    in_judge = max((overlap(q, judge_snip) for q in quotes), default=0)
    in_doc = max((overlap(q, doc) for q in quotes), default=0)
    verdict = "ARTIFACT (grounded in text judge never saw)" if (in_doc >= 0.8 and in_judge < 0.6) \
        else "REAL (not grounded in the document either)"
    if verdict.startswith("ARTIFACT"):
        artifact += 1
    else:
        real += 1
    print(f"  {pub:20s} {r['verdict']:12s} evidence_in_judge_snippet={in_judge:.2f} "
          f"evidence_in_full_doc={in_doc:.2f}  -> {verdict}")

print(f"\nartifact={artifact}  real={real}  of {len(flagged)} flagged")
n = d["n"]
print(f"headline unfaithfulness {len(flagged)}/{n} = {len(flagged)/n:.1%}")
print(f"artifact-corrected      {real}/{n} = {real/n:.1%}")
