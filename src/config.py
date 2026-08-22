"""Central config — loads .env once. (spec §1)"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Parser-only release checks need no credentials and may run as a deployment account that cannot
# read the application's secret file. Keep normal runtime behavior unchanged while giving those
# checks an explicit way to prove they are credential-free.
if os.environ.get("PATENT_SKIP_DOTENV", "").lower() not in ("1", "true", "yes"):
    load_dotenv(ROOT / ".env")

# Postgres
PG = dict(
    host=os.environ.get("PGHOST", "127.0.0.1"),
    port=int(os.environ.get("PGPORT", "5433")),
    dbname=os.environ.get("PGDATABASE", "patents"),
    user=os.environ.get("PGUSER", "patents"),
    password=os.environ.get("PGPASSWORD", "patents_pilot_local"),
)
PG_DSN = f"host={PG['host']} port={PG['port']} dbname={PG['dbname']} user={PG['user']} password={PG['password']}"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "nimo-gpt")

# Embedding
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 768                       # main run (Matryoshka shortening via dimensions param)
BENCH_DIMS = [1024, 3072]             # small benchmark subset only (spec §7/§8)

# LLM for the coverage-ledger agent (query generation / terminology). OpenAI side of the house.
AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4o-mini")

# Patent-drafting model roles. Feature modules consume roles from this central configuration so a
# model release or eval promotion is an environment/config change rather than a code rewrite.
PATENT_FIGURE_IMAGE_MODEL = os.environ.get(
    "PATENT_FIGURE_IMAGE_MODEL", "gemini-2.5-flash-image")
PATENT_FIGURE_VISION_MODEL = os.environ.get(
    "PATENT_FIGURE_VISION_MODEL", "gemini-2.5-pro")

# Seed CPC classes (spec §0). Stored without the space BigQuery uses (e.g. "B66C1/02").
SEED_CPC = [
    "B66C1/02",       # suction lifting devices
    "B66C1/0225",     # handheld suction lifting devices
    "B25J15/0616",    # robotic vacuum grippers
    "B65G47/91",      # suction transfer devices
    "B65G49/061",     # suction handling of fragile sheets
    "B25B11/005",     # vacuum work holders
    "F16B47/00",      # suction cups
    "B65G7/12",       # carrying objects by hand
]
# Human-readable CPC titles for the agent's neighbouring-class reasoning.
SEED_CPC_TITLES = {
    "B66C1/02": "suction lifting devices",
    "B66C1/0225": "handheld suction lifting devices",
    "B25J15/0616": "robotic vacuum grippers",
    "B65G47/91": "suction transfer devices",
    "B65G49/061": "suction handling of fragile sheets",
    "B25B11/005": "vacuum work holders",
    "F16B47/00": "suction cups",
    "B65G7/12": "carrying objects by hand",
}
#  Fallback only. The disclosure now reports the jurisdictions actually present in the corpus
#  (corpus_facts reads them from the publications table); this constant is used only if that read
#  fails, so a DB hiccup degrades to the historic scope rather than to an empty statement.
JURISDICTIONS = ["US", "EP", "WO", "DE"]

#  INGEST scope — which offices the BigQuery extract pulls. Empty list = WORLDWIDE (no country
#  filter at all).
#
#  This was hard-coded as `country_code IN ('US','EP','WO','DE')` in five places across
#  ingest_bq.py and incremental_ingest.py. Two reasons it had to become config:
#    1. BigQuery bills COLUMNS REFERENCED, not rows matched, on this unpartitioned table — the
#       full-text extract costs the same $9.57 whether it returns 25,786 rows or 170 million. The
#       four-office filter was therefore buying nothing at the source; it only shrank the corpus.
#    2. Leaving the weekly delta job narrower than the bootstrap would quietly re-narrow the
#       corpus over time, so both read this one value.
#  Measured: the 8 seed subgroups hold 25,786 pubs across US/EP/WO/DE and 80,308 worldwide.
INGEST_JURISDICTIONS = [c for c in os.environ.get("INGEST_JURISDICTIONS", "").split(",") if c.strip()]

#  INGEST scope — which CPC prefixes the BigQuery extract pulls. Defaults to SEED_CPC.
#
#  Deliberately SEPARATE from SEED_CPC rather than a redefinition of it. SEED_CPC is the *field
#  definition*: domain_detect.py calibrates its in-domain / out-of-domain thresholds against it
#  (SEED_SUBCLASSES = {c[:4] for c in SEED_CPC}), and agent.py feeds it to the LLM as the
#  neighbouring-class context. Widening SEED_CPC therefore silently invalidates a calibrated
#  router, which is a ranking change disguised as a config edit — exactly the class of change the
#  M9 lesson says must be measured, not assumed. Widening the INGEST is safe; re-pointing the
#  router is a separate, measured step.
#
#  What the corpus actually holds is reported from the DATA (corpus_facts), not from either
#  constant, so the disclosure cannot drift out of step with either one.
INGEST_CPC = [c for c in os.environ.get("INGEST_CPC", "").split(",") if c.strip()] or SEED_CPC

DATA = ROOT / "data"
PDF_DIR = DATA / "pdfs"
