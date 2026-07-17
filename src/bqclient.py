"""BigQuery access (bootstrap source only — spec §2). Uses the GCE service-account creds."""
from __future__ import annotations
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
from google.cloud import bigquery
from config import GCP_PROJECT, SEED_CPC

_client = None


def client() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=GCP_PROJECT)
    return _client


def dry_run_gb(sql: str) -> float:
    cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    job = client().query(sql, job_config=cfg)
    return job.total_bytes_processed / 1e9


def run(sql: str, max_gb_billed: float = 300.0):
    """Run a query, return list of dict rows. Caps billed bytes to avoid runaway cost."""
    cfg = bigquery.QueryJobConfig(
        maximum_bytes_billed=int(max_gb_billed * 1e9)
    )
    job = client().query(sql, job_config=cfg)
    return [dict(r) for r in job.result()]


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
