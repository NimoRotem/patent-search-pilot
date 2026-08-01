import json
import zipfile

import report_archive


def test_google_patents_parser_extracts_full_sections():
    parser = report_archive._GoogleTextParser()
    parser.feed("""
      <section itemprop='claims'><div class='claim'>1. A lifting device.</div></section>
      <section itemprop='description'><h2>Description</h2><p>The plate carries an RFID tag.</p></section>
      <div class='abstract'><p>A vacuum lifter.</p></div>
    """)
    assert "A lifting device" in " ".join(parser.clean(parser.claims))
    assert "RFID tag" in " ".join(parser.clean(parser.description))
    assert "vacuum lifter" in " ".join(parser.clean(parser.abstract))


def test_google_full_text_refreshes_partial_cache_and_retries(tmp_path, monkeypatch):
    cache = tmp_path / "US-1234567-A1.json"
    cache.write_text(json.dumps({"claims": ["1. Cached claim."], "description": [],
                                 "abstract": ["Cached abstract."]}))
    monkeypatch.setattr(report_archive, "_google_cache_path", lambda pub: cache)
    monkeypatch.setattr(report_archive.pubnorm, "google_url",
                        lambda pub: "https://patents.example/US1234567A1/en")
    monkeypatch.setattr(report_archive.pubnorm, "mongo_candidates", lambda pub: [])
    monkeypatch.setattr(report_archive._STOP, "wait", lambda seconds: False)

    class Response:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

    responses = [Response(503), Response(200, """
      <section itemprop='claims'><div>1. Fresh claim.</div></section>
      <section itemprop='description'><p>Recovered complete description.</p></section>
    """)]
    monkeypatch.setattr(report_archive.requests, "get",
                        lambda *args, **kwargs: responses.pop(0))
    recovered = report_archive._google_full_text("US-1234567-A1")
    assert recovered["claims"] == ["1. Cached claim."]
    assert recovered["description"] == ["Recovered complete description."]
    assert json.loads(cache.read_text())["description"]


def _record(pub):
    return {
        "publication": {"publication_number": pub, "title": "RFID vacuum lifting tool",
                        "abstract": "A powered vacuum lifter.", "publication_date": "2020-01-02"},
        "claims": [{"claim_no": 1, "is_independent": True,
                    "text": "A handle with an RFID reader and a detachable base."}],
        "paragraphs": [{"para_no": "0010", "heading": "Detailed description",
                        "text": "The reader changes lifting settings for the attached plate."}],
        "figures": [], "classifications": [{"code": "B66C1/02"}], "parties": [],
        "citations": [], "images": ["https://example.test/sketch-1.png"],
        "sources": ["unit test corpus"], "full_text": True,
    }


def test_markdown_contains_full_text_and_sketch_links():
    card = {"pub": "US-1234567-A", "archive_rank": 1, "title": "RFID lifter"}
    text = report_archive._markdown(card, _record(card["pub"]))
    assert "## Claims" in text and "## Full description" in text
    assert "RFID reader" in text and "changes lifting settings" in text
    assert "[Sketch 1](https://example.test/sketch-1.png)" in text


def test_build_writes_one_markdown_per_ranked_patent(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archives"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    monkeypatch.setattr(report_archive, "ARCHIVE_DIR", archive_dir)
    cards = [{"pub": "US-1234567-A", "title": "One", "archive_rank": 1},
             {"pub": "EP-7654321-A1", "title": "Two", "archive_rank": 2}]
    monkeypatch.setattr(report_archive, "_candidate_cards", lambda report, view: cards)
    monkeypatch.setattr(report_archive, "_merge_record", lambda cur, card: _record(card["pub"]))

    class Cursor:
        def close(self):
            pass

    class Connection:
        autocommit = False
        def cursor(self):
            return Cursor()
        def close(self):
            pass

    monkeypatch.setattr(report_archive.db, "connect", lambda: Connection())
    state = report_archive._build("archive-unit", {"query": "RFID lifter"}, {"cards": cards},
                                  reports_dir, "abc123")
    assert state["ready"] is True and state["n_patents"] == 2 and state["n_full_text"] == 2
    path = archive_dir / state["archive_file"]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert len([n for n in names if n.endswith(".md") and n != "README.md"]) == 2
        manifest = json.loads(zf.read("manifest.json"))
        assert [p["publication"] for p in manifest["patents"]] == ["US-1234567-A", "EP-7654321-A1"]


def test_candidate_cards_replace_non_publication_api_identifiers(monkeypatch):
    monkeypatch.setattr(report_archive, "TOP_N", 3)
    monkeypatch.setattr(report_archive.webview, "build_view", lambda report, top_n: {"cards": [
        {"pub": "USPCTUS2020057079", "relevancy_score": 1.0},
        {"pub": "US63176890", "relevancy_score": .99},
        {"pub": "US-2222222-B2", "relevancy_score": .9},
        {"pub": "EP-3333333-A1", "relevancy_score": .8},
    ]})
    monkeypatch.setattr(report_archive.webview, "mongo_enrich_cards", lambda cards: None)
    cards = report_archive._candidate_cards({}, {"cards": [
        {"pub": "USPCTUS2020057079", "relevancy_score": 1.0},
        {"pub": "US-1111111-A1", "relevancy_score": .95},
    ]})
    assert [card["pub"] for card in cards] == [
        "US-1111111-A1", "US-2222222-B2", "EP-3333333-A1"]


def test_shutdown_state_does_not_accept_new_archive_work(tmp_path):
    report_archive._STOP.set()
    try:
        state = report_archive.ensure("stopping", {"query": "x"}, {}, tmp_path)
        assert state["status"] == "interrupted" and state["ready"] is False
    finally:
        report_archive._STOP.clear()
