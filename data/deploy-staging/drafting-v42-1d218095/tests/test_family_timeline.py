"""Feature 1 (worldwide family timeline) + Feature 2 (top-N prefetch bound) unit tests.

Pure/offline: no DB, no network. The INPADOC family fixture below is a trimmed capture of the
real OPS `family/publication/docdb/US.11999030.B2` wire shape (see src/ops_family.py docstring)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ops_family as F  # noqa: E402
import prefetch  # noqa: E402


# A compact but wire-faithful INPADOC family: one US application published twice (A1 + B2 — must
# collapse to ONE US 2023 entry), two DISTINCT CN applications filed 2019 (must stay CN CN), an EP
# filed 2019, and an IL priority-only member filed 2018.
FAMILY_XML = b"""<?xml version="1.0"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
 <ops:patent-family total-result-count="5">
  <ops:family-member family-id="1">
   <publication-reference><document-id document-id-type="docdb">
     <country>IL</country><doc-number>259585</doc-number><kind>A</kind><date>20180501</date>
   </document-id></publication-reference>
   <application-reference><document-id document-id-type="docdb">
     <country>IL</country><doc-number>259585</doc-number><kind>A</kind><date>20180501</date>
   </document-id></application-reference>
  </ops:family-member>
  <ops:family-member family-id="2">
   <publication-reference><document-id document-id-type="docdb">
     <country>US</country><doc-number>2023294251</doc-number><kind>A1</kind><date>20230921</date>
   </document-id></publication-reference>
   <application-reference><document-id document-id-type="docdb">
     <country>US</country><doc-number>202318200107</doc-number><kind>A</kind><date>20230522</date>
   </document-id></application-reference>
   <priority-claim><document-id document-id-type="docdb">
     <country>IL</country><doc-number>2019050502</doc-number><kind>W</kind><date>20190505</date>
   </document-id></priority-claim>
  </ops:family-member>
  <ops:family-member family-id="3">
   <publication-reference><document-id document-id-type="docdb">
     <country>US</country><doc-number>11999030</doc-number><kind>B2</kind><date>20240604</date>
   </document-id></publication-reference>
   <application-reference><document-id document-id-type="docdb">
     <country>US</country><doc-number>202318200107</doc-number><kind>A</kind><date>20230522</date>
   </document-id></application-reference>
  </ops:family-member>
  <ops:family-member family-id="4">
   <publication-reference><document-id document-id-type="docdb">
     <country>CN</country><doc-number>110603214</doc-number><kind>A</kind><date>20191220</date>
   </document-id></publication-reference>
   <application-reference><document-id document-id-type="docdb">
     <country>CN</country><doc-number>201880029000</doc-number><kind>A</kind><date>20190101</date>
   </document-id></application-reference>
  </ops:family-member>
  <ops:family-member family-id="5">
   <publication-reference><document-id document-id-type="docdb">
     <country>CN</country><doc-number>115158956</doc-number><kind>A</kind><date>20191220</date>
   </document-id></publication-reference>
   <application-reference><document-id document-id-type="docdb">
     <country>CN</country><doc-number>202210900000</doc-number><kind>A</kind><date>20190201</date>
   </document-id></application-reference>
  </ops:family-member>
  <ops:family-member family-id="6">
   <publication-reference><document-id document-id-type="docdb">
     <country>EP</country><doc-number>3620420</doc-number><kind>A1</kind><date>20200311</date>
   </document-id></publication-reference>
   <application-reference><document-id document-id-type="docdb">
     <country>EP</country><doc-number>19195000</doc-number><kind>A</kind><date>20190901</date>
   </document-id></application-reference>
  </ops:family-member>
 </ops:patent-family>
</ops:world-patent-data>"""


def _members():
    return F.parse_family_members(FAMILY_XML)


def test_parse_extracts_publication_application_and_priority():
    mem = _members()
    assert len(mem) == 6
    us_b2 = [m for m in mem if m["pub"] == "US11999030B2"][0]
    assert us_b2["country"] == "US" and us_b2["kind"] == "B2"
    assert us_b2["app_country"] == "US" and us_b2["app_number"] == "202318200107"
    assert us_b2["app_date"] == "20230522"
    # earliest priority propagated from the priority-claim on the A1 member
    a1 = [m for m in mem if m["pub"] == "US2023294251A1"][0]
    assert a1["prio_date"] == "20190505"


def test_year_bucketing_by_application_filing_date():
    tl = F.group_timeline(_members())
    years = [g["year"] for g in tl]
    assert years == sorted(years)                 # chronological
    assert "2018" in years and "2019" in years and "2023" in years
    y2018 = {g["year"]: g for g in tl}["2018"]
    assert [c["cc"] for c in y2018["codes"]] == ["IL"]


def test_country_extraction_uses_application_country():
    tl = {g["year"]: g for g in F.group_timeline(_members())}
    # 2019 cluster: two distinct CN applications + one EP -> CN CN EP (multiplicity preserved)
    ccs = sorted(c["cc"] for c in tl["2019"]["codes"])
    assert ccs == ["CN", "CN", "EP"]


def test_dedup_collapses_shared_application_but_keeps_distinct():
    tl = {g["year"]: g for g in F.group_timeline(_members())}
    # US A1 and US B2 share application 202318200107 -> ONE US entry in 2023, not two
    us2023 = [c["cc"] for c in tl["2023"]["codes"]]
    assert us2023 == ["US"]
    # the two CN filings are DIFFERENT applications -> both kept
    assert sum(1 for c in tl["2019"]["codes"] if c["cc"] == "CN") == 2


def test_summary_counts_members_and_jurisdictions():
    tl = F.group_timeline(_members())
    summ = F.family_summary(tl)
    # unique applications: IL(2018) + US(2023) + CN + CN + EP(2019) = 5, jurisdictions IL/US/CN/EP = 4
    assert summ["n_members"] == 5
    assert summ["n_jurisdictions"] == 4


def test_corpus_only_fallback_is_partial():
    rows = [{"pub": "US-11999030-B2", "country": "US", "date": "2023-05-22"},
            {"pub": "EP-3620420-A1", "country": "EP", "date": "2019-09-01"}]
    r = F.corpus_timeline("US-11999030-B2", rows)
    assert r["source"] == "corpus" and r["partial"] is True
    assert r["n_jurisdictions"] == 2
    assert {c["cc"] for g in r["timeline"] for c in g["codes"]} == {"US", "EP"}


def test_to_docdb_number_formatting():
    assert F.to_docdb("US-11999030-B2") == ("US.11999030.B2", "US.11999030")
    assert F.to_docdb("EP-2496850-A1") == ("EP.2496850.A1", "EP.2496850")
    # kindless numbers still yield a usable docdb form
    assert F.to_docdb("WO-2019012345")[0] == "WO.2019012345"


def test_unsafe_pubkey_rejected():
    import pytest
    with pytest.raises(ValueError):
        F._pubkey("../etc/passwd")


# ---- Feature 2: prefetch is bounded to top-N and idempotent --------------------------------
def test_prefetch_bounds_to_top_n(monkeypatch):
    calls = []
    monkeypatch.setattr(prefetch, "_one", lambda slug, pub: calls.append(pub))
    slug = "test-bound-slug"
    prefetch._STATUS.pop(slug, None)
    pubs = [f"US-{i:07d}-B2" for i in range(25)]
    snap = prefetch.prefetch_top(slug, pubs, n=10)
    assert len(snap["pubs"]) == 10                # only the top 10 scheduled, never all 25
    assert snap["pubs"] == pubs[:10]
    # idempotent: a second call for the same slug does not reschedule a different set
    snap2 = prefetch.prefetch_top(slug, pubs[::-1], n=10)
    assert snap2["pubs"] == pubs[:10]
    prefetch._STATUS.pop(slug, None)


def test_prefetch_default_top_n_is_bounded():
    assert prefetch.TOP_N <= 25 and prefetch.TOP_N >= 1
