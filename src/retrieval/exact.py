"""Exact phrase and proximity.

The precision channel. `phraseto_tsquery` requires the lexemes ADJACENT AND IN ORDER, so a hit is
a genuine occurrence of the phrase rather than a coincidence of vocabulary. It is capped at 300
publications per phrase because it is meant to be narrow: a phrase that returns thousands of
documents is not a phrase worth searching.
"""
from __future__ import annotations

from .base import FAMILY_OVERFETCH
from .legal import _date_clause


PHRASE_CAP = 300       # FAMILIES per phrase, not publications


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
                   f"GROUP BY c.publication_id ORDER BY score DESC LIMIT %s")
            #  The 300 counts FAMILIES, so the database has to be asked for more than 300 rows or
            #  the collapse can only ever return fewer. A literal LIMIT 300 here truncated to 300
            #  publications first and made the family cap unreachable.
            for pid, sc in self._families_from_chunks(
                    sql, [ph, ph, *dp, PHRASE_CAP * FAMILY_OVERFETCH], PHRASE_CAP):
                out[pid] = max(out.get(pid, 0), sc)
        pooled = sorted(out.items(), key=lambda t: t[1], reverse=True)
        #  Each phrase is capped at PHRASE_CAP families on its own. Pooling only has to remove
        #  families that TWO phrases both found; it must NOT re-cap, because two phrases that each
        #  legitimately fill their own budget are worth more than 300 families between them and a
        #  global cap here would silently redefine the channel.
        return self.collapse_pairs(pooled)
