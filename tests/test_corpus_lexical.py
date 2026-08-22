"""The release's Tantivy index, and the CJK share `to_tsvector('english')` is blind to.

39.9% of the corpus is CJK. `to_tsvector('english')` has no segmenter for it, so that share is not
degraded, it is dead, and a default Tantivy analyzer is no better: its simple tokenizer splits on
non-alphanumeric boundaries and a run of Chinese characters has none, so a whole sentence becomes
one term no query will ever match. These tests are the evidence that the configured analyzer
actually fixes it, rather than the docstring claiming it does.
"""
import pytest

from corpus import lexical_build

pytestmark = pytest.mark.skipif(not lexical_build.available(),
                                reason=f"tantivy unavailable: {lexical_build.TANTIVY_ERROR}")

DOCS = [
    {"chunk_id": 1, "publication_id": 10, "family_key": "F1", "kind": "claim_own", "lang": "en",
     "text": "A vacuum gripper comprising a suction cup and a rotary vane pump."},
    {"chunk_id": 2, "publication_id": 11, "family_key": "F2", "kind": "claim_own", "lang": "ja",
     "text": "真空吸着パッドを備えた把持装置。"},
    {"chunk_id": 3, "publication_id": 12, "family_key": "F3", "kind": "abstract", "lang": "zh",
     "text": "一种真空吸盘搬运装置，用于搬运多孔工件。"},
    {"chunk_id": 4, "publication_id": 13, "family_key": "F4", "kind": "abstract", "lang": "ko",
     "text": "진공 흡착 그리퍼를 포함하는 반송 장치."},
    {"chunk_id": 5, "publication_id": 14, "family_key": "F5", "kind": "paragraph", "lang": "de",
     "text": "Die Erfindung betrifft einen Vakuumgreifer fuer poroese Werkstuecke."},
]


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    path = tmp_path_factory.mktemp("lexical")
    report = lexical_build.build(str(path), list(DOCS))
    return str(path), report


def test_every_chunk_is_indexed_and_the_cjk_ones_are_counted(index):
    path, report = index
    assert report["docs"] == 5
    assert report["cjk_docs"] == 3, "Japanese, Chinese and Korean all have to reach the CJK field"
    assert lexical_build.count(path) == 5
    assert report["bytes"] > 0 and report["bytes_per_doc"] > 0


def test_a_cjk_query_finds_the_cjk_document(index):
    """The measurement that matters: today this returns nothing at all."""
    path, _ = index
    hits = lexical_build.search(path, "真空", limit=10)
    found = {h["chunk_id"] for h in hits}
    assert 2 in found and 3 in found, f"CJK query returned {found}"


def test_a_two_character_cjk_term_is_exact_rather_than_a_whole_sentence(index):
    path, _ = index
    assert {h["chunk_id"] for h in lexical_build.search(path, "吸盘", limit=10)} == {3}
    assert {h["chunk_id"] for h in lexical_build.search(path, "그리퍼", limit=10)} == {4}


def test_latin_text_is_unaffected_by_the_cjk_field(index):
    path, _ = index
    assert {h["chunk_id"] for h in lexical_build.search(path, "gripper", limit=10)} == {1}
    assert {h["chunk_id"] for h in lexical_build.search(path, "Vakuumgreifer", limit=10)} == {5}


def test_a_latin_only_chunk_is_not_given_a_cjk_field(index):
    """Restricting the bigram field to chunks that contain CJK is what keeps the ngram explosion
    off the 60.1% of the corpus that does not need it."""
    assert not lexical_build.has_cjk(DOCS[0]["text"])
    assert all(lexical_build.has_cjk(d["text"]) for d in DOCS[1:4])
    path, _ = index
    #  Searching the CJK field alone must not surface the latin documents.
    hits = lexical_build.search(path, "gripper", limit=10, fields=("text_cjk",))
    assert hits == []


def test_the_analyzer_configuration_is_recorded_for_the_manifest(index):
    """A shard has to be able to check that it is querying an index built the way it thinks."""
    _, report = index
    a = report["analyzer"]
    assert a["engine"] == "tantivy" and a["engine_version"]
    assert a["analyzer_version"] == lexical_build.ANALYZER_VERSION
    assert a["fields"]["text"]["tokenizer"] == "simple"
    assert a["fields"]["text_cjk"]["tokenizer"].startswith("ngram")
    assert a["fields"]["text"]["stored"] is False, \
        "storing the text duplicates a measured 902 B/chunk the release database already holds"


def test_the_index_is_a_measurable_cost_per_document(index):
    """LEXICAL_BYTES_PER_CHUNK is a sizing input, so it has to come from a measurement this repo
    can repeat rather than from a vendor's estimate."""
    from corpus import sizing
    _, report = index
    assert report["bytes"] == lexical_build.dir_bytes(report["path"])
    assert sizing.LEXICAL_BYTES_PER_CHUNK > 0
