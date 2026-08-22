"""The Retriever: one object, every channel, and the pass that runs them.

Adaptive multi-channel retrieval cascade + RRF + family dedup + rerank (spec §6).

Separate channels, fused by reciprocal-rank fusion (never mixing raw incomparable scores):
 1 exact/phrase/proximity   2 BM25 (top ~1000)      3 dense (top ~1000)
 4 CPC/IPC hierarchy        5 citation+family graph 6 query-by-example
 7 cross-lingual DE<->EN    8 bibliographic (assignee/inventor)

Date/status filtering (spec §5) is AND-ed into every candidate query when a Subject+Mode is
given, so only citable prior art is returned. Widths kept wide even though the corpus is small
, the pilot measures recall, not speed.

STRUCTURE. Each channel lives in its own module as a mixin over `base.RetrieverBase`, which owns
the connection, the ANN width and the chunk-to-publication aggregation they all share. The mixins
are composed here rather than in `__init__` so that importing one channel does not drag in the
whole package.

CONCURRENCY. `search()` still runs its channels in sequence. `base.RetrieverBase` already resolves
a connection PER THREAD, which is the piece that makes a fan-out safe; the fan-out itself is the
next commit on this branch and is deliberately not smuggled into the split.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import embed

from .base import RetrieverBase
from .citations import CitationMixin
from .cpc import CpcMixin
from .dense import DenseMixin
from .exact import ExactMixin
from .family import FamilyMixin
from .fusion import RERANK_TOP, FusionMixin
from .legal import as_mode
from .lexical import LexicalMixin
from .qbe import QbeMixin

#  Channel presets. A caller may also pass an explicit list of channel names.
PRESETS = {
    "keyword": ["exact", "bm25"],
    "vector": ["dense"],
    "hybrid": ["exact", "bm25", "dense", "cpc"],
    "hybrid_rerank": ["exact", "bm25", "dense", "cpc"],
    "agentic": ["dense", "brief_dense", "cpc", "citation", "qbe", "biblio",
                "crosslingual"],
    #  Claim focus BOOSTS claim text, it does not delete the rest of the patent.
    #  MEASURED (RECALL_STUDY_2026-08-02.md): this preset used to omit `dense`, so
    #  description paragraphs were unsearchable. On the case that prompted the rebuild the
    #  best passage of the #1 result was a description paragraph, the best passage of the
    #  reference the searcher named was a description paragraph, and only 10 of the 25
    #  displayed cards matched on a claim at all.
    "claim_agentic": ["claim_dense", "dense", "brief_dense", "claim_bm25", "cpc",
                      "citation", "qbe", "biblio", "crosslingual"],
}


@dataclass
class Result:
    ranked_pubs: list           # [(publication_id, fused_score, provenance dict)]
    family_ranked: list         # [(family_key, publication_id, fused_score, provenance)]
    channel_hits: dict          # channel -> [publication_id,...] in rank order
    query: str
    # --- optional, populated by the federation bridge / domain detector -------------------
    # publication_id -> federation.FederatedHit for hits that have NO local row. A local id is
    # a bigint; an external one is the string "fed:<PUBNUM>". Renderers must look here when a
    # publication_id is not an int.
    external: dict = field(default_factory=dict)
    federation: dict = None     # federation.FederatedResult.to_dict(), when federation ran
    domain: dict = None         # domain_detect.DomainVerdict.to_dict(), when it was computed
    # True when the query looks out-of-domain and the UI should OFFER the wider federated
    # search. Federation is never run implicitly, see federation.search_two_tier.
    federation_offered: bool = False

    def channel_hits_ranked(self) -> dict:
        """channel_hits as {name: [(pid, score)]} so a Result can be re-fused. Only the rank
        ORDER matters to RRF, so a synthetic descending score is sufficient."""
        return {k: [(p, float(len(v) - i)) for i, p in enumerate(v)]
                for k, v in (self.channel_hits or {}).items()}

    def is_external(self, pid) -> bool:
        return isinstance(pid, str) and pid.startswith("fed:")


class Retriever(DenseMixin, LexicalMixin, ExactMixin, CpcMixin, CitationMixin, QbeMixin,
                FamilyMixin, FusionMixin, RetrieverBase):
    """Every channel, the fusion over them, and the family collapse that follows."""

    # ---- top-level search ----------------------------------------------------------------
    def search(self, query, subject=None, mode=None, config="hybrid",
               cpc_hints=None, assignee_hints=None, phrases=None, alt_query_vecs=None,
               do_rerank=None, topk=1000, wide=False):
        """`wide=True` runs the funnel at the seed profile (see SEED_CHUNK_FETCH). Reserved for
        whole-invention passes: it roughly doubles the pass and there are ~20 element passes."""
        mode = as_mode(mode)
        if not query or not query.strip():          # degenerate: an empty query has no signal
            return Result(ranked_pubs=[], family_ranked=[], channel_hits={}, query=query or "")
        if bool(wide) != getattr(self, "_wide", False):
            self.scan_profile(wide=bool(wide))
        qvec = embed.embed_query(query[:8000], 768)
        ch = {}
        presets = PRESETS
        # Callers may pass an explicit bounded channel sequence.  Resolve that before a mapping
        # lookup: ``dict.get(config, ...)`` still hashes ``config`` before evaluating its fallback,
        # so a list raises ``TypeError: unhashable type: 'list'`` even though it is otherwise a
        # valid explicit preset.
        if isinstance(config, (list, tuple)):
            preset = list(config)
        else:
            preset = presets.get(config, ["bm25", "dense"])
        # Cross-lingual query translation is available (query_translations) and used by the agent,
        # but M5 diagnosis showed it does NOT help the DE gap and even hurts (the corpus is
        # English-dominant, so translating a German query to English promotes English distractors).
        # The DE fix that worked is embedding the enriched DE/EP/WO claims (see M5 report). Enable
        # per-request via alt_query_vecs / xlingual=True when a caller wants it.
        if getattr(self, "_force_xlingual", False) and "crosslingual" not in preset:
            preset = preset + ["crosslingual"]
        if ("crosslingual" in preset and not alt_query_vecs
                and config not in ("agentic", "claim_agentic")):
            alt_query_vecs = self.query_translations(query)

        if "dense" in preset:
            ch["dense"] = self.channel_dense(qvec, subject, mode)
        if "claim_dense" in preset:
            ch["claim_dense"] = self.channel_claim_dense(qvec, subject, mode)
        if "brief_dense" in preset:
            ch["brief_dense"] = self.channel_brief_dense(qvec, subject, mode)
        if "bm25" in preset:
            ch["bm25"] = self.channel_bm25(query, subject, mode)
        if "claim_bm25" in preset:
            ch["claim_bm25"] = self.channel_claim_bm25(query, subject, mode)
        if "exact" in preset and phrases:
            ch["exact"] = self.channel_exact(phrases, subject, mode)
        if "cpc" in preset:
            ch["cpc"] = self.channel_cpc(cpc_hints, subject, mode)
        # channels that depend on strong base hits
        base_fused = self.rrf({k: v for k, v in ch.items()})
        strong = [pid for pid, _, _ in base_fused[:40]]
        if "citation" in preset:
            ch["citation"] = self.channel_citation_family(strong, subject, mode)
        if "qbe" in preset:
            ch["qbe"] = self.channel_qbe(strong, subject, mode)
        if "biblio" in preset and assignee_hints:
            ch["biblio"] = self.channel_biblio(assignee_hints, subject, mode)
        if "crosslingual" in preset and alt_query_vecs:
            ch["crosslingual"] = self.channel_crosslingual(alt_query_vecs, subject, mode)

        fused = self.rrf(ch)
        fam = self.dedup_family(fused)

        do_rerank = (config == "hybrid_rerank") if do_rerank is None else do_rerank
        if do_rerank and fam:
            fam = self.rerank_families(query, fam, top=min(RERANK_TOP, len(fam)))
        return Result(ranked_pubs=[(p, s, pr) for _, p, s, pr in fam][:topk],
                      family_ranked=fam[:topk], channel_hits={k: [p for p, _ in v] for k, v in ch.items()},
                      query=query)
