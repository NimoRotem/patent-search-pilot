"""mongo_corpus.get_detail — normalization + candidate-key fallback, with a MOCKED client.

No network: a fake collection is injected so the tests are hermetic and reproducible. The real
doc shape is reproduced from the live 'Flying Power-Washing Drone' record (US20190168875A1),
including the corpus's misspelled `asignees` field and the nested classifications-of-lists shape.
"""
import pytest

import mongo_corpus as mc


# A trimmed copy of the real lemad doc for US20190168875A1.
DRONE_DOC = {
    "publicationNumber": "US20190168875A1",
    "title": "Flying Power-Washing Drone",
    "abstract": "A drone cleaning apparatus includes a drone and a pressure washer...",
    "country": "US",
    "date": "2018-12-06T08:00:00.000Z",
    "publicationDate": 20181206,
    "isFinal": True,
    "lang": "english",
    "pdf": "https://patentimages.storage.googleapis.com/60/27/34/2d4d3fe5eb5367/US20190168875A1.pdf",
    "figures": [
        {"thumbnail": "https://img/US20190168875A1-D00000-t.png",
         "full": "https://img/US20190168875A1-D00000.png"},
        {"thumbnail": "https://img/US20190168875A1-D00001-t.png",
         "full": "https://img/US20190168875A1-D00001.png"},
    ],
    "claims": [{"parent": "1. A drone cleaning apparatus, comprising: a drone; and a pressure washer.",
                "contents": ["wherein the wand is fastened to an underside of the drone"]},
               {"parent": "2. The apparatus of claim 1, wherein the drone hovers."}],
    "description": [{"title": "CROSS-REFERENCE", "content": ["[0001] Priority is claimed."]},
                    {"title": "BACKGROUND", "content": ["[0002] Cleaning tall buildings is hard.",
                                                        "[0003] Drones can help."]}],
    "classifications": [
        [{"cpc": "B", "text": "PERFORMING OPERATIONS"}, {"cpc": "B08", "text": "CLEANING"},
         {"cpc": "B08B3/024", "text": "Cleaning by spraying"}],
        [{"cpc": "B64", "text": "AIRCRAFT"}, {"cpc": "B64C39/024", "text": "UAVs"}],
    ],
    "inventors": [{"name": "Andrew McGuff Ashur"}, {"name": "Adrian Harry Mayans"}],
    "asignees": [{"name": "Lucid Bots Inc"}],       # NOTE: misspelled in the corpus
}

# A vector-only stub: publicationNumber + embedding, everything else empty. Must be a MISS.
STUB_DOC = {"publicationNumber": "US9999999B2", "figures": [], "claims": [],
            "description": [], "classifications": [], "title": None, "abstract": None,
            "isFinal": False}


class FakeCollection:
    """Minimal find_one over an in-memory {publicationNumber: doc} map."""
    def __init__(self, docs):
        self.by_key = {d["publicationNumber"]: d for d in docs}
        self.queries = []

    def find_one(self, q):
        self.queries.append(q["publicationNumber"])
        return self.by_key.get(q["publicationNumber"])


@pytest.fixture
def fake_mongo(monkeypatch, tmp_path):
    monkeypatch.setenv("LEMAD_MONGO_TEST", "1")          # opt in past the pytest auto-disable
    monkeypatch.setattr(mc, "CACHE_DIR", tmp_path / "mcache")
    col = FakeCollection([DRONE_DOC, STUB_DOC])
    monkeypatch.setattr(mc, "_get_collection", lambda: col)
    return col


def test_dropped_zero_form_resolves(fake_mongo):
    """The corpus/report spelling US2019168875A1 (dropped zero) resolves to the padded key."""
    d = mc.get_detail("US2019168875A1", use_cache=False)
    assert d is not None
    assert d["mongo_key"] == "US20190168875A1"          # padded key was tried first and hit
    assert d["n_figures"] == 2


def test_all_three_spellings_hit(fake_mongo):
    for form in ("US-2019168875-A1", "US2019168875A1", "US20190168875A1"):
        d = mc.get_detail(form, use_cache=False)
        assert d and d["n_figures"] == 2, form


def test_normalization_shape(fake_mongo):
    d = mc.get_detail("US-2019168875-A1", use_cache=False)
    assert d["title"] == "Flying Power-Washing Drone"
    assert d["figures"][0] == {"full": "https://img/US20190168875A1-D00000.png",
                               "thumbnail": "https://img/US20190168875A1-D00000-t.png"}
    assert d["assignees"] == ["Lucid Bots Inc"]         # read from misspelled `asignees`
    assert d["inventors"] == ["Andrew McGuff Ashur", "Adrian Harry Mayans"]
    assert len(d["claims"]) == 2 and d["claims"][0].startswith("1. A drone")
    assert "wherein the wand" in d["claims"][0]          # `contents` folded into the claim
    assert d["description"] and any("tall buildings" in p for p in d["description"])
    # nested classifications -> leaf CPC chips, most-specific per hierarchy, deduped
    codes = [c["code"] for c in d["classifications"]]
    assert codes == ["B08B3/024", "B64C39/024"]
    assert d["classifications"][0]["first"] is True
    assert d["pdf_url"].endswith(".pdf")
    assert d["publication_date"] == "2018-12-06"


def test_miss_returns_none(fake_mongo):
    assert mc.get_detail("US-0000001-A1", use_cache=False) is None


def test_vector_only_stub_is_a_miss(fake_mongo):
    """A doc with a publicationNumber but no figures/text is a stub -> None, so the caller still
    runs live recovery instead of caching a blank as a hit."""
    assert mc.get_detail("US-9999999-B2", use_cache=False) is None


def test_cache_avoids_second_query(fake_mongo):
    mc.get_detail("US-2019168875-A1")                    # populates the on-disk cache
    n = len(fake_mongo.queries)
    mc.get_detail("US-2019168875-A1")                    # served from cache
    assert len(fake_mongo.queries) == n


def test_bad_pub_never_raises(fake_mongo):
    assert mc.get_detail("../../etc/passwd") is None
    assert mc.get_detail(None) is None


def test_disabled_when_not_opted_in(monkeypatch):
    """Without LEMAD_MONGO_TEST, mongo_corpus is inert under pytest (no network)."""
    monkeypatch.delenv("LEMAD_MONGO_TEST", raising=False)
    assert mc.available() is False
    assert mc.get_detail("US-2019168875-A1", use_cache=False) is None
