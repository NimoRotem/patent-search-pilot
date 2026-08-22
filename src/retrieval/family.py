"""Family identity, family collapse, and the federated ids that have to behave like local ones.

WHICH MEMBER OF A FAMILY SURVIVES A COLLAPSE IS A LEGAL QUESTION, not only a retrieval one. The
same disclosure published as a US pre-grant publication in 2020 and as a US patent in 2024 is
unconditional 102(a)(1) art in the first form and novelty-only 102(a)(2) art in the second, and
the second is disqualified outright in the US by the 102(b)(2)(C) common-ownership exception. The
rule that resolves that is `webview.resolve_family_reps`, which is applied at READING and DISPLAY
time against the subject's effective filing date, and it is the only place the citable member is
chosen.

So this module deliberately does NOT try to make that choice. It collapses on the retrieval
criterion only, the family's best-scoring member is the one that carried the evidence, so it is
the one whose rank the family inherits, and leaves the citable member to the stage that knows the
subject's date. Both the pre-existing `dedup_family` and the new `collapse_rows` use the same rule
(best-first wins), so the collapse moving earlier in the funnel does not change which document a
reader is eventually handed.
"""
from __future__ import annotations


class FamilyMixin:
    """Family key lookup, collapse and federated-id registration."""

    def family_key(self, pid):
        return self._fam.get(pid, str(pid))

    def collapse_rows(self, rows, cap):
        """Chunk-level rows -> [(publication_id, score)], ONE PER FAMILY, capped at `cap` FAMILIES.

        `rows` is any iterable of dicts with `publication_id` and `score`, already ordered
        best-first. This is the family-aware replacement for `_pubs_from_chunks`'s aggregation:
        the old order was chunk hits -> best pub -> channel limit -> fusion -> family dedup, so a
        family with forty members in the corpus could spend forty of a channel's 1,000 slots and
        then be collapsed to one anyway. Collapsing first means the cap counts distinct families,
        which is the unit the answer is actually measured in.

        Best-first ordering does the work: the first row of a family is its best chunk, so it is
        the member that gets the family's slot, exactly as `dedup_family` would have chosen after
        fusion.
        """
        best, seen = {}, set()
        for r in rows:
            pid = r["publication_id"]
            if pid in best:
                continue
            fk = self.family_key(pid)
            if fk in seen:
                continue
            seen.add(fk)
            best[pid] = r["score"]
            if len(best) >= cap:
                break
        return list(best.items())

    def collapse_pairs(self, pairs, cap=None):
        """The same collapse over an already-aggregated [(pid, score)] list, best-first.

        Channels that rank publications directly in SQL (cpc, citation, biblio) never see chunk
        rows, and the ones that pool several dense passes (qbe, crosslingual) have already
        aggregated. They collapse here instead.
        """
        out, seen = [], set()
        for pid, sc in pairs:
            fk = self.family_key(pid)
            if fk in seen:
                continue
            seen.add(fk)
            out.append((pid, sc))
            if cap is not None and len(out) >= cap:
                break
        return out

    # ---- external (federated) publications ------------------------------------------------
    def register_external(self, pid, family_key):
        """Register a virtual publication id (e.g. 'fed:EP1234567A1') so family_key() and
        dedup_family() treat a federated hit exactly like a local one."""
        self._fam[pid] = family_key

    def resolve_pub_numbers(self, keys):
        """Map normalised publication-number join keys to local rows.

        keys: ['US11999030B2', ...] (alphanumeric-only, upper-case, federation.join_key).
        -> {key: (publication_id, family_key)} for those present in the corpus.

        Matching is done in SQL against a normalised publication_number rather than by holding
        a 107k-entry dict in memory: this box is memory constrained and the lookup only runs on
        the federation path, so one sequential scan is the cheaper trade.
        """
        keys = [k for k in dict.fromkeys(keys) if k]
        if not keys:
            return {}
        sql = ("SELECT id, upper(regexp_replace(publication_number, '[^A-Za-z0-9]', '', 'g')) AS k, "
               "COALESCE(NULLIF(simple_family_id,''), publication_number) AS fam "
               "FROM publications "
               "WHERE upper(regexp_replace(publication_number, '[^A-Za-z0-9]', '', 'g')) = ANY(%s)")
        out = {}
        with self.conn.cursor() as c:
            c.execute(sql, (keys,))
            for r in c.fetchall():
                out[r["k"]] = (r["id"], r["fam"])
        return out

    def dedup_family(self, ranked):
        seen, out = set(), []
        for pid, sc, prov in ranked:
            fk = self.family_key(pid)
            if fk in seen:
                continue
            seen.add(fk)
            out.append((fk, pid, sc, prov))
        return out

    def canonical_reps(self, channel_results):
        """family_key -> the ONE publication id that will represent it through fusion.

        Collapsing inside each channel leaves a family able to arrive as pub A from the dense
        channel and pub B from the citation channel, the same disclosure, two ids, and RRF then
        splits its votes between them and neither wins. That is not a hypothetical: a family with
        a US pre-grant publication and its granted patent is the ordinary case in this corpus.

        The representative is the member that appears in the MOST channels, tie-broken by the best
        rank it reached. Ranks, never scores: channel scores are incomparable, which is the whole
        reason fusion is rank-based.
        """
        stats = {}
        for res in channel_results.values():
            for rank, (pid, _s) in enumerate(res):
                fk = self.family_key(pid)
                cur = stats.setdefault(fk, {}).setdefault(pid, [0, rank])
                cur[0] += 1
                if rank < cur[1]:
                    cur[1] = rank
        out = {}
        for fk, members in stats.items():
            out[fk] = min(members.items(), key=lambda kv: (-kv[1][0], kv[1][1], str(kv[0])))[0]
        return out

    def canonicalise(self, channel_results):
        """Rewrite every channel's hit list onto one publication id per family (`canonical_reps`).

        Order inside a channel is preserved and a channel is already family-unique after the
        collapse, so this is a straight relabelling: no hit is added, dropped or reordered.
        """
        reps = self.canonical_reps(channel_results)
        out = {}
        for name, res in channel_results.items():
            seen, rows = set(), []
            for pid, sc in res:
                rep = reps.get(self.family_key(pid), pid)
                if rep in seen:
                    continue
                seen.add(rep)
                rows.append((rep, sc))
            out[name] = rows
        return out
