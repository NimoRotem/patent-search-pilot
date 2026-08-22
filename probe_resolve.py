import sys, collections
sys.path.insert(0,"src"); sys.path.insert(0,"ops")
from dotenv import load_dotenv; load_dotenv(".env", override=False)
import psycopg
from psycopg.rows import dict_row
import embed_common, gcs_lite, pubnorm

def hyphen_candidates(pub):
    p = pubnorm.parse(pub)
    if not p: return []
    cc, num, kind = p
    out = []
    for n in pubnorm._num_variants(cc, num):
        if kind:
            out.append(f"{cc}-{n}-{kind}")
        out.append(f"{cc}-{n}")
    seen=set(); r=[]
    for x in out:
        if x not in seen:
            seen.add(x); r.append(x)
    return r

pubs=[]
for obj in gcs_lite.list_objects("nimo-patents-fulltext","parsed/"):
    rest=obj["name"][len("parsed/"):]
    pubs.append(rest.partition("/")[0])
print("objects", len(pubs))
c=psycopg.connect(row_factory=dict_row, connect_timeout=30, **embed_common.pg_params())
cur=c.cursor()
cands={}
allc=set()
for p in pubs:
    h=hyphen_candidates(p); cands[p]=h; allc.update(h)
cur.execute("SELECT publication_number, min(id) id FROM publications WHERE publication_number = ANY(%s) GROUP BY 1",(list(allc),))
found={r["publication_number"]: r["id"] for r in cur.fetchall()}
resolved={}
for p,h in cands.items():
    for x in h:
        if x in found:
            resolved[p]=found[x]; break
print("resolved", len(resolved), "of", len(pubs))
miss=[p for p in pubs if p not in resolved]
print("miss sample", miss[:10])
ids=list(resolved.values())
cur.execute("SELECT count(DISTINCT publication_id) c FROM paragraphs WHERE publication_id = ANY(%s)",(ids,))
print("of resolved, have paragraphs rows:", cur.fetchone())
cur.execute("SELECT count(DISTINCT publication_id) c FROM claims WHERE publication_id = ANY(%s)",(ids,))
print("of resolved, have claims rows:", cur.fetchone())
