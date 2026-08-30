"""The dates that decide whether a document may be filed come from the corpus, not a cover cache.

Measured 2026-08-20. US 2022/0331993 A1 was offered against a target whose effective filing date is
2021-08-02:

    enrichment cache   publication_date 2022-04-20   priority ''           filing ''
    corpus             publication_date 2022-10-20   priority 2021-04-20   filing 2022-04-20

The cache had put the FILING date in the publication field and dropped the priority date, so
`classify_basis` saw no priority, called the document NOT_PRIOR_ART, and the compliance pass
refused to file it. It is 102(a)(2) art: earlier-filed, later-published. `subject_facts` already
reads the target's own effective filing date from the corpus for exactly this reason.
"""
import concise_description as cd
import submission_compliance as sc


def test_the_corpus_dates_win_over_the_cache(monkeypatch):
    monkeypatch.setattr(cd, "_display", lambda pub, allow_fetch=True: {
        "title": "Portable vacuum gripper", "publication_date": "2022-04-20",
        "priority_date": "", "filing_date": "", "assignees": ["Individual"]})
    monkeypatch.setattr(cd, "corpus_dates", lambda pub: {
        "publication_date": "2022-10-20", "priority_date": "2021-04-20",
        "filing_date": "2022-04-20", "country": "US"})
    b = cd.biblio("US-2022331993-A1")
    assert b["publication_date"] == "2022-10-20"
    assert b["priority_date"] == "2021-04-20"
    assert b["filing_date"] == "2022-04-20"
    assert b["dates_source"] == "corpus"


def test_the_cache_is_the_fallback_not_the_source(monkeypatch):
    monkeypatch.setattr(cd, "_display", lambda pub, allow_fetch=True: {
        "title": "t", "publication_date": "2019-01-01", "priority_date": "2017-01-01",
        "filing_date": "2018-01-01", "assignees": []})
    monkeypatch.setattr(cd, "corpus_dates", lambda pub: {})
    b = cd.biblio("XX-999-A1")
    assert b["publication_date"] == "2019-01-01"
    assert b["dates_source"] == "enrichment cache"


def test_the_reference_that_was_wrongly_refused_now_qualifies():
    """End to end on the real numbers: earlier-filed, later-published, US, so 102(a)(2)."""
    doc = {"pub": "US-2022331993-A1", "rows": [], "summary": "",
           "biblio": {"pub": "US-2022331993-A1", "country": "US",
                      "publication_date": "2022-10-20", "priority_date": "2021-04-20",
                      "filing_date": "2022-04-20", "assignee": ""}}
    q = sc.qualify(doc, "2021-08-02")
    assert q["basis"] == "secret_prior_art"
    assert q["blocked"] is False


def test_the_cache_dates_would_have_refused_it():
    """Guards the direction of the bug: the same document, dated from the cache, is refused."""
    doc = {"pub": "US-2022331993-A1", "rows": [], "summary": "",
           "biblio": {"pub": "US-2022331993-A1", "country": "US",
                      "publication_date": "2022-04-20", "priority_date": "", "filing_date": "",
                      "assignee": ""}}
    q = sc.qualify(doc, "2021-08-02")
    assert q["basis"] == "not_prior_art" and q["blocked"] is True
