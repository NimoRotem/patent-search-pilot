"""Where does a known citation list actually land? Stage 1: is it in the corpus at all.

Deliberately generic: it takes a list of publication numbers in whatever spelling a human pasted
them, normalises every plausible variant, and reports corpus presence, text depth and family.
Nothing here knows anything about a particular subject patent.
"""
import os
import re
import sys

sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db
import pubnorm

#  The citation list is an ARGUMENT, never a constant: this is a ruler, and a ruler with the
#  answer written on it measures nothing. Pass publication numbers separated by commas or spaces.
RAW = " ".join(sys.argv[1:])


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def candidates(pub):
    """Every spelling worth trying against publications.publication_number, normalised."""
    out = {norm(pub)}
    try:
        for v in pubnorm.variants(pub):
            out.add(norm(v))
    except Exception:
        pass
    m = re.match(r"^([A-Z]{2})([A-Z]?[0-9]+)([A-Z][0-9]?)?$", norm(pub))
    if m:
        cc, num, kind = m.group(1), m.group(2), m.group(3) or ""
        out.add(cc + num)                       # kindless
        out.add(cc + num + kind)
        #  WO short years: WO0121357 is WO2001/21357; WO9744592 is WO1997/44592.
        if cc == "WO" and len(num) >= 7 and num[:2].isdigit():
            yy = int(num[:2])
            century = "19" if yy >= 50 else "20"
            out.add(cc + century + num[:2] + num[2:] + kind)
            out.add(cc + century + num[:2] + num[2:])
        #  Old JP kokai carry an era letter that some sources drop.
        if cc == "JP" and num.startswith(("H", "S")):
            out.add(cc + num[1:] + kind)
    return {x for x in out if x}


def like_patterns(pub):
    """LIKE patterns for the cases an exact set cannot cover: a cited number given WITHOUT a kind
    code (US5795001 is stored as US-5795001-A), and a WO number given with a four-digit year when
    the corpus stores the two-digit form (WO1997044592A1 vs WO-9744592-A1)."""
    m = re.match(r"^([A-Z]{2})([A-Z]?[0-9]+?)([A-Z][0-9]?)?$", norm(pub))
    if not m:
        return []
    cc, num = m.group(1), m.group(2)
    pats = {f"{cc}-{num}-%", f"{cc}-{num}"}
    if cc == "WO" and len(num) >= 8 and num[:4].isdigit():
        short = num[2:4] + num[4:].lstrip("0")
        pats |= {f"WO-{short}-%", f"WO-{num[2:]}-%"}
    if cc == "JP" and num and num[0] in "HS":
        pats |= {f"JP-{num}-%", f"JP-{num[1:]}-%"}
    return sorted(pats)


def main():
    pubs = sorted({p for p in re.split(r"[,\s]+", RAW) if p})
    if not pubs:
        raise SystemExit("usage: gold_probe.py PUB[,PUB...]")
    con = db.connect()
    con.autocommit = True
    cur = con.cursor()
    print(f"{'cited':18s} {'found as':22s} {'clm':>4s} {'par':>4s} {'emb':>4s} {'family':>10s}  title")
    found, missing = [], []
    for p in pubs:
        cands = sorted(candidates(p))
        cur.execute(
            """SELECT id, publication_number, title,
                      COALESCE(NULLIF(simple_family_id,''), publication_number) fam
               FROM publications
               WHERE upper(regexp_replace(publication_number,'[^A-Za-z0-9]','','g')) = ANY(%s)""",
            (cands,))
        rows = cur.fetchall()
        if not rows:
            for pat in like_patterns(p):
                cur.execute(
                    """SELECT id, publication_number, title,
                              COALESCE(NULLIF(simple_family_id,''), publication_number) fam
                       FROM publications WHERE publication_number LIKE %s LIMIT 4""", (pat,))
                rows = cur.fetchall()
                if rows:
                    break
        if not rows:
            missing.append((p, cands))
            print(f"{p:18s} {'NOT IN CORPUS':22s}")
            continue
        r = rows[0]
        cur.execute("""SELECT count(*) FILTER (WHERE kind LIKE 'claim%%') c,
                              count(*) FILTER (WHERE kind='paragraph') pa,
                              count(*) FILTER (WHERE embedding IS NOT NULL) e
                       FROM chunks WHERE publication_id=%s""", (r["id"],))
        s = cur.fetchone()
        found.append({"cited": p, "pub": r["publication_number"], "fam": r["fam"],
                      "id": r["id"], "claims": s["c"], "paras": s["pa"], "emb": s["e"]})
        print(f"{p:18s} {r['publication_number']:22s} {s['c']:4d} {s['pa']:4d} {s['e']:4d} "
              f"{str(r['fam']):>10s}  {(r['title'] or '')[:44]}")
    print()
    print(f"in corpus: {len(found)} of {len(pubs)}   missing: {len(missing)}")
    if missing:
        print("missing:", ", ".join(m[0] for m in missing))
    print("\ndistinct families in corpus:", len({f['fam'] for f in found}))
    thin = [f["cited"] for f in found if f["claims"] == 0]
    print("in corpus but with NO claim text:", len(thin), thin)
    unemb = [f["cited"] for f in found if f["emb"] == 0]
    print("in corpus but NOT embedded:", len(unemb), unemb)
    import json
    with open(os.environ.get("GOLD_OUT", "/tmp/gold_found.json"), "w") as fh:
        json.dump({"found": found, "missing": [m[0] for m in missing]}, fh)


if __name__ == "__main__":
    main()
