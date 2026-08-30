"""Score a finished report against the references a patent attorney actually filed.

    python eval/attorney_recall.py <slug> [<slug> ...]

Every other metric here scores retrieval against a citation list scraped from a register. This one
scores the WHOLE FUNNEL against an expert's finished work product, and it reports where each miss
happened rather than a single number, because the five stages fail for five unrelated reasons and
a scalar hides which one moved:

    in corpus      -> the retrieval universe contains it at all
    retrieved      -> the ranked list contains its family
    screened       -> the screen was shown it
    read in full   -> its full text was charted against the checklist
    on the page    -> a human would have seen it

`on the page` is the only one that is the product. The others exist to say what to fix.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
import db                                                          # noqa: E402

GOLD = os.path.join(HERE, "attorney_gold.json")
REPORTS = os.path.join(os.path.dirname(HERE), "data", "reports")


def load_gold(path=GOLD):
    with open(path) as fh:
        return json.load(fh)


def resolve(cur, gold):
    """{name: {pub, fam, n_claims, n_paras}} as the corpus holds it TODAY.

    Resolved live rather than trusted from the file: acquisition writes new publications into the
    corpus, so "not in corpus" is a fact with a date on it and re-checking is the point.
    """
    out = {}
    for c in gold["citations"]:
        like = c["pub"].rsplit("-", 1)[0] + "-%"
        cur.execute(
            """SELECT p.publication_number pub,
                      COALESCE(NULLIF(p.simple_family_id,''), p.publication_number) fam,
                      (SELECT count(*) FROM claims x WHERE x.publication_id=p.id) nc,
                      (SELECT count(*) FROM chunks h WHERE h.publication_id=p.id
                         AND h.kind='paragraph') np
               FROM publications p WHERE p.publication_number LIKE %s LIMIT 1""", (like,))
        r = cur.fetchone()
        out[c["name"]] = ({"pub": r["pub"], "fam": r["fam"], "nc": r["nc"], "np": r["np"]}
                          if r else None)
    return out


def score(slug, gold, held):
    rep = json.load(open(os.path.join(REPORTS, f"{slug}.json")))
    dr = rep.get("deep_rank") or {}
    #  RETRIEVED means the ranked list contains its family; SCREENED means the screen was shown
    #  it. Two different depths (7,618 vs 2,500 on the baseline run) and two different fixes, so
    #  they are counted separately rather than conflated into one "found it" number.
    ranked_fams = set(rep.get("ranked_families") or [])
    cand_fams = set(dr.get("candidate_families") or [])
    by_pub = dr.get("by_pub") or {}
    screen_scores = dr.get("screen_scores") or {}
    order = {p: i + 1 for i, p in enumerate(dr.get("order") or [])}
    #  FAMILY LEVEL, because that is the unit the report operates on: retrieval collapses to
    #  families and any member may stand in. Scoring by publication number alone reports a family
    #  that WAS read as unread whenever a sibling was the one charted.
    fam_members = {}
    for p, v in by_pub.items():
        fam_members.setdefault(v.get("family") or p, []).append(p)
    try:
        view = json.load(open(os.path.join(REPORTS, f"{slug}.view.json")))
        cards = view.get("cards") or []
    except Exception:
        cards = []
    card_pos = {}
    for i, c in enumerate(cards):
        for k in (c.get("pub"), c.get("family")):
            if k and k not in card_pos:
                card_pos[k] = i + 1
    #  IN LEDGER: the reference is cited as kept evidence for at least one limitation. This is the
    #  deliverable-level stage the 60-card page cut was hiding: a reference read cell-perfectly
    #  and ranked 103 was invisible to the old funnel, while the claim ledger carried it the whole
    #  time. Every ledger row is user-reachable (jumpRef falls back to the document panel).
    ledger_pubs = {}
    for lim in ((rep.get("ledger") or {}).get("limitations") or []):
        for e in (lim.get("evidence") or []):
            p = e.get("pub")
            if p:
                ledger_pubs.setdefault(p, []).append(lim.get("id") or "")

    rows, tally = [], {"in_corpus": 0, "retrieved": 0, "screened": 0, "read": 0,
                       "ledger": 0, "page": 0}
    for c in gold["citations"]:
        if c.get("findable_by_search") is False:
            rows.append((c["name"], c.get("pub") or "—", "not reachable by search (NPL)", None))
            continue
        h = held.get(c["name"])
        if not h:
            rows.append((c["name"], "—", "not in corpus", None))
            continue
        tally["in_corpus"] += 1
        fam, pub = h["fam"], h["pub"]
        retrieved = fam in ranked_fams
        screened_here = fam in cand_fams
        kin = [pub] + [m for m in fam_members.get(fam, []) if m != pub]
        entry = next((by_pub[m] for m in kin if m in by_pub), {})
        stand_in = next((m for m in kin if m in by_pub and m != pub), None)
        read = bool(entry.get("read_in_full"))
        page = card_pos.get(pub) or card_pos.get(fam) or next(
            (card_pos[m] for m in kin if m in card_pos), None)
        #  The screen score lives in screen_scores; by_pub holds only what was CHARTED, so a
        #  candidate that was screened and then not read reads as "never screened" without this.
        sc = next((screen_scores[m] for m in kin if m in screen_scores), None)
        if sc is None:
            sc = entry.get("screen")
        if retrieved:
            tally["retrieved"] += 1
        if screened_here or sc is not None:
            tally["screened"] += 1
        if read:
            tally["read"] += 1
        in_ledger = [m for m in kin if m in ledger_pubs]
        if in_ledger:
            tally["ledger"] += 1
        if page:
            tally["page"] += 1
        via = f" (via {stand_in})" if stand_in else ""
        rank = next((order[m] for m in kin if m in order), None)
        if page:
            where = f"ON THE PAGE (card {page}){via}"
        elif in_ledger:
            n_lims = len(ledger_pubs.get(in_ledger[0]) or [])
            where = (f"IN THE LEDGER (evidence for {n_lims} limitation(s)), "
                     f"ranked {rank} — below the card cut{via}")
        elif read:
            where = f"read, ranked {rank} — off the page{via}"
        elif retrieved and h["nc"] == 0 and h["np"] == 0:
            where = f"retrieved, no text here, screened {sc}"
        elif retrieved:
            where = f"retrieved, screened {sc}, not read"
        else:
            where = "in corpus, never retrieved"
        rows.append((c["name"], pub, where, page))
    return rep, rows, tally


def main(argv):
    #  --gold <path> so a second subject does not need a second copy of this file. There are now
    #  two frozen expert sets and they are different inventions: attorney_gold.json is a Schmalz
    #  grip unit, nguyen_gold.json a portable vacuum gripper.
    argv = list(argv)
    path = GOLD
    if "--gold" in argv:
        i = argv.index("--gold")
        path = argv[i + 1]
        del argv[i:i + 2]
    gold = load_gold(path)
    slugs = argv[1:] or [(gold.get("baseline_2026_08_15") or {}).get("run")]
    slugs = [s for s in slugs if s]
    with db.cursor() as cur:
        held = resolve(cur, gold)
    base = gold.get("baseline_2026_08_15") or {}
    for slug in slugs:
        try:
            rep, rows, tally = score(slug, gold, held)
        except FileNotFoundError:
            print(f"{slug}: no such report")
            continue
        n = sum(1 for c in gold["citations"] if c.get("findable_by_search") is not False)
        print(f"\n=== {slug} — {gold['subject']['title']} ===")
        for name, pub, where, _ in rows:
            print(f"  {name:14} {str(pub):18} {where}")
        print(f"  ---- {tally['page']}/{n} on the page · {tally['ledger']}/{n} in the ledger · "
              f"{tally['read']}/{n} read · "
              f"{tally['screened']}/{n} screened · {tally['retrieved']}/{n} retrieved · "
              f"{tally['in_corpus']}/{n} in corpus")
        if base:
            print(f"  baseline 2026-08-15 (before the fixes): {base['on_the_page']}/{n} on the "
                  f"page · {base['read_in_full']}/{n} read · {base['retrieved']}/{n} retrieved · "
                  f"{base['in_corpus']}/{n} in corpus")
        #  Documents nothing in a patent corpus could ever return are counted apart. Scoring an
        #  office action from a file wrapper as a retrieval miss measures the wrong thing.
        unsearchable = [c["name"] for c in gold["citations"]
                        if c.get("findable_by_search") is False]
        if unsearchable:
            print(f"  not reachable by any patent-corpus search, excluded from the ratio above: "
                  f"{', '.join(unsearchable)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
