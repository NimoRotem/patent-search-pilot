"""Citation graph and DOCDB family expansion around the strongest hits.

32,579,883 edges. Directions are weighted differently on purpose: what a strong hit CITES is the
best signal (w=3, an examiner or applicant already judged it relevant to this subject matter), a
same-family member is next (w=2), and what CITES the strong hit is the weakest (w=1, it is usually
later art). `citations.origin` also carries the search-report relevance codes, X on 340,000 edges
and Y on 1,180,521, which are an evaluation label source at a scale nothing here uses yet.
"""
from __future__ import annotations

from .base import FAMILY_OVERFETCH
from .family import family_id_sql
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
            f"WITH seed AS (SELECT id, publication_number, {family_id_sql()} AS fam_id "
            f"              FROM publications WHERE id IN {inlist}), "
            #  Join on the family id with DOCDB's sentinels folded to NULL. Joining on the raw
            #  column made the 21,862 publications that carry '-1' one family of 21,862 members,
            #  so a single seed in that set flooded this channel with unrelated documents at w=2.
            f"fam AS (SELECT p.id, 2 AS w FROM publications p JOIN seed s ON {family_id_sql('p')}=s.fam_id "
            f"        WHERE s.fam_id IS NOT NULL), "
            f"cited AS (SELECT p.id, 3 AS w FROM citations ci JOIN seed s ON ci.src_pub=s.publication_number "
            f"          JOIN publications p ON p.publication_number=ci.dst_pub), "
            f"citing AS (SELECT p.id, 1 AS w FROM citations ci JOIN seed s ON ci.dst_pub=s.publication_number "
            f"           JOIN publications p ON p.publication_number=ci.src_pub), "
            f"u AS (SELECT id, sum(w) AS score FROM (SELECT * FROM fam UNION ALL SELECT * FROM cited "
            f"      UNION ALL SELECT * FROM citing) z GROUP BY id) "
            f"SELECT u.id AS publication_id, u.score FROM u JOIN publications p ON p.id=u.id "
            f"WHERE true {dc} ORDER BY u.score DESC LIMIT %s")
        with self.conn.cursor() as c:
            c.execute(sql, list(seeds) + list(dp) + [self._cap() * FAMILY_OVERFETCH])
            rows = [(r["publication_id"], float(r["score"])) for r in c.fetchall()]
        return self.collapse_pairs(rows, self._cap())
