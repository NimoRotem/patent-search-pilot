"""OPS facsimile recovery: instance parsing, page addressing, and both-links building.

All offline — these lock the wire-format details that were established against live OPS
(and the one that was previously WRONG), so a future refactor cannot silently go dark
again the way `parse_images` did.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ops
import ops_drawings


# Verbatim shape of a real OPS images response (DE1286275B, 2026-07-19).
IMAGES_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
  <ops:document-inquiry>
    <ops:inquiry-result>
      <ops:document-instance system="ops.epo.org" number-of-pages="4" desc="FullDocument"
          link="published-data/images/DE/1286275/B/fullimage"/>
      <ops:document-instance system="ops.epo.org" number-of-pages="1" desc="Drawing"
          link="published-data/images/DE/1286275/B/thumbnail"/>
    </ops:inquiry-result>
  </ops:document-inquiry>
</ops:world-patent-data>"""


def test_parse_images_finds_full_document():
    """REGRESSION: the desc is 'FullDocument'. An earlier version matched 'fullimage' —
    the last segment of the LINK, not the desc — so every full-document instance was
    dropped and publications without a separate Drawing instance looked like they had no
    imagery at all."""
    insts = ops.parse_images(IMAGES_XML)
    descs = {i["desc"] for i in insts}
    assert "FullDocument" in descs
    assert "Drawing" in descs


def test_parse_images_puts_drawing_first():
    """A Drawing instance is drawing sheets by construction, so it must be preferred: it
    needs no drawing-vs-text classification, which is where figures get lost."""
    insts = ops.parse_images(IMAGES_XML)
    assert insts[0]["desc"] == "Drawing"
    assert insts[0]["pages"] == 1
    assert insts[0]["link"].endswith("/thumbnail")


def test_image_page_url_shape():
    """OPS serves ONE PAGE PER REQUEST as `{link}.pdf?Range=<n>`; there is no
    whole-document form. Encoded here because the page cap depends on it."""
    link = ops.parse_images(IMAGES_XML)[0]["link"]
    assert link == "published-data/images/DE/1286275/B/thumbnail"
    assert ops.MAX_IMAGE_PAGES >= 1


def test_espacenet_family_url_matches_known_good():
    """The family id is zero-padded to NINE digits in Espacenet's path. OPS returns it
    unpadded ('07128644'), so an unpadded id builds a broken link."""
    url = ops_drawings.espacenet_url("DE-1286275-B", "07128644")
    assert url == ("https://worldwide.espacenet.com/patent/search/family/007128644"
                   "/publication/DE1286275B?q=pn%3DDE1286275B")


def test_espacenet_falls_back_to_publication_scope():
    url = ops_drawings.espacenet_url("DE-1286275-B", None)
    assert url == ("https://worldwide.espacenet.com/patent/search/publication"
                   "/DE1286275B?q=pn%3DDE1286275B")


def test_espacenet_uses_exact_pn_lookup_not_free_text():
    """`pn=` is an exact publication lookup; a bare term is a full-text search that can
    land on the wrong document."""
    assert "q=pn%3D" in ops_drawings.espacenet_url("EP-0267233-A1")


def test_recover_is_noop_without_credentials(monkeypatch):
    """No creds must mean a clean empty result, never an exception into the display path."""
    monkeypatch.setattr(ops, "have_creds", lambda: False)
    assert ops_drawings.recover("DE-1286275-B") == {}


def test_want_for_national_docs_skips_fulltext():
    """OPS serves full text for EP/WO only; asking for DE claims spends quota on a
    guaranteed 404. Images and legal DO resolve for DE, which is the point."""
    assert "claims" not in ops.want_for("DE-1286275-B")
    assert "images" in ops.want_for("DE-1286275-B")
    assert "claims" in ops.want_for("EP-2496850-A1")
