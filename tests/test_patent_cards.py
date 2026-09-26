"""Each patent's first drawing and abstract: read once from its Google Patents page, kept."""
import datetime
import io
import json
import urllib.error

import patent_cards as pc

PAGE = """<html><head><meta name="description" content="Gripper device (10) for gripping an object, comprising a base (12).">
</head><body>
<section itemprop="abstract"><h2>Abstract</h2><div class="abstract" lang="EN">The invention relates to a
method for handling an object (88) by means of a gripping apparatus (10), comprising a base (12-1, 12-2).</div></section>
<img itemprop="thumbnail" src="https://patentimages.storage.googleapis.com/thumbnails/US1/US1-D00001.png">
<img itemprop="thumbnail" src="https://patentimages.storage.googleapis.com/thumbnails/US1/US1-D00000.png">
<meta itemprop="full" content="https://patentimages.storage.googleapis.com/US1/US1-D00000.png">
</body></html>"""


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (900, 1200), (0, 0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def test_the_abstract_is_read_without_its_reference_numerals_and_thumbnails_come_first():
    abstract, urls = pc.parse(PAGE)
    assert abstract == ("The invention relates to a method for handling an object by means of a "
                        "gripping apparatus, comprising a base.")
    assert urls[0].endswith("/thumbnails/US1/US1-D00000.png")


def test_a_page_with_no_abstract_section_falls_back_to_its_description():
    abstract, _ = pc.parse('<meta name="description" content="Gripper device (10) for gripping.">')
    assert abstract == "Gripper device for gripping."


def test_a_card_keeps_a_small_jpeg_and_the_store_is_filled_once(tmp_path):
    calls = []

    def fetcher(url):
        calls.append(url)
        return PAGE.encode() if "patents.google.com" in url else _png()
    store = tmp_path / "cards.json"
    s = pc.fill(["US1", "US1"], fetcher=fetcher, path=store, image_dir=tmp_path / "img", pause=0,
                us=lambda row: None, ops_img=lambda p: None, ops_abs=lambda p: "")
    assert s["done"] == 1 and s["images"] == 1
    card = json.loads(store.read_text())["US1"]
    assert card["image"] == "US1.jpg" and card["abstract"].startswith("The invention")
    from PIL import Image
    im = Image.open(tmp_path / "img" / "US1.jpg")
    assert max(im.size) <= pc.THUMB_PX and im.mode == "RGB"
    pc.fill(["US1"], fetcher=fetcher, path=store, image_dir=tmp_path / "img", pause=0,
            us=lambda row: None, ops_img=lambda p: None, ops_abs=lambda p: "")
    assert len(calls) == 2                                   # page + drawing, once


def test_a_missing_page_is_retried_only_after_a_week(tmp_path):
    def gone(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    store = tmp_path / "cards.json"
    pc.fill(["EP9"], fetcher=gone, path=store, image_dir=tmp_path, pause=0,
            ops_img=lambda p: None, ops_abs=lambda p: "")
    card = json.loads(store.read_text())["EP9"]
    assert card["error"] == "Google Patents HTTP 404"
    assert not pc.needs(card)
    week = datetime.date.today() + datetime.timedelta(days=pc.RETRY_DAYS)
    assert pc.needs(card, today=week)


def test_scrapingbee_is_asked_for_google_only_with_custom_google(monkeypatch):
    seen = []

    class R:
        status = 200
        def read(self): return b"ok"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setenv("SCRAPINGBEE_API_KEY", "k")
    monkeypatch.setattr(pc.urllib.request, "urlopen", lambda req, timeout=0: seen.append(req.full_url) or R())
    pc._bee("https://patents.google.com/patent/US1/en")
    pc._bee("https://patentimages.storage.googleapis.com/US1/x.png")
    assert "custom_google=true" in seen[0] and "custom_google" not in seen[1]


def test_when_google_has_no_drawing_the_office_supplies_one(tmp_path):
    page = PAGE.replace("patentimages", "nowhere")          # the page lists no drawings
    fetcher = lambda url: page.encode()                      # noqa: E731
    asked = []

    def us(row):
        asked.append(row)
        (tmp_path / "US2.png").write_bytes(b"png")
        return "US2.png"
    store = tmp_path / "cards.json"
    pc.fill([{"publication": "US2", "application": "18/123,456"}, "EP3"], fetcher=fetcher, path=store,
            image_dir=tmp_path, pause=0, us=us, ops_img=lambda p: pc.thumbnail(_png()),
            ops_abs=lambda p: "", us_abs=lambda row: "")
    cards = json.loads(store.read_text())
    assert cards["US2"]["image"] == "US2.png" and cards["US2"]["source"] == "uspto"
    assert asked == [{"application": "18/123,456", "publication": "US2"}]
    assert cards["EP3"]["image"] == "EP3.jpg" and cards["EP3"]["source"] == "epo"


def test_a_card_from_before_the_fallbacks_is_tried_again_once():
    assert pc.needs({"abstract": "x", "image": "", "fetched": "2026-09-26"})
    assert not pc.needs({"v": pc.VERSION, "abstract": "x", "image": "",
                         "tried": datetime.date.today().isoformat()})
    assert pc.needs({"v": pc.VERSION - 1, "abstract": "x", "image": "y.jpg"})   # an older reading


def test_a_translated_page_keeps_only_the_english_and_a_b1_falls_back_to_claim_one():
    page = ('<section itemprop="claims"><div class="claim"><div class="claim-text">'
            '<span class="google-src-text">Sauggreifer (10) mit einem Gehaeuse</span>'
            'Suction gripper (10) with a housing</div></div></section>')
    abstract, _ = pc.parse(page)
    assert abstract == "Suction gripper with a housing"


def test_a_us_page_google_has_not_indexed_takes_the_abstract_from_the_file(tmp_path):
    def gone(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    store = tmp_path / "cards.json"
    pc.fill([{"publication": "US20260193044A1", "application": "18/999,001"}], fetcher=gone, path=store,
            image_dir=tmp_path, pause=0, us=lambda row: None,
            us_abs=lambda row: "A vacuum lifter with a sealing lip.")
    assert json.loads(store.read_text())["US20260193044A1"]["abstract"] == "A vacuum lifter with a sealing lip."
