"""Profile coverage FIRST (spec §2.1, build-order step 3).

Per jurisdiction, how many seed-CPC docs have title/abstract/claims/description present in
BigQuery. Reveals where BigQuery has holes (expect: worldwide biblio + US full text strong;
EP/DE/WO full text weak) and justifies the EPO/USPTO enrichment step. Saves CSV + markdown.
"""
from __future__ import annotations
import csv, json, sys
from datetime import date
from pathlib import Path
import bqclient
from config import DATA

OUT = DATA / "coverage"
OUT.mkdir(parents=True, exist_ok=True)

COVERAGE_SQL = f"""
WITH matched AS (
  SELECT
    publication_number,
    CASE WHEN country_code IN ('US','EP','WO','DE') THEN country_code ELSE 'OTHER' END AS juris,
    (SELECT LOGICAL_OR(t.text IS NOT NULL AND LENGTH(t.text) > 0) FROM UNNEST(title_localized) t)       AS has_title,
    (SELECT LOGICAL_OR(a.text IS NOT NULL AND LENGTH(a.text) > 0) FROM UNNEST(abstract_localized) a)    AS has_abstract,
    (SELECT LOGICAL_OR(c.text IS NOT NULL AND LENGTH(c.text) > 0) FROM UNNEST(claims_localized) c)       AS has_claims,
    (SELECT LOGICAL_OR(d.text IS NOT NULL AND LENGTH(d.text) > 0) FROM UNNEST(description_localized) d)  AS has_desc,
    (SELECT STRING_AGG(DISTINCT gl.language) FROM (
        SELECT language FROM UNNEST(abstract_localized) UNION ALL
        SELECT language FROM UNNEST(claims_localized)) gl)                                               AS langs
  FROM `patents-public-data.patents.publications`
  WHERE country_code IN ('US','EP','WO','DE')
    AND EXISTS (SELECT 1 FROM UNNEST(cpc) c WHERE {bqclient.cpc_like_clause()})
)
SELECT
  juris,
  COUNT(*)                    AS n_pubs,
  COUNTIF(has_title)          AS with_title,
  COUNTIF(has_abstract)       AS with_abstract,
  COUNTIF(has_claims)         AS with_claims,
  COUNTIF(has_desc)           AS with_description
FROM matched
GROUP BY juris
ORDER BY juris
"""

# Per-CPC counts (classification-expansion sanity — the seed classes cross-reference).
PERCPC_SQL = f"""
SELECT c.code AS cpc, COUNT(DISTINCT publication_number) AS n
FROM `patents-public-data.patents.publications`, UNNEST(cpc) c
WHERE country_code IN ('US','EP','WO','DE')
  AND ({bqclient.cpc_like_clause()})
GROUP BY cpc
ORDER BY n DESC
LIMIT 40
"""


def pct(a, b):
    return f"{100.0*a/b:.1f}%" if b else "n/a"


def main():
    est = bqclient.dry_run_gb(COVERAGE_SQL)
    print(f"[coverage] dry-run estimate: {est:.1f} GB (${est/1000*6.25:.2f} on-demand)")
    if est > 2500:
        print("[coverage] ABORT: estimate exceeds 2.5 TB safety cap.", file=sys.stderr)
        sys.exit(2)

    rows = bqclient.run(COVERAGE_SQL, max_gb_billed=max(50.0, est * 1.5))
    # persist raw CSV
    with open(OUT / "coverage_by_jurisdiction.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)

    percpc = bqclient.run(PERCPC_SQL, max_gb_billed=100.0)
    with open(OUT / "coverage_by_cpc.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=percpc[0].keys())
        w.writeheader(); w.writerows(percpc)

    # markdown report
    total = sum(r["n_pubs"] for r in rows)
    lines = [
        "# BigQuery Coverage Profile — seed CPC (vacuum gripping)",
        f"_Generated {date.today().isoformat()} · source `patents-public-data.patents.publications`_",
        "",
        f"**Total seed-CPC publications (US/EP/WO/DE, all dates): {total:,}**",
        "",
        "## Full-text field presence by jurisdiction",
        "",
        "| Juris | Pubs | Title | Abstract | Claims | Description |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for r in rows:
        n = r["n_pubs"]
        lines.append(
            f"| {r['juris']} | {n:,} | {pct(r['with_title'],n)} | {pct(r['with_abstract'],n)} "
            f"| {pct(r['with_claims'],n)} | {pct(r['with_description'],n)} |"
        )
    # auto-narrative: flag full-text holes
    holes = []
    for r in rows:
        n = r["n_pubs"] or 1
        if r["with_claims"] / n < 0.6:
            holes.append(f"- **{r['juris']}**: claims present for only {pct(r['with_claims'],n)} → enrich from EPO OPS / USPTO ODP.")
        if r["with_description"] / n < 0.5:
            holes.append(f"- **{r['juris']}**: description present for only {pct(r['with_description'],n)} → BigQuery lacks full text; enrich the CORE set from official feeds.")
    lines += ["", "## Holes → enrichment plan (spec §2.3)", ""]
    lines += holes or ["- No major full-text holes detected at the biblio+claims level."]
    lines += ["", "## Top CPC subclasses in the seed field", "", "| CPC | Pubs |", "|---|--:|"]
    for r in percpc[:20]:
        lines.append(f"| `{r['cpc']}` | {r['n']:,} |")
    (OUT / "coverage_report.md").write_text("\n".join(lines) + "\n")

    print("[coverage] wrote:", OUT / "coverage_report.md")
    for r in rows:
        n = r["n_pubs"]
        print(f"  {r['juris']}: {n:,} pubs | claims {pct(r['with_claims'],n)} | desc {pct(r['with_description'],n)}")


if __name__ == "__main__":
    main()
