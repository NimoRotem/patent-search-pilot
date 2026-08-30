"""The one place a channel NAME becomes a channel CALL.

Both tiers go through this table. The hot tier calls it with the retriever that owns the hot
corpus connection; the cold tier calls it with a retriever bound to a woken domain shard
(`retrieval.cold.bind`). That is the whole reason the shard seam hands back a CONNECTION and not a
`search()`: a cold shard holds the same schema as the hot corpus, so `cold:dense` must be the same
SQL as `dense`, not a second implementation that drifts from it a fix at a time.

It also settles two things that used to be an `if/elif` chain inside `search()`:

* WHICH PHASE a channel belongs to. `citation` and `qbe` consume the strong seeds fused out of
  phase 1, which is a data dependency, not an ordering preference.
* WHETHER ITS INPUT EXISTS. `exact` needs phrases, `biblio` needs assignee hints, `crosslingual`
  needs the alternate vectors. A channel whose input is missing is not run at all, and never was.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#  Channels whose inputs all exist at request start.
PHASE1 = ("dense", "claim_dense", "brief_dense", "bm25", "claim_bm25", "exact", "cpc",
          "biblio", "crosslingual")
#  Channels that expand around / re-query from the strong seeds fused out of phase 1.
PHASE2 = ("citation", "qbe")

#  The tier names that are not channels of their own: they mirror the preset's OTHER channels onto
#  another corpus (`cold`) or stand for one external engine (`global`).
TIERS = ("cold", "global")


@dataclass(frozen=True)
class ChannelArgs:
    """Everything a channel might need, resolved once per search."""
    query: str = ""
    qvec: object = None
    subject: object = None
    mode: object = None
    cpc_hints: object = None
    assignee_hints: object = None
    phrases: object = None
    alt_query_vecs: object = None
    seeds: tuple = field(default_factory=tuple)

    def with_seeds(self, seeds):
        return ChannelArgs(self.query, self.qvec, self.subject, self.mode, self.cpc_hints,
                           self.assignee_hints, self.phrases, self.alt_query_vecs,
                           tuple(seeds or ()))


def phase_of(name):
    """1, 2, or None for a name that is not a channel."""
    if name in PHASE1:
        return 1
    if name in PHASE2:
        return 2
    return None


def has_input(name, args: ChannelArgs) -> bool:
    """False when the channel's own input is missing, which is not a failure, it is silence.

    Kept EXACTLY as the `if/elif` chain in `search()` had it: `exact` without phrases, `biblio`
    without assignee hints and `crosslingual` without alternate vectors were never submitted, and
    a channel that is not submitted contributes no key at all rather than an empty list.
    """
    if name == "exact":
        return bool(args.phrases)
    if name == "biblio":
        return bool(args.assignee_hints)
    if name == "crosslingual":
        return bool(args.alt_query_vecs)
    #  `citation` and `qbe` are always submitted, seeds or none, exactly as the `if/elif` chain
    #  did. Each guards its own empty seed list and returns [] without issuing a query, and a
    #  channel that was asked and had nothing to say contributes an empty list rather than
    #  disappearing from `channel_hits`, which is a different thing to a reader.
    return name in PHASE1 or name in PHASE2


def call(retriever, name, args: ChannelArgs):
    """Run one channel on `retriever`. -> [(publication_id, score)] best-first."""
    r, s, m = retriever, args.subject, args.mode
    if name == "dense":
        return r.channel_dense(args.qvec, s, m)
    if name == "claim_dense":
        return r.channel_claim_dense(args.qvec, s, m)
    if name == "brief_dense":
        return r.channel_brief_dense(args.qvec, s, m)
    if name == "bm25":
        return r.channel_bm25(args.query, s, m)
    if name == "claim_bm25":
        return r.channel_claim_bm25(args.query, s, m)
    if name == "exact":
        return r.channel_exact(args.phrases, s, m)
    if name == "cpc":
        return r.channel_cpc(args.cpc_hints, s, m)
    if name == "biblio":
        return r.channel_biblio(args.assignee_hints, s, m)
    if name == "crosslingual":
        return r.channel_crosslingual(args.alt_query_vecs, s, m)
    if name == "citation":
        return r.channel_citation_family(list(args.seeds), s, m)
    if name == "qbe":
        return r.channel_qbe(list(args.seeds), s, m)
    raise ValueError(f"unknown retrieval channel {name!r}")


def thunk(retriever, name, args: ChannelArgs):
    """`call` as a zero-argument callable, for submission to a pool."""
    def run():
        return call(retriever, name, args)
    return run
