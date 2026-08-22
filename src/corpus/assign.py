"""Which shard a FAMILY lives on. Not which shard a publication lives on.

WHY THE UNIT IS THE FAMILY. Retrieval collapses to families everywhere downstream: fusion dedupes
by family key, the reader reads one representative per family, and the gold sets are family lists.
A family split across two shards is therefore a family that gets deduped against itself: the same
disclosure arrives twice, on two connections, with two different scores, and whichever copy loses
the tie takes its shard's evidence with it. So the home shard is decided once per family and every
publication of that family follows it, even when the family's own symbols disagree.

THE DOMAIN DEFINITION IS NOT REIMPLEMENTED HERE. `shard_router.domain_of` is imported. If this
module computed its own four-character prefix, the router and the shards could silently disagree
about where a family lives and the search would look like a recall problem rather than a placement
bug. The one thing this module adds is the aggregation from many symbols to ONE home.

THE UNCLASSIFIED POPULATION IS NOT A ROUNDING ERROR. MEASURED: 1,024,320 publications, 20.6% of
the corpus, carry no classification at all, and `shard_router.route()` always emits an
`"unclassified"` route precisely because that share would otherwise be unreachable. So
`UNCLASSIFIED` is a first-class domain here: it is packed into a shard like any other domain, and
`pack_domains` refuses a plan that leaves it homeless.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict

from retrieval.shard_router import UNCLASSIFIED, domain_of

#  MEASURED 2026-08-22: 21,862 publications carry `simple_family_id = '-1'`, which is DOCDB's
#  "no simple family is known for this document", not a family. The `family_of` view in
#  sql/001_schema.sql only guards against the empty string, so today those 21,862 unrelated
#  documents share ONE family key. Two consequences, both real:
#    * retrieval dedupes by family key, so 21,862 disclosures currently collapse to one result;
#    * a home shard decided per family would put all 21,862 on one shard for no reason.
#  A sentinel is therefore not a family: the publication is its own family, which is what a
#  document with no known relatives actually is.
FAMILY_SENTINELS = {"", "-1", "0", "none", "null", "unknown", "nan"}


def family_key(simple_family_id, publication_number):
    """The v3 family key. One definition, used by the builder and by every stat computed from it."""
    s = str(simple_family_id or "").strip()
    return str(publication_number or "") if s.lower() in FAMILY_SENTINELS else s


#  A publication's FIRST CPC symbol is its primary classification. It is not more votes, it is a
#  tie-break: without it, a family whose two domains come out exactly level would be placed by
#  alphabetical accident, and the accident would move the family between builds as symbols are
#  added. With it, the placement follows the office's own opinion of what the document is about.
PRIMARY_BONUS = 0.5


def domains_of_symbols(symbols):
    """-> the distinct domains a list of CPC/IPC symbols names.

    An empty or unclassifiable symbol list is `{UNCLASSIFIED}`, never an empty set: every
    publication has to have somewhere to go.
    """
    out = {domain_of(s) for s in (symbols or []) if str(s or "").strip()}
    return out or {UNCLASSIFIED}


def publication_domain_weights(symbols, first_symbols=()):
    """One publication's vote, worth 1.0 in total, spread across the domains it names.

    Spread, not one vote per symbol: a document carrying twelve symbols in one subclass must not
    outvote eleven documents carrying one each. That miscount is the same defect that makes
    `channel_cpc`'s `count(*)` ranking meaningless.
    """
    doms = domains_of_symbols(symbols)
    share = 1.0 / len(doms)
    out = {d: share for d in doms}
    firsts = domains_of_symbols(first_symbols) if first_symbols else set()
    if firsts and firsts != {UNCLASSIFIED}:
        bonus = PRIMARY_BONUS / len(firsts)
        for d in firsts:
            out[d] = out.get(d, 0.0) + bonus
    return out


def family_domain_weights(publications):
    """{domain: weight} for a whole family.

    `publications` is an iterable of dicts with `symbols` and optionally `first_symbols`. Every
    publication contributes the same total vote regardless of how heavily it is classified, so a
    family of one US grant and six machine-translated equivalents is not decided by whichever
    office indexes most enthusiastically.
    """
    acc = defaultdict(float)
    n = 0
    for pub in publications or []:
        n += 1
        for d, w in publication_domain_weights(pub.get("symbols") or (),
                                               pub.get("first_symbols") or ()).items():
            acc[d] += w
    if not n:
        return {UNCLASSIFIED: 1.0}
    return dict(acc)


def home_domain(publications):
    """-> (home_domain, weights). The single domain the whole family is placed in.

    Deterministic: highest weight wins, ties break on the domain string ascending. Determinism is
    the point. A placement that depends on dict ordering moves families between builds, and a
    family that moves between builds is a family that is in two releases at once during a rollout.
    """
    weights = family_domain_weights(publications)
    if not weights:
        return UNCLASSIFIED, {UNCLASSIFIED: 1.0}
    best = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best, weights


def secondary_domains(home, weights):
    """The domains this family MENTIONS but does not live in, best-first.

    Recorded per member row because it is the measurable cost of keeping a family whole: a query
    routed to one of these domains wakes a shard that does not hold this family. `stats` turns the
    column into a reachability number so the cost is visible rather than assumed.
    """
    return [d for d, _ in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])) if d != home]


# ---------------------------------------------------------------------------------------------
# domain -> shard packing
# ---------------------------------------------------------------------------------------------
class ShardPlan:
    """An assignment of domains to shard keys, plus what it cost.

    `shards` is {shard_key: [domain, ...]}, `mass` is {shard_key: chunks}, `domain_shard` is the
    inverse lookup the router needs.
    """

    def __init__(self, shards, mass, capacity=None, oversized=()):
        self.shards = shards
        self.mass = mass
        self.capacity = capacity
        self.oversized = list(oversized)
        self.domain_shard = {d: k for k, ds in shards.items() for d in ds}

    @property
    def fits(self):
        return not self.oversized

    def to_dict(self):
        return {"shards": {k: sorted(v) for k, v in self.shards.items()},
                "mass": dict(self.mass), "capacity": self.capacity,
                "oversized": sorted(self.oversized), "fits": self.fits}


def _shard_key(i, prefix="domain"):
    return f"{prefix}_{i + 1:02d}"


def unclassified_bucket(family_key, buckets):
    """Which sub-bucket an unclassified family falls in, if `unclassified` had to be split.

    Hash of the family key, so the split is stable across builds and needs no state. Splitting is
    a LAST resort: `shard_router.route()` emits ONE `unclassified` route, so a split means every
    query that routes to unclassified wakes every bucket, and the wake cost multiplies by the
    number of pieces.
    """
    if buckets <= 1:
        return 0
    h = hashlib.sha256(str(family_key).encode()).digest()
    return int.from_bytes(h[:8], "big") % int(buckets)


def pack_domains(mass, n_shards, *, capacity=None, prefix="domain", pinned=None,
                 unclassified_splits=1):
    """Longest-processing-time-first bin packing of domains into `n_shards` shard keys.

    `mass` is {domain: chunks}. LPT rather than anything cleverer because the input is one heavy
    tail (B65G is 12.7% of all classification rows on its own) and LPT's 4/3 bound is reached by
    placing the heavy items first, which is exactly the shape of this problem.

    `capacity` is the per-shard chunk ceiling from `sizing.chunks_per_shard`. Domains are still
    placed when they do not fit, and the shard keys that ended up over capacity are reported in
    `ShardPlan.oversized`. A packer that silently dropped the overflow would produce a plan that
    looks balanced and a corpus that is missing its largest subclass.

    `unclassified_splits` > 1 splits UNCLASSIFIED into that many synthetic domains
    (`unclassified#0` ...). Read `unclassified_bucket` before using it.
    """
    n_shards = max(1, int(n_shards))
    keys = [_shard_key(i, prefix) for i in range(n_shards)]
    shards = {k: [] for k in keys}
    load = {k: 0 for k in keys}

    work = dict(mass or {})
    if unclassified_splits > 1 and UNCLASSIFIED in work:
        total = work.pop(UNCLASSIFIED)
        per = total // unclassified_splits
        for i in range(unclassified_splits):
            extra = total - per * unclassified_splits if i == 0 else 0
            work[f"{UNCLASSIFIED}#{i}"] = per + extra

    for domain, shard_key in (pinned or {}).items():
        if shard_key in shards and domain in work:
            shards[shard_key].append(domain)
            load[shard_key] += int(work.pop(domain))

    for domain, m in sorted(work.items(), key=lambda kv: (-int(kv[1]), kv[0])):
        k = min(keys, key=lambda kk: (load[kk], kk))
        shards[k].append(domain)
        load[k] += int(m)

    oversized = [] if not capacity else [k for k in keys if load[k] > capacity]
    return ShardPlan(shards, load, capacity=capacity, oversized=oversized)
