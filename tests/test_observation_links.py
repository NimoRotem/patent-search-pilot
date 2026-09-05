"""The docket's links into the filing app's packets and this app's own searches.

Everything here runs against temp directories shaped like the two real stores, so what is pinned
is the on-disk format each one actually writes: a US 1.290 packet's meta.json with an
application number and an office-spelt publication number, an EP observation's bare number, a
generic submission's target block, and a report's meta plus document stash.
"""
import datetime
import json
import os

import observation_links as links


# ---------------------------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------------------------

def test_both_spellings_of_a_us_pre_grant_number_meet():
    """The office writes US 2026/0070232, DOCDB writes US2026070232: one number, two keys."""
    assert links.pub_keys("US 2026/0070232 A1") == {"US20260070232", "US2026070232"}
    assert links.pub_keys("US-2026070232-A1") == {"US20260070232", "US2026070232"}
    assert links.pub_keys("US20260070232A1") & links.pub_keys("US-2026070232-A1")


def test_kind_codes_and_punctuation_are_not_part_of_the_key():
    assert links.pub_keys("DE 10 2020 129 586 B4") == {"DE102020129586"}
    assert links.pub_keys("EP4446072B1") == links.pub_keys("EP 4 446 072 A1")
    assert links.pub_keys("EP4506111") == {"EP4506111"}
    assert links.pub_keys("") == set()


def test_an_application_number_is_its_digits():
    assert links.app_key("19/318,450") == "19318450"
    assert links.app_key("19318450") == "19318450"
    assert links.app_key("EP26150655") == "EP26150655"


# ---------------------------------------------------------------------------------------------
# packets
# ---------------------------------------------------------------------------------------------

def _store(tmp_path):
    root = tmp_path / "filing"
    us = root / "observations" / "1788575013-obs"
    us.mkdir(parents=True)
    (us / "meta.json").write_text(json.dumps({
        "id": "1788575013-obs", "application_number": "18577905",
        "publication_number": "US 2024/0316792 A1", "title": "VACUUM GRIPPER",
        "items": [{"n": i} for i in range(20)], "files": [{"name": "00_QA_REPORT.md"}],
        "forms": [], "package": {"concise_files": ["a.pdf", "b.pdf"]},
        "status": "handed off", "session": "uspto-observation-18577905",
        "created": 1788575013.8, "signer_name": "X"}))
    demo = root / "observations" / "1788475211-demo"
    demo.mkdir(parents=True)
    (demo / "meta.json").write_text(json.dumps({
        "id": "1788475211-demo", "application_number": "18577905", "demo": True,
        "publication_number": "US 2024/0316792 A1", "items": [1], "created": 1788475211.0}))
    ep = root / "ep_observations" / "ep-1788241089964"
    ep.mkdir(parents=True)
    (ep / "meta.json").write_text(json.dumps({
        "id": "ep-1788241089964", "number": "4506111", "number_kind": "publication",
        "title": "Handling system", "items": [1, 2], "forms": [{"name": "obs.pdf"}],
        "status": "draft", "created": 1788241089.9}))
    de = root / "submissions" / "1788565869-de-opposition"
    de.mkdir(parents=True)
    (de / "meta.json").write_text(json.dumps({
        "id": "1788565869-de-opposition", "submission_type": "de-opposition",
        "number": "DE102020129586B4", "number_kind": "DE",
        "target": {"number": "DE102020129586B4", "kind": "DE"},
        "items": [], "files": [{"name": "x"}],
        "package": {"kind": "generic", "filed": ["01_Einspruchsschrift_59_PatG.pdf", "D1.pdf"],
                    "set_aside": ["00_QA_REPORT.md"]},
        "status": "draft", "created": 1788565869.6}))
    return root


def test_every_store_is_read_and_a_demo_is_marked_as_one(tmp_path):
    rows = links.packages(_store(tmp_path))
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"1788575013-obs", "1788475211-demo", "ep-1788241089964",
                          "1788565869-de-opposition"}
    us = by_id["1788575013-obs"]
    assert us["state"] == "handed off" and us["items"] == 20 and us["concise"] == 2
    assert us["label"] == "US 1.290 submission"
    assert us["url"].endswith("#/sub/us-observation/1788575013-obs")
    assert by_id["1788475211-demo"]["demo"] is True
    assert by_id["ep-1788241089964"]["label"] == "EP Art. 115 observations"
    assert by_id["ep-1788241089964"]["state"] == "built"
    assert by_id["1788565869-de-opposition"]["label"] == "DE Einspruch"
    #  A generic packet counts the papers it will file, not its (empty) reference list.
    assert by_id["1788565869-de-opposition"]["items"] == 2
    assert by_id["1788565869-de-opposition"]["url"].endswith("#/sub/submission/1788565869-de-opposition")
    #  Real packets first, then demos, newest first within each.
    assert [r["demo"] for r in rows] == [False, False, False, True]


def test_a_packet_is_pinned_to_its_own_case_and_never_to_a_sibling(tmp_path):
    rows = links.packages(_store(tmp_path))
    cases = [
        {"publication": "US20240316792A1", "application": "18577905"},
        {"publication": "EP4161743A1", "granted_as": "EP4161743B1"},       # same family, EP
        {"publication": "EP4506111A1"},
        {"publication": "DE102020129586A1", "granted_as": "DE102020129586B4"},
    ]
    links.attach_packages(cases, rows)
    assert [p["id"] for p in cases[0]["packages"]] == ["1788575013-obs", "1788475211-demo"]
    assert cases[0]["package_state"] == "handed off"
    assert cases[1]["packages"] == [] and cases[1]["package_state"] == "none"
    assert [p["id"] for p in cases[2]["packages"]] == ["ep-1788241089964"]
    #  Status says draft, but the package block says the papers exist: that is built.
    assert cases[3]["package_state"] == "built"


def test_a_case_with_only_a_demo_packet_says_demo_not_built(tmp_path):
    root = _store(tmp_path)
    os.remove(root / "observations" / "1788575013-obs" / "meta.json")
    cases = [{"publication": "US20240316792A1", "application": "18577905"}]
    links.attach_packages(cases, links.packages(root))
    assert cases[0]["package_state"] == "demo"


# ---------------------------------------------------------------------------------------------
# searches
# ---------------------------------------------------------------------------------------------

def _reports(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "adhoc-efbf2979420b.meta.json").write_text(json.dumps(
        {"doc_token": "b76426f593d94518aaf40a3a49bb94cf", "subject": None, "mode": "novelty",
         "depth": "deep", "query": "A magnetic gripper..."}))
    (reports / "doc-b76426f593d94518aaf40a3a49bb94cf.json").write_text(json.dumps(
        {"publication_number": "US-20260070232-A1", "title": "Magnetic gripper",
         "source": "link", "chunk_vecs": [[0.1] * 4]}))
    (reports / "adhoc-dff5c10823f0.meta.json").write_text(json.dumps(
        {"doc_token": "", "subject": "US-20260034666-A1", "mode": "novelty"}))
    (reports / "adhoc-plain.meta.json").write_text(json.dumps(
        {"doc_token": "", "subject": None, "query": "typed text"}))
    c = reports / "concise" / "adhoc-efbf2979420b"
    c.mkdir(parents=True)
    for n in ("ConciseDescription_Doc01_GB874600A.pdf", "ConciseDescription_Doc02_X.pdf",
              "ConciseDescription_Doc02_X.docx", "01_DocumentList_and_Statements.pdf"):
        (c / n).write_bytes(b"%PDF")
    return reports


ROWS = [
    {"slug": "adhoc-efbf2979420b", "query": "A magnetic gripper for holding...", "title": None,
     "mode": "novelty", "status": "complete", "updated_at": datetime.datetime(2026, 8, 23, 10)},
    {"slug": "adhoc-dff5c10823f0", "query": "handling system", "title": None,
     "mode": "novelty", "status": "complete", "updated_at": datetime.datetime(2026, 8, 17, 9)},
    {"slug": "adhoc-plain", "query": "typed text", "title": None, "mode": "novelty",
     "status": "complete", "updated_at": datetime.datetime(2026, 8, 1, 9)},
]


def test_a_search_remembers_the_publication_it_was_run_from(tmp_path):
    reports = _reports(tmp_path)
    idx = tmp_path / "index.json"
    out = links.searches_for(4, rows=ROWS, reports=reports, index_path=idx)
    by = {s["slug"]: s for s in out}
    assert by["adhoc-efbf2979420b"]["pub"] == "US-20260070232-A1"
    assert by["adhoc-efbf2979420b"]["concise"] == 2           # PDFs only, the docx is not a filing
    assert by["adhoc-efbf2979420b"]["when"] == "2026-08-23"
    assert by["adhoc-dff5c10823f0"]["pub"] == "US-20260034666-A1"
    assert by["adhoc-plain"]["pub"] == "" and by["adhoc-plain"]["concise"] == 0
    #  The stash was read once and the answer kept: the index now holds it.
    saved = json.loads(idx.read_text())
    assert saved["adhoc-efbf2979420b"]["pub"] == "US-20260070232-A1"
    #  A second pass must not need the stash at all.
    os.remove(reports / "doc-b76426f593d94518aaf40a3a49bb94cf.json")
    out2 = links.searches_for(4, rows=ROWS, reports=reports, index_path=idx)
    assert {s["slug"]: s["pub"] for s in out2}["adhoc-efbf2979420b"] == "US-20260070232-A1"


def test_searches_are_pinned_by_publication_or_by_the_slug_counsel_wrote_down(tmp_path):
    reports = _reports(tmp_path)
    rows = links.searches_for(4, rows=ROWS, reports=reports, index_path=tmp_path / "i.json")
    cases = [
        {"publication": "US20260070232A1", "application": "19318450"},
        {"publication": "US20260034666A1"},
        {"publication": "EP4706914A1", "counsel_report": "adhoc-efbf2979420b (post-fix)"},
        {"publication": "DE102024125824A1"},                     # same family, nothing named
    ]
    links.attach_searches(cases, 4, rows=rows)
    assert [s["slug"] for s in cases[0]["searches"]] == ["adhoc-efbf2979420b"]
    assert cases[0]["search_state"] == "zip"
    assert cases[1]["search_state"] == "done"
    #  Named by counsel on the EP member, so it shows there too, but not on the German one.
    assert [s["slug"] for s in cases[2]["searches"]] == ["adhoc-efbf2979420b"]
    assert cases[3]["searches"] == [] and cases[3]["search_state"] == "none"


def test_summary_counts_real_packets_and_finished_searches():
    cases = [{"package_state": "handed off", "search_state": "zip"},
             {"package_state": "demo", "search_state": "done"},
             {"package_state": "none", "search_state": "none"}]
    assert links.summary(cases) == {"packets": 1, "searched": 2, "zipped": 1}
