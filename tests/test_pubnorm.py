"""Canonical publication-number normalizer + variant generator (pubnorm).

The load-bearing case is the dropped-leading-zero US pre-grant bug: BigQuery/our reports store
`US2019168875A1`, but Google Patents and the lemad Mongo key it `US20190168875A1`. pubnorm must
put the zero-PADDED key first so the lookup hits regardless of which spelling we hold.
"""
import pytest

import pubnorm


@pytest.mark.parametrize("raw,expected", [
    ("US-2019168875-A1", "US-2019168875-A1"),
    ("US2019168875A1", "US-2019168875-A1"),          # concatenated report form
    ("us 2019168875 a1", "US-2019168875-A1"),        # spaced, lowercase
    ("DE1286275B", "DE-1286275-B"),
    ("de-1286275-b", "DE-1286275-B"),
    ("EP4048620B1", "EP-4048620-B1"),
    ("US-11999030-B2", "US-11999030-B2"),
    ("US1691198A", "US-1691198-A"),                  # old grant, single-letter kind
])
def test_canonical(raw, expected):
    assert pubnorm.canonical(raw) == expected


@pytest.mark.parametrize("junk", ["", None, "not a patent", "1234", "X", "../../etc/passwd", "A"])
def test_canonical_rejects_junk(junk):
    assert pubnorm.canonical(junk) is None
    assert pubnorm.mongo_candidates(junk) == []


def test_dropped_zero_padded_key_is_first():
    """THE fix: both stored spellings must produce the padded Google/Mongo key FIRST."""
    for form in ("US-2019168875-A1", "US2019168875A1", "US20190168875A1"):
        cands = pubnorm.mongo_candidates(form)
        assert cands[0] == "US20190168875A1", (form, cands)
        # the un-padded (dropped-zero) spelling is still tried, just not first
        assert "US2019168875A1" in cands


def test_us_pregrant_generates_both_directions():
    """Given the PADDED spelling we must still try the dropped-zero one, and vice-versa."""
    padded = pubnorm.mongo_candidates("US20190168875A1")
    assert "US20190168875A1" in padded and "US2019168875A1" in padded
    dropped = pubnorm.mongo_candidates("US2019168875A1")
    assert "US20190168875A1" in dropped and "US2019168875A1" in dropped


def test_second_dropped_zero_example():
    """A 2006 pre-grant with the same pattern (US2006026971A1 -> US20060026971A1)."""
    cands = pubnorm.mongo_candidates("US-2006026971-A1")
    assert cands[0] == "US20060026971A1"
    assert "US2006026971A1" in cands


def test_kind_included_in_candidates():
    cands = pubnorm.mongo_candidates("DE-1286275-B")
    assert cands[0] == "DE1286275B"          # concatenated WITH kind, tried first
    assert "DE1286275" in cands              # kindless fallback


def test_non_us_not_zero_padded():
    """A granted US patent (no year prefix) must NOT be treated as pre-grant / padded."""
    cands = pubnorm.mongo_candidates("US-9737154-B2")
    assert cands[0] == "US9737154B2"
    # no bogus 11-digit padding was invented
    assert all(not c.startswith("US09737154") for c in cands)


def test_candidates_are_deduped_and_ordered():
    cands = pubnorm.mongo_candidates("US-2019168875-A1")
    assert len(cands) == len(set(cands))     # no duplicates


# ---- outbound deep links (Google Patents / Espacenet) --------------------------------------
# The dropped-leading-zero bug also produced DEAD outbound links. The user's live example:
#   BROKEN  https://patents.google.com/patent/US2022153556        (a MISSING page)
#   CORRECT https://patents.google.com/patent/US20220153556A1/en  (resolves)
def test_google_url_pads_user_example():
    assert pubnorm.google_url("US-2022153556-A1") == \
        "https://patents.google.com/patent/US20220153556A1/en"


def test_google_url_pads_user_example_kindless():
    # Even without a kind code the leading zero must be restored (never the bare dropped form).
    assert pubnorm.google_url("US2022153556") == \
        "https://patents.google.com/patent/US20220153556/en"
    assert "US2022153556/" not in pubnorm.google_url("US2022153556")


def test_google_url_covers_all_jurisdictions():
    assert pubnorm.google_url("US-2019168875-A1") == \
        "https://patents.google.com/patent/US20190168875A1/en"          # US pre-grant padded
    assert pubnorm.google_url("US-9737154-B2") == \
        "https://patents.google.com/patent/US9737154B2/en"              # US grant, not padded
    assert pubnorm.google_url("EP-4048620-B1") == \
        "https://patents.google.com/patent/EP4048620B1/en"
    assert pubnorm.google_url("WO-2020123456-A1") == \
        "https://patents.google.com/patent/WO2020123456A1/en"
    assert pubnorm.google_url("DE-1286275-B") == \
        "https://patents.google.com/patent/DE1286275B/en"
    assert pubnorm.google_url("CN-112233445-A") == \
        "https://patents.google.com/patent/CN112233445A/en"


def test_espacenet_url_pads_user_example():
    u = pubnorm.espacenet_url("US-2022153556-A1")
    assert "US20220153556A1" in u
    assert "pn%3DUS20220153556A1" in u
    assert "US2022153556A1" not in u.replace("US20220153556A1", "")     # no dropped form left


def test_espacenet_url_family_scoped():
    u = pubnorm.espacenet_url("US-2019168875-A1", family_id="12345")
    assert "/family/000012345/" in u                                    # zero-padded to 9 digits
    assert "US20190168875A1" in u


def test_link_builders_none_on_junk():
    assert pubnorm.google_url("") is None
    assert pubnorm.espacenet_url(None) is None
