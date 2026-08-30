"""Exact phrase and proximity.

The precision channel. `phraseto_tsquery` requires the lexemes ADJACENT AND IN ORDER, so a hit is
a genuine occurrence of the phrase rather than a coincidence of vocabulary. It is capped at 300
FAMILIES per phrase because it is meant to be narrow: a phrase that returns thousands of documents
is not a phrase worth searching.

THE COST IS THE PHRASE, NOT THE NUMBER OF PHRASES. MEASURED 2026-08-22 on the live corpus against
EP 3 707 092, four phrases a model produced for that subject:

    'air extraction means'    0.33 s    12 families
    'vacuum seal element'     2.80 s     4 families
    'rigid base element'      3.27 s     5 families
    'contact surface'        97.26 s   300 families

One generic two-word phrase was 94% of the channel's 103 s, because the query aggregates over the
WHOLE match set before the 1,200-row limit truncates it, and a phrase that matches tens of
thousands of chunks therefore pays for a ranking that is then thrown away. So the channel probes a
phrase's selectivity first and declines the ones it cannot afford to rank, which is the same
judgement the channel's own design already states, made with a number instead of a hope.
"""
from __future__ import annotations

import os

from .base import FAMILY_OVERFETCH
from .legal import _date_clause


PHRASE_CAP = 300       # FAMILIES per phrase, not publications

#  A phrase matching more chunks than this is declined. 20,000 is roughly 17x the 1,200 rows the
#  channel can return for one phrase (PHRASE_CAP x FAMILY_OVERFETCH), so a phrase over it cannot
#  have its ranking respected anyway: the LIMIT was always going to truncate it.
#
#  DECLINED, NOT TRUNCATED. Reading the first 20,000 matches and ranking those would look like a
#  result and be an arbitrary subset of one, which is worse than a channel that says nothing: this
#  is a PRECISION channel and a precise-looking answer drawn from an arbitrary slice is the one
#  failure mode it must not have.
PHRASE_MAX_CHUNKS = int(os.environ.get("EXACT_PHRASE_MAX_CHUNKS", "20000"))


class ExactMixin:

    def phrase_is_affordable(self, phrase):
        """False when `phrase` matches more than PHRASE_MAX_CHUNKS chunks.

        MEASURED 2026-08-22: the probe costs 1.16 s at a limit of 5,000 and 3.70 s at 40,000 for a
        phrase that fills it, against 97.26 s to rank that same phrase. A selective phrase pays
        only its own match count: 0.30 s for the 1,111 chunks of 'vacuum seal element'.

        The date window is deliberately NOT applied here. It would add the `publications` join to
        a probe whose whole purpose is to be cheaper than the query it guards, and the cost this
        is measuring is the size of the tsv match set, which the date filter does not change.
        """
        if PHRASE_MAX_CHUNKS <= 0:
            return True
        with self.conn.cursor() as c:
            c.execute("SELECT count(*) AS n FROM (SELECT 1 FROM chunks c "
                      "WHERE c.tsv @@ phraseto_tsquery('english', %s) LIMIT %s) z",
                      (phrase, PHRASE_MAX_CHUNKS))
            rows = c.fetchall()
        return int((rows[0].get("n") if rows else 0) or 0) < PHRASE_MAX_CHUNKS

    def channel_exact(self, phrases, subject=None, mode=None):
        """Exact phrase / proximity via phraseto_tsquery (ordered adjacency)."""
        if not phrases:
            return []
        dc, dp = _date_clause(subject, mode)
        out = {}
        for ph in phrases:
            if not self.phrase_is_affordable(ph):
                continue
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
