"""Central config — loads .env once. (spec §1)"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
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
JURISDICTIONS = ["US", "EP", "WO", "DE"]

DATA = ROOT / "data"
PDF_DIR = DATA / "pdfs"
