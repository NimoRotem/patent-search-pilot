"""Exact phrase and proximity.

The precision channel. `phraseto_tsquery` requires the lexemes ADJACENT AND IN ORDER, so a hit is
a genuine occurrence of the phrase rather than a coincidence of vocabulary. It is capped at 300
publications per phrase because it is meant to be narrow: a phrase that returns thousands of
documents is not a phrase worth searching.
"""
from __future__ import annotations

from .legal import _date_clause


class ExactMixin:

    def channel_exact(self, phrases, subject=None, mode=None):
        """Exact phrase / proximity via phraseto_tsquery (ordered adjacency)."""
        if not phrases:
            return []
        dc, dp = _date_clause(subject, mode)
        out = {}
        for ph in phrases:
            sql = (f"SELECT c.publication_id, max(ts_rank_cd(c.tsv, phraseto_tsquery('english',%s))) AS score "
                   f"FROM chunks c JOIN publications p ON p.id=c.publication_id "
                   f"WHERE c.tsv @@ phraseto_tsquery('english',%s) {dc} "
                   f"GROUP BY c.publication_id ORDER BY score DESC LIMIT 300")
            for pid, sc in self._pubs_from_chunks(sql, [ph, ph, *dp], cap=300):
                out[pid] = max(out.get(pid, 0), sc)
        return sorted(out.items(), key=lambda t: t[1], reverse=True)
