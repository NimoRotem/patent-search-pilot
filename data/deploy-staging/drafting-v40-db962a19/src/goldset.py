"""Frozen evaluation gold set — BUILT BEFORE THE INDEX (spec §8, build-order step 4).

Relevance judgments are grounded, not guessed: each anchor patent's examiner / search-report
citations (resolved to DOCDB families) are the gold-relevant set (CLEF-IP methodology), plus
curated real-world competitor families. Direct citation edges are recorded so retrieval can
HIDE them during tests (no leakage).

Run once; output data/goldset/goldset.json is then treated as frozen.
"""
from __future__ import annotations
import json, sys
from datetime import date, datetime
from pathlib import Path
import bqclient
from config import DATA

OUT = DATA / "goldset"
OUT.mkdir(parents=True, exist_ok=True)

# --- Curated anchors (real publication numbers from the seed field) -------------------------
# category ∈ grabo_own | competitor | cross_lingual | hard_combination | neighbouring_cpc
# mode ∈ novelty | inventive_step. extra_gold_families = hand-known relevant competitor families.
GRABO_FAMS = ["66624664", "83194486"]
SCHMALZ_FAMS = ["63449883", "70050062", "34201690"]
PROBST_FAMS = ["6495403", "6480115", "7782789", "7889498"]

ANCHORS = [
    dict(id="grabo_gripper_novelty", pub="US-11999030-B2", category="grabo_own",
         mode="novelty", notes="GRABO core portable vacuum gripper (fam 66624664).",
         extra_gold_families=SCHMALZ_FAMS + PROBST_FAMS),
    dict(id="grabo_gripper_inventive", pub="US-11999030-B2", category="grabo_own",
         mode="inventive_step", notes="Same anchor, inventive-step mode (public art only).",
         extra_gold_families=SCHMALZ_FAMS + PROBST_FAMS),
    dict(id="grabo_extended_frame", pub="US-11731291-B2", category="grabo_own",
         mode="novelty", notes="GRABO extended-frame portable vacuum gripper (fam 83194486).",
         extra_gold_families=SCHMALZ_FAMS),
    dict(id="grabo_de_utility_xling", pub="DE-202019005606-U1", category="cross_lingual",
         mode="novelty", notes="GRABO DE utility model — German-text anchor (cross-lingual recall test).",
         extra_gold_families=SCHMALZ_FAMS + PROBST_FAMS),
    dict(id="schmalz_sauggreifsystem", pub="DE-102017106252-A1", category="competitor",
         mode="novelty", notes="Schmalz suction-gripper system (fam 63449883); DE anchor.",
         extra_gold_families=GRABO_FAMS),
    dict(id="schmalz_vacuum_clamp", pub="DE-102019107477-A1", category="competitor",
         mode="inventive_step", notes="Schmalz console/vacuum clamping device (fam 70050062).",
         extra_gold_families=[]),
    dict(id="probst_stone_lifter_xling", pub="DE-4327663-A1", category="cross_lingual",
         mode="novelty", notes="Probst vacuum lifting device for stone elements (fam 6495403); DE.",
         extra_gold_families=GRABO_FAMS + ["6480115"]),
    dict(id="probst_kerb_lifter", pub="DE-4303944-A1", category="competitor",
         mode="inventive_step", notes="Probst kerbstone/paving lifting appliance (fam 6480115).",
         extra_gold_families=["6495403"]),
    # Natural-language feature-combination queries (curated gold families; deliberately hard).
    dict(id="nl_handheld_vacuum_seal_sensor", pub=None, category="hard_combination",
         mode="novelty",
         nl_query=("handheld battery-powered vacuum lifter with a flexible sealing lip, an "
                   "electric vacuum pump, and a pressure sensor that warns the operator when "
                   "grip vacuum is lost"),
         notes="Feature combination: portability + seal + active pump + loss-of-vacuum sensor.",
         extra_gold_families=GRABO_FAMS + SCHMALZ_FAMS),
    dict(id="nl_porous_surface_gripper", pub=None, category="hard_combination",
         mode="novelty",
         nl_query=("vacuum suction gripper able to lift rough or porous surfaces such as "
                   "concrete, stone, or textured tile by maintaining airflow with a continuously "
                   "running pump"),
         notes="Hard: porous/rough-surface handling — a differentiator of the field.",
         extra_gold_families=GRABO_FAMS + PROBST_FAMS),
    dict(id="nl_robot_eoat_vacuum", pub=None, category="neighbouring_cpc",
         mode="inventive_step",
         nl_query=("robotic end-of-arm vacuum gripper for handling sheets or panels with an "
                   "array of suction cups and independent vacuum zones"),
         notes="Neighbouring CPC: B25J15/0616 robotic grippers vs handheld B66C1/0225.",
         extra_gold_families=SCHMALZ_FAMS),
]

# Anchors read from the cheap CORE staging table (already clustered by publication_number).
ANCHOR_SQL = """
SELECT
  publication_number, country_code, kind_code, publication_date, filing_date, priority_date,
  family_id, title_en AS title, abstract_en AS abstract, claims_en AS claims_text, cites
FROM `nimo-gpt.patent_pilot.core`
WHERE publication_number IN UNNEST(@pubs)
"""

# Cited docs may be off-corpus -> resolve family via a small-column lookup on the full table.
RESOLVE_CITE_FAM_SQL = """
SELECT publication_number, CAST(family_id AS STRING) AS family_id, country_code
FROM `patents-public-data.patents.publications`
WHERE publication_number IN UNNEST(@pubs)
"""


def _q(sql, **params):
    from google.cloud import bigquery
    qp = []
    for k, v in params.items():
        if isinstance(v, list):
            qp.append(bigquery.ArrayQueryParameter(k, "STRING", v))
        else:
            qp.append(bigquery.ScalarQueryParameter(k, "STRING", v))
    cfg = bigquery.QueryJobConfig(query_parameters=qp, maximum_bytes_billed=int(300e9))
    return [dict(r) for r in bqclient.client().query(sql, job_config=cfg).result()]


def first_claim(claims_text: str) -> str:
    if not claims_text:
        return ""
    # claims_localized is one blob; claim 1 runs until "2." at a line start.
    import re
    m = re.split(r"\n\s*2\s*[\.\)]", claims_text, maxsplit=1)
    c1 = m[0].strip()
    return c1[:1500]


def build():
    pubs = [a["pub"] for a in ANCHORS if a.get("pub")]
    anchor_rows = {r["publication_number"]: r for r in _q(ANCHOR_SQL, pubs=pubs)}
    print(f"[goldset] fetched {len(anchor_rows)}/{len(pubs)} anchor pubs")

    # resolve all cited publication_numbers -> family
    all_cited = set()
    for r in anchor_rows.values():
        for c in r["cites"]:
            all_cited.add(c["pub"])
    cite_fam = {}
    cited_list = list(all_cited)
    for i in range(0, len(cited_list), 5000):
        for row in _q(RESOLVE_CITE_FAM_SQL, pubs=cited_list[i:i+5000]):
            cite_fam[row["publication_number"]] = row
    print(f"[goldset] resolved families for {len(cite_fam)}/{len(all_cited)} cited pubs")

    entries = []
    for a in ANCHORS:
        gold_fams = set(a.get("extra_gold_families", []))
        hidden_edges = []
        subj = None
        query_text = a.get("nl_query", "")
        anchor_fam = None
        title = None
        if a.get("pub"):
            r = anchor_rows.get(a["pub"])
            if not r:
                print(f"[goldset] WARN anchor {a['pub']} not found; skipping", file=sys.stderr)
                continue
            anchor_fam = str(r["family_id"])
            title = r["title"]
            # relevance = families of cited docs (X/Y/A), excluding anchor's own family
            for c in r["cites"]:
                fam = cite_fam.get(c["pub"], {}).get("family_id")
                if fam and str(fam) != anchor_fam:
                    gold_fams.add(str(fam))
                hidden_edges.append({"src": r["publication_number"], "dst": c["pub"],
                                     "category": c.get("category"), "type": c.get("type")})
            # query-by-example text: title + abstract + claim 1 (own text)
            c1 = first_claim(r.get("claims_text") or "")
            parts = [p for p in [title, r.get("abstract"), (f"Claim 1: {c1}" if c1 else "")] if p]
            query_text = "\n".join(parts)
            subj = dict(
                number=r["publication_number"],
                efd=str(r["priority_date"] or r["filing_date"] or r["publication_date"]),
                filing_date=str(r["filing_date"]) if r["filing_date"] else None,
                publication_date=str(r["publication_date"]) if r["publication_date"] else None,
                jurisdiction=r["country_code"],
                has_claims=bool(c1),
            )
        entries.append(dict(
            id=a["id"], category=a["category"], mode=a["mode"],
            anchor_publication=a.get("pub"), anchor_family=anchor_fam,
            title=title, notes=a["notes"],
            query_text=query_text,
            subject=subj,
            gold_families=sorted(gold_fams),
            n_gold_families=len(gold_fams),
            hidden_edges=hidden_edges,
        ))

    frozen = dict(
        _meta=dict(
            generated=date.today().isoformat(),
            methodology=("Relevance = DOCDB families of each anchor's examiner/search-report "
                         "citations + curated competitor families. Citation edges hidden at "
                         "retrieval time. Frozen — do not edit after retrieval tuning begins."),
            n_entries=len(entries),
            frozen=True,
        ),
        entries=entries,
    )
    (OUT / "goldset.json").write_text(json.dumps(frozen, indent=2, default=str))

    # human summary
    md = ["# Frozen Evaluation Gold Set", f"_Generated {date.today()} · {len(entries)} searches_", ""]
    md += ["| id | category | mode | anchor | #gold families | #hidden edges |",
           "|---|---|---|---|--:|--:|"]
    for e in entries:
        md.append(f"| `{e['id']}` | {e['category']} | {e['mode']} | {e['anchor_publication'] or 'NL query'} "
                  f"| {e['n_gold_families']} | {len(e['hidden_edges'])} |")
    cats = {}
    for e in entries:
        cats[e["category"]] = cats.get(e["category"], 0) + 1
    md += ["", "## Category coverage (spec §8 requires these)", ""]
    md += [f"- {k}: {v}" for k, v in sorted(cats.items())]
    (OUT / "goldset.md").write_text("\n".join(md) + "\n")
    print(f"[goldset] wrote {len(entries)} entries -> {OUT/'goldset.json'}")
    for e in entries:
        print(f"  {e['id']:32s} {e['category']:16s} {e['mode']:14s} gold_fams={e['n_gold_families']:3d} hidden={len(e['hidden_edges'])}")


def load():
    return json.loads((OUT / "goldset.json").read_text())


if __name__ == "__main__":
    build()
