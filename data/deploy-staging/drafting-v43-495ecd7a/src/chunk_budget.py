"""Diagnostic: is the dense channel chunk-budget-bound rather than corpus-bound?

channel_dense pulls a FIXED CHUNK_FETCH=4000 chunks ordered by vector distance, then aggregates
to distinct publications (PUB_CAP=1000). If deepening a document multiplies its chunk count, that
document consumes proportionally more of the fixed budget -- so the number of DISTINCT families the
channel can surface FALLS as text depth RISES. That would explain a recall-neutral OPS backfill.

Measures, per gold query: chunks fetched, distinct pubs/families yielded, and the chunk-budget
share consumed by the top-N most chunk-heavy publications.
"""
import json, sys, statistics
sys.path.insert(0, 'src')
import db, embed, goldset, evaluate
from retrieval import _vec, _date_clause, CHUNK_FETCH, PUB_CAP

gs = goldset.load()
depth = json.loads(open('data/eval/depth_snapshot.json').read())
strat = {f: r['stratum'] for f, r in depth['families'].items()}

conn = db.connect()
conn.autocommit = True
with conn.cursor() as c:
    c.execute("SET hnsw.ef_search = 200")
    c.execute("SET hnsw.iterative_scan = relaxed_order")
    c.execute("SET hnsw.max_scan_tuples = 12000")

rows = []
for e in gs['entries']:
    subj = evaluate.subject_from(e)
    dc, dp = _date_clause(subj, e['mode'])
    qv = _vec(embed.embed_query(e['query_text'][:8000], 768))
    sql = (f"SELECT c.publication_id, p.simple_family_id fam "
           f"FROM chunks c JOIN publications p ON p.id=c.publication_id "
           f"WHERE c.embedding IS NOT NULL {dc} "
           f"ORDER BY c.embedding <=> %s::vector LIMIT %s")
    with conn.cursor() as c:
        c.execute(sql, [*dp, qv, CHUNK_FETCH])
        fetched = c.fetchall()
    n = len(fetched)
    per_pub = {}
    for r in fetched:
        per_pub[r['publication_id']] = per_pub.get(r['publication_id'], 0) + 1
    fams = set(r['fam'] for r in fetched if r['fam'])
    gold = set(e['gold_families'])
    # how much of the budget did the 20 heaviest publications eat?
    heavy = sorted(per_pub.values(), reverse=True)
    top20_share = round(sum(heavy[:20]) / max(1, n), 3)
    rows.append({
        'query': e['id'], 'chunks_fetched': n,
        'distinct_pubs': len(per_pub), 'distinct_families': len(fams),
        'chunks_per_pub': round(n / max(1, len(per_pub)), 1),
        'top20_pub_budget_share': top20_share,
        'gold_families_in_dense_pool': len(fams & gold),
        'gold_deepened_NEW_in_pool': len([f for f in (fams & gold) if strat.get(f) == 'deepened_NEW']),
    })
    print(json.dumps(rows[-1]), flush=True)

print()
print('MEAN distinct pubs from a', CHUNK_FETCH, 'chunk budget:',
      round(statistics.mean(r['distinct_pubs'] for r in rows), 1))
print('MEAN chunks consumed per distinct pub:',
      round(statistics.mean(r['chunks_per_pub'] for r in rows), 2))
print('PUB_CAP is', PUB_CAP, '-> the cap is',
      'NOT the binding constraint' if statistics.mean(r['distinct_pubs'] for r in rows) < PUB_CAP * 0.9
      else 'binding')
open('data/eval/chunk_budget.json', 'w').write(json.dumps(rows, indent=1))
print('[diag] wrote data/eval/chunk_budget.json')
