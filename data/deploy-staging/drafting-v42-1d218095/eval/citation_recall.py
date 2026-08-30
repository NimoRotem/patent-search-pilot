"""Citation recall: where does a known citation list land in a finished report?

Generic. Takes a report slug and a list of publication numbers in any spelling, and reports, per
cited FAMILY, the furthest stage it reached:

    not-in-corpus  ->  never-retrieved  ->  screened  ->  charted  ->  displayed

Family level on purpose: a citation is satisfied by any member of its DOCDB simple family, because
the report shows one card per family. Nothing here is specific to one subject patent.
"""
import json
import os
import re
import sys

#  RELATIVE TO THIS FILE, never the deployed checkout. Hardcoding
#  `/home/nimrod_rotem/patent-search-pilot/src` here put the DEPLOYED tree at the front of
#  `sys.path` for the whole process, and pytest imports this module during collection: from that
#  moment every module not already cached was imported from production instead of the worktree
#  under test. It was invisible until a signature changed on one side, and then it read as a
#  mysterious order-dependent failure. A test run must only ever import the tree it is testing.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))
import db          # noqa: E402
import pubnorm     # noqa: E402

REPORTS = os.environ.get("PATENTS_REPORTS_DIR") or os.path.join(_ROOT, "data", "reports")


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _like_patterns(pub):
    m = re.match(r"^([A-Z]{2})([A-Z]?[0-9]+?)([A-Z][0-9]?)?$", norm(pub))
    if not m:
        return []
    cc, num = m.group(1), m.group(2)
    pats = {f"{cc}-{num}-%", f"{cc}-{num}"}
    if cc == "WO" and len(num) >= 8 and num[:4].isdigit():
        pats |= {f"WO-{num[2:4] + num[4:].lstrip('0')}-%", f"WO-{num[2:]}-%"}
    if cc == "JP" and num and num[0] in "HS":
        pats |= {f"JP-{num}-%", f"JP-{num[1:]}-%"}
    return sorted(pats)


def resolve(cur, pubs):
    """cited spelling -> {pub, fam, id, claims, paras} or None."""
    out = {}
    for p in pubs:
        cands = {norm(p)}
        try:
            cands |= {norm(v) for v in pubvariants(p)}
        except Exception:
            pass
        cur.execute(
            """SELECT id, publication_number, title,
                      COALESCE(NULLIF(simple_family_id,''), publication_number) fam
               FROM publications
               WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) = ANY(%s)""",
            (sorted(cands),))
        rows = cur.fetchall()
        if not rows:
            for pat in _like_patterns(p):
                cur.execute(
                    """SELECT id, publication_number, title,
                              COALESCE(NULLIF(simple_family_id,''), publication_number) fam
                       FROM publications WHERE publication_number LIKE %s LIMIT 1""", (pat,))
                rows = cur.fetchall()
                if rows:
                    break
        if not rows:
            out[p] = None
            continue
        r = rows[0]
        cur.execute("""SELECT count(*) FILTER (WHERE kind LIKE 'claim%%') c,
                              count(*) FILTER (WHERE kind='paragraph') pa
                       FROM chunks WHERE publication_id=%s""", (r["id"],))
        s = cur.fetchone()
        out[p] = {"pub": r["publication_number"], "fam": r["fam"], "id": r["id"],
                  "title": r["title"] or "", "claims": s["c"], "paras": s["pa"]}
    return out


def pubvariants(p):
    return pubnorm.variants(p)


def family_members(cur, fams):
    cur.execute("""SELECT COALESCE(NULLIF(simple_family_id,''), publication_number) fam,
                          publication_number FROM publications
                   WHERE COALESCE(NULLIF(simple_family_id,''), publication_number) = ANY(%s)""",
                (list(fams),))
    out = {}
    for r in cur.fetchall():
        out.setdefault(r["fam"], set()).add(r["publication_number"])
    return out


def audit(slug, cited):
    rep = json.load(open(f"{REPORTS}/{slug}.json"))
    try:
        view = json.load(open(f"{REPORTS}/{slug}.view.json"))
    except Exception:
        view = {"cards": []}
    dr = rep.get("deep_rank") or {}
    con = db.connect(); con.autocommit = True
    cur = con.cursor()
    res = resolve(cur, cited)
    fams = {v["fam"] for v in res.values() if v}
    members = family_members(cur, fams)

    displayed = {c["pub"]: c["rank"] for c in (view.get("cards") or [])}
    notread = {r["pub"]: i + 1 for i, r in enumerate(dr.get("not_readable") or [])}
    charted = {p: i + 1 for i, p in enumerate(dr.get("order") or [])}
    screen = dr.get("screen_scores") or {}
    cand_rank = {p: i + 1 for i, p in enumerate(dr.get("candidates") or [])}
    ranked_fams = {f: i + 1 for i, f in enumerate(rep.get("ranked_families") or [])}

    print(f"report {slug}: {len(rep.get('ranked_families') or [])} families ranked, "
          f"{dr.get('n_candidates')} screened, {dr.get('charted')} charted, "
          f"{len(view.get('cards') or [])} displayed")
    print()
    hdr = (f"{'cited':17s} {'in corpus as':20s} {'txt':>4s} {'fusion':>6s} {'scrn':>5s} "
           f"{'chart':>5s} {'CARD':>5s}  verdict")
    print(hdr); print("-" * len(hdr))
    tally = {}
    seen_fam = set()
    for p in cited:
        r = res.get(p)
        if not r:
            print(f"{p:17s} {'-':20s} {'':>4s} {'':>6s} {'':>5s} {'':>5s} {'':>5s}  NOT IN CORPUS")
            tally["not-in-corpus"] = tally.get("not-in-corpus", 0) + 1
            continue
        mem = members.get(r["fam"], {r["pub"]})
        card = min([displayed[m] for m in mem if m in displayed] or [0]) or None
        ch = min([charted[m] for m in mem if m in charted] or [0]) or None
        sc = max([screen[m] for m in mem if m in screen] or [-1])
        cr = min([cand_rank[m] for m in mem if m in cand_rank] or [0]) or None
        fr = ranked_fams.get(r["fam"])
        txt = "full" if r["claims"] and r["paras"] else ("clm" if r["claims"] else "abs")
        nr = min([notread[m] for m in mem if m in notread] or [0]) or None
        if card:
            v = "DISPLAYED"
        elif nr:
            v = f"listed as not-readable (#{nr})"
        elif ch:
            v = "charted, not displayed"
        elif sc >= 0:
            v = "screened, not read"
        elif fr:
            v = "retrieved, not screened"
        else:
            v = "NEVER RETRIEVED"
        if r["fam"] in seen_fam:
            v += " (same family as an earlier row)"
        seen_fam.add(r["fam"])
        tally[v.split(" (")[0]] = tally.get(v.split(" (")[0], 0) + 1
        print(f"{p:17s} {r['pub']:20s} {txt:>4s} {str(fr or '-'):>6s} "
              f"{(str(sc) if sc >= 0 else '-'):>5s} {str(ch or '-'):>5s} {str(card or '-'):>5s}  {v}")
    print()
    print("tally:", tally)
    uniq = {}
    for p in cited:
        r = res.get(p)
        if r:
            uniq.setdefault(r["fam"], p)
    hit = surfaced = 0
    for fam, p in uniq.items():
        mem = members.get(fam, set())
        if any(m in displayed for m in mem):
            hit += 1
            surfaced += 1
        elif any(m in notread for m in mem):
            surfaced += 1
    print(f"\nFAMILY-LEVEL RECALL: {hit} / {len(uniq)} families in corpus in the RANKED top "
          f"{len(view.get('cards') or [])}; {surfaced} / {len(uniq)} surfaced on the page "
          f"(ranked list + the not-readable section)")
    return hit, len(uniq)


if __name__ == "__main__":
    slug = sys.argv[1]
    cited = sys.argv[2].split(",")
    audit(slug, cited)

