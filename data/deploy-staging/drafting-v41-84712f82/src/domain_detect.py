"""Out-of-domain detection for the pilot corpus (8 seed CPC branches, vacuum gripping).

WHY THIS EXISTS
---------------
The local index covers only vacuum-gripping/suction-handling art. A query from any other
field still returns a confidently-ranked top-25 — the dense channel always has a nearest
neighbour, and RRF always produces an ordering. For a prior-art tool that is worse than
returning nothing: the user cannot tell "these are the 25 closest documents in a corpus
that does not contain your field" from "these are the 25 most relevant documents".

So every search gets a calibrated in-domain signal, and out-of-domain queries are either
refused or routed to the federated sibling app (see federation.py).

SIGNALS (measured, not guessed — see calibrate() and the numbers in the module docstring
of tests/test_domain_detect.py)
  cos_top10  mean cosine of the 10 best-matching publications' best chunk.
             The single strongest continuous signal. IN min 0.782 vs OOD max 0.771.
  cpc_seed   fraction of the top-30 dense publications carrying at least one classification
             inside the 8 seed CPC branches. IN mean 0.945 vs OOD mean 0.244.
  centroid   cosine of the query to the corpus centroid. MEASURED AND REJECTED: it separates
             (IN 0.708 vs OOD 0.609 on the mean) but its worst-case margin is thinner than
             cos_top10's (IN min 0.661 vs OOD max 0.658) while additionally requiring a
             stored/refreshed centroid vector. Dominated — computed only as a diagnostic
             when explicitly requested, never part of the score.

Neither signal is sufficient alone. cos_top10 alone misclassifies agricultural-robot queries
(0.771, just under the in-domain floor); cpc_seed alone misclassifies a 5G query, which scored
cpc_seed=1.00 because its nearest neighbours happened to be sparsely classified — but that same
query has the lowest cos_top10 in the whole set (0.634). The two signals fail on disjoint cases,
which is exactly why the weighted combination separates the labelled set perfectly.

An LLM tiebreak (gemini-2.5-flash) arbitrates only the narrow uncertain band, so the common
case costs one embedding + two SQL queries and no LLM call.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field

from config import SEED_CPC, SEED_CPC_TITLES

# --- calibrated constants -------------------------------------------------------------------
# Normalisation band for cos_top10. Below COS_LO the query is semantically unrelated to
# anything in the corpus; above COS_HI it is as close as a genuine in-field document.
COS_LO = 0.70
COS_HI = 0.85

W_COS = 0.60          # cos_top10 carries most of the signal
W_CPC = 0.40          # seed-CPC hit fraction is the corrective that catches the cosine's misses

DECIDE = 0.59         # score >= DECIDE -> in-domain. Midpoint of the observed separation gap
                      # (worst in-domain 0.628, best out-of-domain 0.552).
BAND_LO = 0.50        # inside [BAND_LO, BAND_HI] the score alone is not trusted and the LLM
BAND_HI = 0.66        # tiebreak decides. Outside the band the verdict is high-confidence.

PROBE_CHUNKS = 200    # chunks pulled for the probe (cheap; ~top-60 distinct publications)
PROBE_PUBS = 30       # publications examined for the CPC fraction
COS_K = 10            # publications averaged for cos_top10

USE_LLM = os.environ.get("DOMAIN_DETECT_LLM", "1") == "1"

SEED_SUBCLASSES = sorted({c[:4] for c in SEED_CPC})
DOMAIN_DESCRIPTION = (
    "vacuum / suction gripping and lifting: suction lifting devices, handheld vacuum "
    "lifters, robotic vacuum grippers and end-of-arm suction tooling, suction transfer "
    "and sheet-handling devices, vacuum work holders, and suction cups"
)


@dataclass
class DomainVerdict:
    """Calibrated in-domain signal. Never just a boolean — the caller needs the score and the
    reason to decide between 'answer locally', 'federate', and 'refuse'."""
    in_domain: bool
    score: float                  # 0..1, calibrated; DECIDE is the boundary
    confidence: str               # "high" (outside the uncertain band) | "low" (inside it)
    reason: str                   # human-readable, safe to show a user
    signals: dict = field(default_factory=dict)
    llm_used: bool = False

    @property
    def should_federate(self) -> bool:
        """Out-of-domain, or in-domain but only marginally — both are cases where the local
        corpus alone is not a defensible answer."""
        return (not self.in_domain) or self.confidence == "low"

    def to_dict(self) -> dict:
        return {
            "in_domain": self.in_domain,
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "reason": self.reason,
            "signals": {k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in self.signals.items()},
            "llm_used": self.llm_used,
            "should_federate": self.should_federate,
        }


def _norm_cos(c: float) -> float:
    if c <= COS_LO:
        return 0.0
    if c >= COS_HI:
        return 1.0
    return (c - COS_LO) / (COS_HI - COS_LO)


def _vec(e) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


# --- signal extraction ----------------------------------------------------------------------
def probe_signals(query: str, conn=None, retriever=None) -> dict:
    """Run the dense probe and return the raw signals. Cheap: one embedding + two SQL queries.

    Pass an existing `retriever` (or `conn`) to reuse the pooled connection; otherwise a
    short-lived connection is opened and closed.
    """
    import embed

    own = False
    if retriever is not None:
        conn = retriever.conn
    if conn is None:
        import db
        conn = db.connect()
        conn.autocommit = True
        own = True
    try:
        qv = embed.embed_query(query[:8000], 768)
        v = _vec(qv)
        with conn.cursor() as c:
            c.execute(
                "SELECT c.publication_id, 1-(c.embedding <=> %s::vector) AS s "
                "FROM chunks c WHERE c.embedding IS NOT NULL "
                "ORDER BY c.embedding <=> %s::vector LIMIT %s",
                (v, v, PROBE_CHUNKS))
            rows = c.fetchall()

        best, order = {}, []
        for r in rows:
            pid = r["publication_id"]
            if pid not in best:              # rows already ordered best-first
                best[pid] = float(r["s"])
                order.append(pid)
        if not order:
            return {"cos_top1": 0.0, "cos_top10": 0.0, "cpc_seed": 0.0,
                    "n_pubs": 0, "empty": True}

        sims = [best[p] for p in order]
        top = order[:PROBE_PUBS]
        with conn.cursor() as c:
            like = " OR ".join(["cl.symbol LIKE %s"] * len(SEED_CPC))
            c.execute(
                f"SELECT count(DISTINCT cl.publication_id) AS n FROM classifications cl "
                f"WHERE cl.publication_id = ANY(%s) AND ({like})",
                [top] + [s + "%" for s in SEED_CPC])
            n_seed = int(c.fetchone()["n"])

        return {
            "cos_top1": sims[0],
            "cos_top10": statistics.mean(sims[:COS_K]),
            "cpc_seed": n_seed / len(top),
            "n_pubs": len(order),
            "empty": False,
        }
    finally:
        if own:
            conn.close()


def score_signals(sig: dict) -> float:
    """Weighted combination of the two calibrated signals -> 0..1."""
    if sig.get("empty"):
        return 0.0
    return W_COS * _norm_cos(sig["cos_top10"]) + W_CPC * min(1.0, sig["cpc_seed"])


# --- LLM tiebreak ---------------------------------------------------------------------------
def _llm_tiebreak(query: str) -> tuple[bool | None, str]:
    """Ask gemini-2.5-flash whether the query belongs to the corpus's field.
    Returns (verdict|None, rationale). None means the call failed — the caller falls back
    to the numeric score, so an LLM outage degrades to score-only, never to an exception."""
    try:
        import llm
    except Exception:
        return None, "llm unavailable"
    titles = "; ".join(f"{k} ({v})" for k, v in SEED_CPC_TITLES.items())
    system = (
        "You classify whether a patent search query falls inside a narrow technical corpus.\n"
        f"The corpus covers ONLY this field: {DOMAIN_DESCRIPTION}.\n"
        f"Its CPC branches are: {titles}.\n"
        "A query is in_domain ONLY if prior art for it would plausibly sit in those branches. "
        "General material-handling, robotics or conveying that does NOT involve vacuum/suction "
        "gripping is OUT of domain. "
        'Return JSON {"in_domain": true|false, "why": "<12 words>"}.')
    out = llm.chat_json(system, query[:1500], max_tokens=200) or {}
    if "in_domain" not in out:
        return None, "llm returned no verdict"
    return bool(out["in_domain"]), str(out.get("why", ""))[:120]


# --- public API -----------------------------------------------------------------------------
def detect(query: str, conn=None, retriever=None, use_llm: bool | None = None) -> DomainVerdict:
    """Decide whether the local corpus can plausibly answer `query`.

    Returns a DomainVerdict with a calibrated score, not a bare boolean. Callers should branch
    on `.should_federate` (out-of-domain OR low-confidence) rather than on `.in_domain` alone.
    """
    if not query or not query.strip():
        return DomainVerdict(False, 0.0, "high", "empty query", {}, False)

    use_llm = USE_LLM if use_llm is None else use_llm
    sig = probe_signals(query, conn=conn, retriever=retriever)
    if sig.get("empty"):
        return DomainVerdict(False, 0.0, "high",
                             "the local index returned no embedded content", sig, False)

    score = score_signals(sig)
    in_band = BAND_LO <= score <= BAND_HI
    verdict = score >= DECIDE
    llm_used = False
    why = ""

    if in_band and use_llm:
        llm_v, why = _llm_tiebreak(query)
        if llm_v is not None:
            verdict, llm_used = llm_v, True

    confidence = "low" if in_band and not llm_used else "high"
    pct = int(round(sig["cpc_seed"] * 100))
    if verdict:
        reason = (f"in-domain: nearest local art matches strongly "
                  f"(similarity {sig['cos_top10']:.2f}) and {pct}% of the top hits are "
                  f"classified in the corpus's own CPC branches")
    else:
        reason = (f"OUT OF DOMAIN: the local corpus covers only {DOMAIN_DESCRIPTION}. "
                  f"Best local similarity is only {sig['cos_top10']:.2f} and just {pct}% of "
                  f"the nearest documents are classified in the corpus's CPC branches — "
                  f"local results would be the closest available art, not relevant art")
    if llm_used and why:
        reason += f" [classifier: {why}]"

    return DomainVerdict(verdict, score, confidence, reason, sig, llm_used)


# --- calibration harness --------------------------------------------------------------------
# The labelled set. In-domain queries are the 11 frozen gold queries (loaded from goldset.json
# when present, else the 3 natural-language ones that are inlined here). Out-of-domain queries
# are written by hand across clearly different fields. "Adjacent" queries are deliberately
# hard negatives: material handling that is NOT vacuum gripping.
OOD_QUERIES = [
    ("pharma_kinase", "small molecule JAK2 kinase inhibitor for treating myelofibrosis, pharmaceutical composition with a pharmaceutically acceptable carrier"),
    ("pharma_antibody", "monoclonal antibody that binds PD-L1 and its use in treating non-small-cell lung cancer"),
    ("semi_euv", "extreme ultraviolet lithography apparatus with a tin droplet plasma source and multilayer mirror collector optics"),
    ("semi_finfet", "method of fabricating a FinFET transistor with a high-k metal gate and self-aligned contacts"),
    ("ml_transformer", "neural network training using a transformer architecture with multi-head self-attention and rotary positional embeddings"),
    ("ml_federated", "federated learning system that aggregates model gradients from edge devices with differential privacy noise"),
    ("battery_solidstate", "solid-state lithium metal battery with a sulfide solid electrolyte separator and a lithium anode"),
    ("telecom_5g", "5G NR beamforming with channel state information feedback and massive MIMO antenna arrays"),
    ("agri_harvest", "autonomous strawberry harvesting robot using stereo vision to identify ripe fruit"),
    ("food_brewing", "method of brewing beer using a continuous fermentation vessel with immobilized yeast"),
    ("finance_blockchain", "distributed ledger consensus mechanism using proof of stake with validator slashing"),
    ("med_stent", "drug-eluting coronary stent with a biodegradable polymer coating releasing sirolimus"),
    ("auto_engine", "turbocharged internal combustion engine with variable valve timing and cylinder deactivation"),
    ("textile_weave", "loom for weaving three-dimensional carbon fibre preforms for composite aerospace parts"),
    ("cosmetic_uv", "sunscreen composition comprising zinc oxide nanoparticles and a photostable UVA filter"),
    ("adj_magnetic_lifter", "permanent magnetic lifter for handling steel plates in a workshop"),
    ("adj_conveyor", "belt conveyor system for transporting cardboard boxes in a warehouse"),
]

INLINE_IN_DOMAIN = [
    ("nl_handheld_vacuum_seal_sensor",
     "handheld battery-powered vacuum lifter with a flexible sealing lip, an electric vacuum "
     "pump, and a pressure sensor that warns the operator when grip vacuum is lost"),
    ("nl_porous_surface_gripper",
     "vacuum suction gripper able to lift rough or porous surfaces such as concrete, stone, or "
     "textured tile by maintaining airflow with a continuously running pump"),
    ("nl_robot_eoat_vacuum",
     "robotic end-of-arm vacuum gripper for handling sheets or panels with an array of suction "
     "cups and independent vacuum zones"),
]


def labelled_set() -> list[tuple[str, str, bool]]:
    """-> [(id, query_text, is_in_domain)]"""
    import json
    from config import DATA

    items: list[tuple[str, str, bool]] = []
    gs = DATA / "goldset" / "goldset.json"
    if gs.exists():
        try:
            for e in json.loads(gs.read_text())["entries"]:
                q = (e.get("query_text") or "").strip()
                if q:
                    items.append((e["id"], q[:1500], True))
        except Exception:
            pass
    if not items:
        items = [(i, q, True) for i, q in INLINE_IN_DOMAIN]
    items += [(i, q, False) for i, q in OOD_QUERIES]
    return items


def calibrate(use_llm: bool | None = None, verbose: bool = True) -> dict:
    """Run the detector over the labelled set and report real accuracy."""
    rows, tp = [], 0
    fp = fn = 0
    for qid, q, truth in labelled_set():
        v = detect(q, use_llm=use_llm)
        ok = v.in_domain == truth
        if ok and truth:
            tp += 1
        if not ok and v.in_domain:
            fp += 1
        if not ok and not v.in_domain:
            fn += 1
        rows.append((qid, truth, v))
        if verbose:
            mark = "ok " if ok else "MISS"
            print(f"{mark} {qid:34} truth={'IN ' if truth else 'OOD'} "
                  f"pred={'IN ' if v.in_domain else 'OOD'} score={v.score:.3f} "
                  f"cos10={v.signals.get('cos_top10', 0):.3f} "
                  f"cpc={v.signals.get('cpc_seed', 0):.2f} "
                  f"conf={v.confidence}{' llm' if v.llm_used else ''}")
    n = len(rows)
    correct = sum(1 for _, t, v in rows if v.in_domain == t)
    n_in = sum(1 for _, t, _ in rows if t)
    n_out = n - n_in
    out = {
        "n": n, "n_in_domain": n_in, "n_out_of_domain": n_out,
        "accuracy": correct / n if n else 0.0,
        "false_positives": fp,   # OOD query wrongly accepted as in-domain (the dangerous one)
        "false_negatives": fn,   # in-domain query wrongly rejected
        "recall_in_domain": tp / n_in if n_in else 0.0,
        "specificity": (n_out - fp) / n_out if n_out else 0.0,
        "llm_calls": sum(1 for _, _, v in rows if v.llm_used),
    }
    if verbose:
        print("\n" + repr(out))
    return out


if __name__ == "__main__":
    calibrate()
