"""Citation graph and DOCDB family expansion around the strongest hits.

32,579,883 edges. Directions are weighted differently on purpose: what a strong hit CITES is the
best signal (w=3, an examiner or applicant already judged it relevant to this subject matter), a
same-family member is next (w=2), and what CITES the strong hit is the weakest (w=1, it is usually
later art). `citations.origin` also carries the search-report relevance codes, X on 340,000 edges
and Y on 1,180,521, which are an evaluation label source at a scale nothing here uses yet.
"""
from __future__ import annotations

from .legal import _date_clause


class CitationMixin:

    def channel_citation_family(self, seed_pids, subject=None, mode=None):
        """Expand strong hits along citation edges + DOCDB family (both directions)."""
        if not seed_pids:
            return []
        dc, dp = _date_clause(subject, mode)
        seeds = seed_pids[:40]
        inlist = "(" + ",".join(["%s"] * len(seeds)) + ")"
        sql = (
            f"WITH seed AS (SELECT id, publication_number, simple_family_id FROM publications WHERE id IN {inlist}), "
            f"fam AS (SELECT p.id, 2 AS w FROM publications p JOIN seed s ON p.simple_family_id=s.simple_family_id "
            f"        WHERE p.simple_family_id IS NOT NULL), "
            f"cited AS (SELECT p.id, 3 AS w FROM citations ci JOIN seed s ON ci.src_pub=s.publication_number "
            f"          JOIN publications p ON p.publication_number=ci.dst_pub), "
            f"citing AS (SELECT p.id, 1 AS w FROM citations ci JOIN seed s ON ci.dst_pub=s.publication_number "
            f"           JOIN publications p ON p.publication_number=ci.src_pub), "
            f"u AS (SELECT id, sum(w) AS score FROM (SELECT * FROM fam UNION ALL SELECT * FROM cited "
            f"      UNION ALL SELECT * FROM citing) z GROUP BY id) "
            f"SELECT u.id AS publication_id, u.score FROM u JOIN publications p ON p.id=u.id "
            f"WHERE true {dc} ORDER BY u.score DESC LIMIT %s")
        with self.conn.cursor() as c:
            c.execute(sql, list(seeds) + list(dp) + [self._cap()])
            return [(r["publication_id"], float(r["score"])) for r in c.fetchall()]
