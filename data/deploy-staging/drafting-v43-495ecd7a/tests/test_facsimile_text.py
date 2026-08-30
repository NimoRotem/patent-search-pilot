"""Reading a publication off its own printed pages, and refusing to when the reading is invented.

DE 10 2024 133 318 A1, 2026-08-20: Google Patents 404s on it, the EPO full-text service covers EP
and WO only, and the extractor's verdict was "no usable text extracted". The search ran on a vision
description of four drawings and called a vacuum tube lifter "a robotic arm end effector system".

The first attempt at a fix handed the model the DRAWING instance — four pages of line art — and it
returned a complete title, abstract and claim for a forestry tool. None of it was in the document.
That is why corroboration is not optional here.
"""
import facsimile_text as fx


REAL = {
    "title": "Schlauchheber mit Belüftungsventil und Drosselventil",
    "abstract": "Die Erfindung betrifft einen Schlauchheber mit einem Belüftungsventil und "
                "selbsttätig schaltendem Drosselventil.",
    "claims": [{"claim_no": 1, "independent": True,
                "text": "Schlauchheber (10), umfassend: einen Hubschlauch (12), welcher einen "
                        "Schlauchinnenraum (14) aufweist"}],
}
#  What the model produced from the drawings alone. Fluent, plausible, and about another invention.
INVENTED = {
    "title": "Vorrichtung zum Betreiben eines Arbeitsgeräts",
    "abstract": "Die Erfindung betrifft eine Vorrichtung zum Betreiben eines Arbeitsgeräts, "
                "insbesondere eines Forst- oder Gartengeräts, mit einem Elektromotor.",
    "claims": [{"claim_no": 1, "independent": True,
                "text": "Vorrichtung (10) zum Betreiben eines Arbeitsgeräts (12) mit einem "
                        "Gehäuse (14) und einem Elektromotor (32)"}],
}
OFFICIAL = ("Die Erfindung betrifft einen Schlauchheber mit einem Belüftungsventil (32) und "
            "selbsttätig schaltendem Drosselventil (34). Die Erfindung betrifft auch eine "
            "Bedienvorrichtung für einen Schlauchheber.")


def test_the_real_transcription_is_corroborated():
    ok, hit, total = fx.corroborated(REAL, OFFICIAL)
    assert ok, "the genuine transcription was rejected (%d of %d)" % (hit, total)
    assert hit >= 3


def test_the_invented_one_is_not():
    """THE WHOLE POINT. Fluent German about a different machine must not pass."""
    ok, hit, total = fx.corroborated(INVENTED, OFFICIAL)
    assert not ok, "an invented claim set was corroborated (%d of %d)" % (hit, total)


def test_an_empty_reference_cannot_corroborate_anything():
    """"0 of 0 words matched" is not a check. The first version of this gate treated it as a pass
    and would have accepted the invented claims."""
    for ref in ("", None, "der die das"):
        ok, _hit, _tot = fx.corroborated(REAL, ref)
        assert not ok, ref


def test_nothing_is_returned_when_the_reading_is_not_corroborated(monkeypatch):
    """`read` must return {} rather than a claim set nobody can trust."""
    monkeypatch.setattr(fx, "full_document_pages", lambda pub, log=print, **k: [b"%PDF-1.4"])
    monkeypatch.setattr(fx, "official_abstract", lambda pub, log=print: OFFICIAL)

    class _Resp:
        text = '{"title":"Vorrichtung zum Betreiben eines Arbeitsgeräts","language":"de",' \
               '"abstract":"Forst- oder Gartengerät mit Elektromotor","claims":' \
               '[{"claim_no":1,"independent":true,"text":"Vorrichtung zum Betreiben eines ' \
               'Arbeitsgeräts mit einem Gehäuse und einem Elektromotor"}]}'

    import llm
    monkeypatch.setattr(llm, "_call_vision", lambda *a, **k: _Resp())
    assert fx.read("DE102024133318A1", log=lambda *a: None) == {}


def test_a_corroborated_reading_comes_back_with_its_claims(monkeypatch):
    monkeypatch.setattr(fx, "full_document_pages", lambda pub, log=print, **k: [b"%PDF-1.4"] * 19)
    monkeypatch.setattr(fx, "official_abstract", lambda pub, log=print: OFFICIAL)

    class _Resp:
        text = ('{"title":"Schlauchheber mit Belüftungsventil und Drosselventil","language":"de",'
                '"abstract":"Die Erfindung betrifft einen Schlauchheber mit einem '
                'Belüftungsventil und selbsttätig schaltendem Drosselventil. Bedienvorrichtung.",'
                '"claims":[{"claim_no":1,"independent":true,"text":"Schlauchheber (10), umfassend '
                'einen Hubschlauch (12) mit einem Schlauchinnenraum (14)"},'
                '{"claim_no":2,"independent":false,"text":"Schlauchheber nach Anspruch 1, wobei '
                'der Ventilkörper (64) beaufschlagt ist"},'
                '{"claim_no":3,"independent":false,"text":"zu kurz"}]}')

    import llm
    monkeypatch.setattr(llm, "_call_vision", lambda *a, **k: _Resp())
    got = fx.read("DE102024133318A1", log=lambda *a: None)
    assert got["source"] == "facsimile" and got["n_pages"] == 19
    #  the stub row is dropped, the two real ones survive with their own numbers
    assert [c["claim_no"] for c in got["claims"]] == [1, 2]
    assert got["claims"][0]["independent"] is True
    assert got["corroboration"]["matched"] >= 3


def test_the_drawings_instance_is_never_used(monkeypatch):
    """`ops.fetch_facsimile` prefers the Drawing instance, which is what produced the invented
    claim set. This path must ask for FullDocument by name and give up if there is none."""
    calls = {}

    class _Ops:
        def ops_fetch(self, pub, want=()):
            return {"images": [{"desc": "Drawing", "pages": 4, "link": "thumb"}]}

        def fetch_image_page(self, link, i):
            calls["link"] = link
            return b"x"

    import sys
    monkeypatch.setitem(sys.modules, "ops", _Ops())
    assert fx.full_document_pages("DE102024133318A1", log=lambda *a: None) == []
    assert "link" not in calls, "it fetched pages from the drawings instance"


def test_the_extractor_reaches_for_it_only_when_there_is_nothing_else():
    """A publication record that HAS an abstract or claims must not pay for a vision pass."""
    import ingest_input
    src = open(ingest_input.__file__.replace(".pyc", ".py")).read()
    assert "import facsimile_text" in src
    i = src.index("import facsimile_text")
    guard = src[max(0, i - 400):i]
    assert "if not (abstract or claims_list):" in guard, (
        "the facsimile read is not gated on the record being empty")
