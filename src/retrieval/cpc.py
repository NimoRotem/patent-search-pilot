"""Classification: the CPC/IPC channel and the subject's own symbols.

The measured position, from the leave-one-out ablation: removing this channel costs -0.033
recall@2500, the LARGEST single-channel contribution of any channel in the fusion, and it does
that while ranking by a signal that is not relevance. It is also the most expensive query in the
funnel at 7.2 to 7.8 s a call, and a deep run makes about thirty of those calls. Both facts are
about the same query, because nothing in the pipeline ever passed `cpc_hints`, so every one of
those thirty calls asked the same question and got the same answer.
"""
from __future__ import annotations

import os

from config import SEED_CPC
from .legal import _date_clause

#  How many of the subject's own classification symbols the CPC channel may ask about. Its own
#  symbols are the high-signal hint; the eight seed branches are the fallback when there is no
#  subject, and they match ~82,000 publications, i.e. almost no signal at all.
CPC_HINT_MAX = int(os.environ.get("CPC_HINT_MAX", "12"))


class CpcMixin:

    def subject_cpc(self, subject):
        """The subject's OWN classification symbols, most specific first. [] when unknown.

        This is what the CPC channel should be asking about. Nothing in the pipeline ever passed
        `cpc_hints`, so the channel fell back to SEED_CPC -- all eight indexed branches, ~82,000
        publications -- and returned the same documents for every query in the corpus.
        """
        num = getattr(subject, "number", None) if subject is not None else None
        if not num:
            return []
        with self.conn.cursor() as c:
            c.execute(
                """SELECT DISTINCT cl.symbol FROM classifications cl
                   JOIN publications p ON p.id = cl.publication_id
                   WHERE p.publication_number = %s AND cl.symbol IS NOT NULL""", (num,))
            syms = [r["symbol"] for r in c.fetchall()]
        #  Longest (most specific) first, and drop a symbol that is merely a prefix of another
        #  we already have, so the LIKE set stays tight.
        syms.sort(key=len, reverse=True)
        out = []
        for s in syms:
            if not any(o.startswith(s) for o in out):
                out.append(s)
        return out[:CPC_HINT_MAX]

    def channel_cpc(self, cpc_hints, subject=None, mode=None):
        """Publications in the indexed field, ranked by classification-match count.

        This channel is weak and two attempts to strengthen it both made it WORSE. Recorded so
        neither is retried.

        What it does today: nothing in the pipeline ever passes `cpc_hints`, so `hints` is always
        SEED_CPC, all eight indexed branches, ~82,000 publications. `count(*)` then ranks by how
        many matching symbols a publication carries, which is not relevance but how heavily
        classified a document is, so the channel returns much the same documents for every query
        in the corpus. Against a real citation list it surfaced 2 of 12 cited documents, at ranks
        1,251 and 2,139 of 2,500.

        TRIED AND REFUTED, both measured on the same 12:

          1. Narrow the pool to the SUBJECT'S OWN symbols and rank by match specificity: 0 of 12.
             Examiner citations do not sit in the subject's own subgroups. The cited documents
             share only 3 to 10 characters of CPC prefix with EP 3 707 092, most of them 3 (the
             subclass), and live in neighbouring groups -- B25J robot grippers, B65G conveyors,
             B23Q machine tools -- not its own B66C1/023.
          2. Keep the broad pool but order it by deepest shared prefix with the subject: also
             0 of 12. The pool is capped at PUB_CAP, and proximity ranking spends that entire
             budget on the subject's immediate neighbourhood, which is precisely where the cited
             art is NOT.

        The honest reading is that these documents are not reachable by classification at this
        pool size: they are also beyond the 50,000 nearest chunks in embedding space. Improving
        this channel needs a bigger pool or a different signal, not a better ordering of 2,500.
        """
        hints = list(cpc_hints or SEED_CPC)
        dc, dp = _date_clause(subject, mode)
        like = " OR ".join(["cl.symbol LIKE %s"] * len(hints))
        params = [h + "%" for h in hints] + list(dp)
        sql = (f"SELECT p.id AS publication_id, count(*) AS score "
               f"FROM classifications cl JOIN publications p ON p.id=cl.publication_id "
               f"WHERE ({like}) {dc} GROUP BY p.id ORDER BY score DESC LIMIT %s")
        with self.conn.cursor() as c:
            c.execute(sql, params + [self._cap()])
            return [(r["publication_id"], r["score"]) for r in c.fetchall()]
