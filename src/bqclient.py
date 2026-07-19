"""BigQuery access (bootstrap source only — spec §2). Uses the GCE service-account creds."""
from __future__ import annotations
import os
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
from google.cloud import bigquery
from config import GCP_PROJECT, SEED_CPC

_client = None

# On-demand BigQuery analysis price (US multi-region), $ per TB scanned. Used only to turn
# dry-run byte estimates into a human-readable figure in the logs.
USD_PER_TB = float(os.environ.get("BQ_USD_PER_TB", "6.25"))


class CostCeilingExceeded(RuntimeError):
    """Raised when a query's dry-run estimate exceeds the configured ceiling.

    This is the hard stop that keeps an automated (cron) job from ever issuing a
    runaway scan against the 3.1 TB patents-public-data table.
    """

    def __init__(self, label, est_gb, ceiling_gb):
        self.label, self.est_gb, self.ceiling_gb = label, est_gb, ceiling_gb
        super().__init__(
            f"[{label}] dry-run estimate {est_gb:,.1f} GB exceeds ceiling {ceiling_gb:,.1f} GB "
            f"(~${est_gb / 1000 * USD_PER_TB:,.2f}). Refusing to run. "
            f"Raise the ceiling explicitly if this is intended."
        )


def client() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=GCP_PROJECT)
    return _client


def dry_run_gb(sql: str) -> float:
    cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    job = client().query(sql, job_config=cfg)
    return job.total_bytes_processed / 1e9


def usd(gb: float) -> float:
    """Estimated on-demand cost in USD for `gb` gigabytes scanned."""
    return gb / 1000.0 * USD_PER_TB


def estimate_and_guard(sql: str, ceiling_gb: float, label: str = "query", log=print) -> float:
    """Dry-run `sql`, log the estimate, and raise CostCeilingExceeded if it is over `ceiling_gb`.

    Every automated query MUST go through this. The dry run itself is free.
    Returns the estimate in GB so the caller can record what it actually committed to.
    """
    est = dry_run_gb(sql)
    log(f"[cost] {label}: dry-run estimate {est:,.1f} GB (~${usd(est):,.2f}); "
        f"ceiling {ceiling_gb:,.1f} GB")
    if est > ceiling_gb:
        raise CostCeilingExceeded(label, est, ceiling_gb)
    return est


def run(sql: str, max_gb_billed: float = 300.0):
    """Run a query, return list of dict rows. Caps billed bytes to avoid runaway cost."""
    cfg = bigquery.QueryJobConfig(
        maximum_bytes_billed=int(max_gb_billed * 1e9)
    )
    job = client().query(sql, job_config=cfg)
    return [dict(r) for r in job.result()]


def run_guarded(sql: str, ceiling_gb: float, label: str = "query", log=print):
    """dry-run guard + execute, with maximum_bytes_billed set to the same ceiling.

    Belt and braces: the dry run refuses obviously-too-big queries up front, and
    maximum_bytes_billed makes BigQuery itself abort the job server-side if the real
    scan somehow exceeds the estimate. Returns (rows, estimated_gb, billed_gb).
    """
    est = estimate_and_guard(sql, ceiling_gb, label, log=log)
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(ceiling_gb * 1e9))
    job = client().query(sql, job_config=cfg)
    rows = [dict(r) for r in job.result()]
    billed = (job.total_bytes_billed or 0) / 1e9
    log(f"[cost] {label}: BILLED {billed:,.1f} GB (~${usd(billed):,.2f}), {len(rows):,} rows")
    return rows, est, billed


def run_to_table(sql: str, dest_table: str, max_gb_billed: float = 300.0, cluster=None):
    """Materialize a query into a destination table (project.dataset.table)."""
    cfg = bigquery.QueryJobConfig(
        destination=dest_table,
        write_disposition="WRITE_TRUNCATE",
        maximum_bytes_billed=int(max_gb_billed * 1e9),
    )
    if cluster:
        cfg.clustering_fields = cluster
    job = client().query(sql, job_config=cfg)
    job.result()
    t = client().get_table(dest_table)
    return t.num_rows


def run_ddl_guarded(sql: str, dest_table: str, ceiling_gb: float, label: str = "ddl", log=print):
    """Run a CREATE OR REPLACE TABLE ... AS SELECT under the cost guard.

    Returns (num_rows, estimated_gb, billed_gb).
    """
    est = estimate_and_guard(sql, ceiling_gb, label, log=log)
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(ceiling_gb * 1e9))
    job = client().query(sql, job_config=cfg)
    job.result()
    billed = (job.total_bytes_billed or 0) / 1e9
    n = client().get_table(dest_table).num_rows
    log(f"[cost] {label}: BILLED {billed:,.1f} GB (~${usd(billed):,.2f}) -> {n:,} rows in {dest_table}")
    return n, est, billed


def cpc_like_clause(alias: str = "c", col: str = "code") -> str:
    """OR-of-LIKE fragment matching the seed CPC prefixes on UNNEST(cpc) AS c."""
    return " OR ".join(f"{alias}.{col} LIKE '{code}%'" for code in SEED_CPC)


def ensure_dataset(dataset: str = "patent_pilot", location: str = "US"):
    ds_id = f"{GCP_PROJECT}.{dataset}"
    try:
        client().get_dataset(ds_id)
    except Exception:
        ds = bigquery.Dataset(ds_id)
        ds.location = location
        # auto-clean scratch tables after 3 days
        ds.default_table_expiration_ms = 3 * 24 * 3600 * 1000
        client().create_dataset(ds, exists_ok=True)
    return ds_id
