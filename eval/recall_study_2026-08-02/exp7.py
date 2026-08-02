"""E7: how widespread is the wrong-abstract corruption?"""
import sys, re, random, json
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import db
con = db.connect(); con.autocommit = True; cur = con.cursor()

cur.execute("select id, publication_number, title, left(abstract,120) a from publications "
            "where id between 11180 and 11196 order by id")
print("== neighbouring ids around US-10625955-B2 (id 11188) ==")
for r in cur.fetchall():
    print(f"  {r['id']:6d} {r['publication_number']:20s} {(r['title'] or '')[:40]:40s} | {(r['a'] or '')[:70]}")

print()
print("== does that touch-display abstract belong to another publication? ==")
cur.execute("select id, publication_number, title from publications "
            "where abstract like 'A touch display device includes a display module%' limit 10")
for r in cur.fetchall():
    print("  ", r["id"], r["publication_number"], r["title"])

print()
print("== is the abstract CHUNK the same wrong text? ==")
cur.execute("select kind, left(text,90) t from chunks where publication_id=11188 "
            "and kind in ('abstract','whole') order by kind")
for r in cur.fetchall():
    print("  ", r["kind"], "|", r["t"].replace("\n", " "))

print()
print("== prevalence: title/abstract vocabulary overlap on a random sample ==")
STOP = set("a an the of and or to for with in on at by is are be as from that this it its "
           "which such said having having comprising comprises method system device apparatus "
           "using used one first second".split())
def toks(s):
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower()) if w not in STOP}
cur.execute("""select publication_number, title, abstract from publications
               where abstract is not null and length(abstract) > 200
                 and title is not null and length(title) > 12
                 and country in ('US','EP','WO')
               order by id limit 4000""")
rows = cur.fetchall()
random.seed(7); random.shuffle(rows)
sample = rows[:1500]
zero, tot, examples = 0, 0, []
for r in sample:
    t, a = toks(r["title"]), toks(r["abstract"])
    if not t:
        continue
    tot += 1
    if not (t & a):
        zero += 1
        if len(examples) < 8:
            examples.append((r["publication_number"], r["title"][:48], r["abstract"][:70]))
print(f"  sample={tot}  zero title-abstract word overlap = {zero} ({100.0*zero/max(tot,1):.1f}%)")
for e in examples:
    print(f"    {e[0]:20s} {e[1]:48s} | {e[2]}")

print()
print("== duplicate abstracts shared across unrelated publications (top offenders) ==")
cur.execute("""select left(md5(abstract),10) h, count(*) n, min(publication_number) a,
                      max(publication_number) b, min(left(abstract,60)) s
               from publications where abstract is not null and length(abstract) > 300
               group by 1 having count(*) > 1 order by n desc limit 8""")
for r in cur.fetchall():
    print(f"  n={r['n']:5d}  {r['a']} .. {r['b']}  | {r['s']}")
