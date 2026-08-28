"""Text-depth snapshot, split by whether the enrichment landed BEFORE or AFTER the 2026-07-18
baseline eval. This matters: the SerpApi/DE enrichment (910 pubs, 07-17 21:00 -> 07-18 02:31) was
already in the committed 0.1697 baseline. Only epo:ops + sibling_recovery (07-19) are new, so only
they can be credited with any change in this run.
"""
import json, sys
sys.path.insert(0, 'src')
import db

CUTOFF = '2026-07-18 06:56:00+00'   # mtime of the committed baseline eval_results.json

gs = json.load(open('data/goldset/goldset.json'))
fams = set()
for e in gs['entries']:
    fams |= set(e['gold_families'])
fams = sorted(fams)

with db.cursor() as c:
    c.execute("""
    SELECT gf.fam,
      EXISTS(SELECT 1 FROM publications p WHERE p.simple_family_id=gf.fam) in_corpus,
      (SELECT count(*) FROM chunks ch JOIN publications p ON p.id=ch.publication_id
         WHERE p.simple_family_id=gf.fam AND ch.kind IN ('claim_own','claim_resolved')
           AND ch.embedding IS NOT NULL) n_claim_chunks,
      COALESCE((SELECT array_agg(DISTINCT s.name) FROM field_provenance fp
         JOIN sources s ON s.id=fp.source_id
         JOIN publications p ON p.id=fp.entity_id AND fp.entity='publication'
         WHERE p.simple_family_id=gf.fam AND s.name<>'bigquery:patents-public-data'
           AND fp.ingested_at <  %s::timestamptz), '{}') pre_sources,
      COALESCE((SELECT array_agg(DISTINCT s.name) FROM field_provenance fp
         JOIN sources s ON s.id=fp.source_id
         JOIN publications p ON p.id=fp.entity_id AND fp.entity='publication'
         WHERE p.simple_family_id=gf.fam AND s.name<>'bigquery:patents-public-data'
           AND fp.ingested_at >= %s::timestamptz), '{}') new_sources
    FROM unnest(%s::text[]) gf(fam)""", (CUTOFF, CUTOFF, fams))
    rows = {r['fam']: dict(r) for r in c.fetchall()}


def stratum(r):
    if not r['in_corpus']:
        return 'absent'
    if r['new_sources']:
        return 'deepened_NEW'      # OPS / sibling recovery, after the baseline
    if r['pre_sources']:
        return 'deepened_pre'      # SerpApi/DE enrichment, already in the baseline
    if r['n_claim_chunks'] > 0:
        return 'bq_deep'           # had BigQuery claims all along
    return 'thin'


for f, r in rows.items():
    r['stratum'] = stratum(r)

tot = {}
for r in rows.values():
    tot[r['stratum']] = tot.get(r['stratum'], 0) + 1

per_query = {}
for e in gs['entries']:
    g = e['gold_families']
    d = {}
    for f in g:
        s = rows[f]['stratum']
        d[s] = d.get(s, 0) + 1
    per_query[e['id']] = d

out = {'cutoff': CUTOFF, 'families': rows, 'totals': tot, 'per_query': per_query}
open('data/eval/depth_snapshot.json', 'w').write(json.dumps(out, indent=1, default=str))
print('TOTALS', json.dumps(tot, indent=1))
print()
for k, v in per_query.items():
    print(f"{k:34s} {json.dumps(v)}")
print()
print('gold families with NEW text:',
      sorted(f for f, r in rows.items() if r['stratum'] == 'deepened_NEW'))
