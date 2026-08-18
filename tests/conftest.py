"""Shared fixtures + hermetic mocks for the test suite (Milestone 7 §1).

No paid APIs are hit: SerpApi (enrich.fetch_details) and the LLM (llm.chat_json) are mocked;
Vertex embeddings are replaced with deterministic hash-seeded vectors so dense/match queries run
fast and reproducibly. The real Postgres is read (fast). The reranker is never triggered (tests
use cached reports/views, not live agent runs)."""
import os, sys, hashlib
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

#  THE SUITE MUST NOT INHERIT A DEPLOYMENT'S COOKIE SCOPE. config.py loads the app's .env, and a
#  second instance under the same domain sets SESSION_COOKIE_PATH to its own prefix
#  (/patents-fable). Flask's test client honours that path, requests "/", is therefore never sent
#  the session cookie back, and EVERY login-dependent web test fails on a checkout whose only sin
#  is being deployed. 29 of 31 failures on 2026-08-18 were exactly this. python-dotenv does not
#  override variables that already exist, so setting them here wins over the .env.
os.environ.setdefault("SESSION_COOKIE_PATH", "/")
os.environ.setdefault("SESSION_COOKIE_NAME", "session")


def _det_vec(text, dim=768):
    """Deterministic unit-ish vector seeded by the text — same text -> same vector."""
    h = hashlib.sha256((text or "").encode()).digest()
    # expand the 32-byte digest into `dim` floats in [-1,1]
    vals = []
    i = 0
    while len(vals) < dim:
        h = hashlib.sha256(h + i.to_bytes(2, "big")).digest()
        vals.extend((b / 127.5 - 1.0) for b in h)
        i += 1
    return vals[:dim]


@pytest.fixture(autouse=True)
def no_paid_apis(monkeypatch):
    """Mock the paid/external calls for every test."""
    import embed, enrich, llm
    monkeypatch.setattr(embed, "embed_query", lambda text, dim=768, **k: _det_vec(text, dim))
    monkeypatch.setattr(embed, "embed_texts",
                        lambda texts, dim=768, **k: [_det_vec(t, dim) for t in texts])
    monkeypatch.setattr(enrich, "fetch_details", lambda *a, **k: None)   # no SerpApi
    monkeypatch.setattr(enrich, "fetch_google_full_text",
                        lambda *a, **k: {"claims": [], "description": [],
                                         "abstract": [], "source": ""})
    monkeypatch.setattr(llm, "chat_json",
                        lambda system, user, **k: {"elements": ["a vacuum gripper element",
                                                                 "a sealing element", "a vacuum pump"],
                                                   "queries": ["vacuum gripper"], "synonyms": [], "de": ""})
    #  The evidence store must be INERT under test: reuse would serve one test's mocked answers
    #  to another test asking about the same fake pub, and saves would pollute the real graph
    #  with fixture rows. The dedicated phase-1 tests re-enable pieces explicitly.
    import evidence
    monkeypatch.setattr(evidence, "REUSE", False)
    monkeypatch.setattr(evidence, "save_chart", lambda *a, **k: None)
    monkeypatch.setattr(evidence, "save_cells_from_chart", lambda *a, **k: 0)


@pytest.fixture(scope="session")
def gold_slug():
    return "grabo_gripper_novelty"


@pytest.fixture()
def app_client():
    import webapp
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()
