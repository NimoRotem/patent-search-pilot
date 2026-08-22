"""Query by example: dense retrieval seeded from the best chunks of the strongest hits.

A second dense pass whose query vector is a DOCUMENT rather than the user's prose. It reaches art
that shares vocabulary with a strong hit but not with the query, which is exactly the gap a
description written by one drafter leaves against a corpus written by thousands.
"""
from __future__ import annotations


class QbeMixin:

    def channel_qbe(self, seed_pids, subject=None, mode=None, per=1):
        """Query-by-example: dense search from the best chunks of the strongest hits."""
        if not seed_pids:
            return []
        with self.conn.cursor() as c:
            c.execute("SELECT embedding FROM chunks WHERE publication_id = ANY(%s) "
                      "AND kind IN ('whole','abstract','claim_own') AND embedding IS NOT NULL "
                      "LIMIT %s", (seed_pids[:5], per * 5))
            vecs = [r["embedding"] for r in c.fetchall()]
        agg = {}
        for v in vecs[:per]:
            for pid, sc in self.channel_dense_raw(v, subject, mode, limit=500):
                agg[pid] = max(agg.get(pid, 0), sc)
        pooled = sorted(agg.items(), key=lambda t: t[1], reverse=True)
        return self.collapse_pairs(pooled, self._cap())
