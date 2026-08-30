"""Sanity check: are the OPS-backfilled chunks actually LIVE in the HNSW index?

A null result ("deepening didn't help") is only meaningful if the new text is genuinely searchable.
Test: take OPS-added claim chunks from newly-deepened GOLD families, use each chunk's own text as
the query, and check the chunk's own publication comes back top-1 under the same retrieval settings
the app uses. If self-retrieval fails, the backfill never became searchable and the null result is
an artifact.
"""
import sys, json
sys.path.insert(0, 'src')
import db, embed
from retrieval import _vec

NEW_GOLD = ['26224347', '32297463', '32981160', '39244538', '42115107',
            '45554511', '56830674', '63449883', '6918853', '70050062', '7961965']

conn = db.connect()
conn.autocommit = True
with conn.cursor() as c:
    c.execute("SET hnsw.ef_search = 200")
    c.execute("SET hnsw.iterative_scan = relaxed_order")
    c.execute("SET hnsw.max_scan_tuples = 12000")

# pick claim chunks belonging to pubs that OPS enriched, in gold families
with conn.cursor() as c:
    c.execute("""
      SELECT ch.id, ch.publication_id, ch.text, p.publication_number, p.simple_family_id fam
      FROM chunks ch
      JOIN publications p ON p.id = ch.publication_id
      WHERE p.simple_family_id = ANY(%s)
        AND ch.kind IN ('claim_own','claim_resolved')
        AND ch.embedding IS NOT NULL
        AND length(ch.text) > 300
        AND EXISTS (SELECT 1 FROM field_provenance fp JOIN sources s ON s.id=fp.source_id
                    WHERE fp.entity='publication' AND fp.entity_id=p.id AND s.name='epo:ops')
      ORDER BY random() LIMIT 8""", (NEW_GOLD,))
    samples = c.fetchall()

print(f"testing {len(samples)} OPS-added claim chunks from gold families\n")
ok = 0
for s in samples:
    qv = _vec(embed.embed_query(s['text'][:2000], 768))
    with conn.cursor() as c:
        c.execute("SELECT c.publication_id, 1-(c.embedding <=> %s::vector) score "
                  "FROM chunks c WHERE c.embedding IS NOT NULL "
                  "ORDER BY c.embedding <=> %s::vector LIMIT 5", (qv, qv))
        hits = c.fetchall()
    top_pids = [h['publication_id'] for h in hits]
    hit = s['publication_id'] in top_pids
    ok += hit
    print(f"  {s['publication_number']:22s} fam={s['fam']:9s} self-retrieved={hit} "
          f"top1_score={hits[0]['score']:.3f}")

print(f"\nself-retrieval: {ok}/{len(samples)} -> "
      f"{'OPS chunks ARE live in the index' if ok == len(samples) else 'INDEXING PROBLEM'}")
