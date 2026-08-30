#!/usr/bin/env bash
# Stage A driver: chunk + embed the Tier-1 CPC subclass corpus, two-tier depth.
#
# Runs to completion detached (setsid) because the whole job is many hours and must survive the
# session that started it — an earlier run died silently when its parent shell went away, having
# committed 254k rows. Every stage is resumable, so re-running this is safe at any point:
#   * chunking is driven by unchunked_publication_ids() — chunks ARE the state
#   * embedding is driven by `WHERE embedding IS NULL`
#
# TWO DECISIONS BAKED IN, both measured rather than assumed:
#
# 1. TWO-TIER DEPTH (no description paragraphs, no figure captions).
#    Measured on the frozen gold set, dense channel, everything else identical:
#      full depth 0.1658  ->  no paragraphs 0.1619  ->  no paragraphs+captions 0.1619
#    So 71% of the chunk budget buys 2.4% of relative recall. What it costs is EVIDENCE, not
#    retrieval — description is what the claim chart quotes — and the display path already
#    fetches that on demand per surfaced document.
#
# 2. THE HNSW INDEX IS LEFT IN PLACE during embedding, which is ~7x slower than the M3
#    bulk pattern (drop index -> embed -> rebuild). That is deliberate:
#      * dropping it takes vector search OFF the live app at rotem.ai/patents for the entire
#        embed + rebuild window (many hours), and
#      * rebuilding ~14.5M vectors wants maintenance_work_mem >= the finished index (~46 GB)
#        while this box has 15 GB total and a 6 GB Postgres container, so it would spill hard.
#    Incremental HNSW insert is exactly what pgvector supports; slow and up beats fast and down.
set -uo pipefail
ROOT=/home/nimrod_rotem/patent-search-pilot
PY=$ROOT/.venv/bin/python
LOG=$ROOT/data/stage_a.log
cd "$ROOT/src" || exit 1

log(){ echo "$(date -Is) $*" >> "$LOG"; }

log "=== stage A start ==="

# ---- 0. wait for any in-flight load to finish -------------------------------------------
while pgrep -f "ingest_pg" > /dev/null 2>&1; do sleep 60; done
log "load stage clear"

# ---- 1. chunk (two-tier), resumable ------------------------------------------------------
nice -n 10 ionice -c2 -n7 "$PY" -u - >> "$LOG" 2>&1 <<'PYEOF'
import sys, time; sys.path.insert(0, '.')
import incremental_ingest as ii, ingest_bq
t = time.time()
# two_tier MUST match the chunk_publications() call below. If the queue counted description-only
# publications as chunkable while the chunker skipped their paragraphs, they would come back on
# every run forever and the backlog would never read zero.
ids = ii.unchunked_publication_ids(two_tier=True)
print(f"[chunk] {len(ids):,} publications need chunking (two-tier)", flush=True)
if ids:
    orig = ii._orig_abstracts_from_staging(ingest_bq.CORE_TBL)
    print(f"[chunk] non-English abstracts available: {len(orig):,}", flush=True)
    n = ii.chunk_publications(ids, orig=orig, two_tier=True)
    print(f"[chunk] done -> {n:,} chunks in {time.time()-t:.0f}s", flush=True)
PYEOF
log "chunking done"

# ---- 2. embed, benchmark-starved subclasses FIRST ----------------------------------------
# The wide gold set measured which subclasses are corpus-limited rather than ranking-limited:
# B66C 2.6% reachable, B66F 4.0%, B25B 10.8% — against B25J 29.3% and B65G 31.4%. Embedding the
# starved ones first means the first re-measurable improvement lands hours earlier, and if the
# job is interrupted the most valuable part is already live.
nice -n 10 ionice -c2 -n7 "$PY" -u - >> "$LOG" 2>&1 <<'PYEOF'
import sys, time; sys.path.insert(0, '.')
import db, embed

def pending(where=""):
    return db.scalar(f"SELECT count(*) FROM chunks WHERE embedding IS NULL{where}") or 0

def cpc_where(sub):
    #  MUST qualify the outer column as `chunks.publication_id`.
    #
    #  This read `cl.publication_id = publication_id`, and inside the EXISTS the bare name
    #  resolves to the INNER table — so it meant `cl.publication_id = cl.publication_id`, always
    #  true. The EXISTS then only asked "does ANY classification row anywhere start with this
    #  prefix", which is trivially true, so the filter matched every pending chunk (6,260,599)
    #  rather than the subclass's 433,371. Nothing was corrupted and everything still got
    #  embedded — but the benchmark-priority ordering silently did not happen.
    #
    #  '%%' not '%': these fragments are spliced into SQL that psycopg still parses for
    #  placeholders even when no parameters are passed, so a literal LIKE wildcard must be
    #  doubled — exactly as embed.run already does for its `id %% n` shard clause.
    return (" AND EXISTS (SELECT 1 FROM classifications cl "
            "WHERE cl.publication_id = chunks.publication_id "
            f"AND replace(cl.symbol,' ','') LIKE '{sub}%%')")

# Starved first (measured on the wide gold set), then the ranking-limited ones, then a final
# unfiltered pass to catch anything whose classification rows did not match a prefix.
PRIORITY = ["B66C", "B66F", "B25B", "F16B", "B25J", "B65G"]
for sub in PRIORITY + [None]:
    where = cpc_where(sub) if sub else ""
    label = sub or "remaining"
    n = pending(where)
    print(f"[embed] {label}: {n:,} chunks pending", flush=True)
    while n:
        t = time.time()
        embed.run(limit=20000, where_sql=where)
        left = pending(where)
        print(f"[embed] {label}: +{n-left:,} in {time.time()-t:.0f}s ({left:,} left)", flush=True)
        if left >= n:      # no progress -> stop rather than spin
            break
        n = left
        time.sleep(2)
print(f"[embed] all done, {pending():,} still NULL", flush=True)
PYEOF
log "embedding done"

$PY -c "
import sys; sys.path.insert(0,'src')
import db
print('publications', db.scalar('SELECT count(*) FROM publications'))
print('chunks', db.scalar('SELECT count(*) FROM chunks'))
print('unembedded', db.scalar('SELECT count(*) FROM chunks WHERE embedding IS NULL'))
" >> "$LOG" 2>&1
log "=== stage A complete ==="
