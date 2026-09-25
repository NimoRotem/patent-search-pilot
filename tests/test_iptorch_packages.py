"""iptorch.com's packages on the docket: which are copied, when, and which row each one lands on.

Everything runs against temp directories shaped like iptorch's real package store (a concise/<slug>
folder with BUILT.json, PACKET.json, JOB.json and SENT.json, a <slug>.meta.json beside it) and the
filing app's intake folder. The search rows and the zip download are handed in, so no database
and no iptorch process is needed.
"""
import json

import observation_links as links
import iptorch_packages as ip
import observations


def _pkg(root, slug, built_at=None, packet_at=None, job="done", sent=None, pub="EP-4446072-B1",
         instrument="ep_opposition"):
    d = root / "reports" / "concise" / slug
    d.mkdir(parents=True, exist_ok=True)
    if built_at:
        (d / "BUILT.json").write_text(json.dumps({"built_at": built_at, "docs": 7,
                                                  "instrument": instrument}))
    if packet_at:
        (d / "PACKET.json").write_text(json.dumps({"built_at": packet_at, "forum": "US",
                                                   "instrument": instrument, "shipped": 10,
                                                   "label": "Third-party preissuance submission"}))
    (d / "JOB.json").write_text(json.dumps({"state": job}))
    if sent:
        (d / "SENT.json").write_text(json.dumps(sent))
    (root / "reports" / ("%s.meta.json" % slug)).write_text(json.dumps(
        {"input": {"publication_number": pub, "title": "Vacuum gripper"}}))
    return d


def _fetcher(calls):
    def fetch(slug):
        calls.append(slug)
        body = b"PK\x03\x04" + ("%s-%d" % (slug, len(calls))).encode()
        return body, {"x-packet-instrument": "ep_opposition", "x-packet-forum": "EP",
                      "x-packet-qa": "ready", "content-disposition":
                      'attachment; filename="EP4446072B1_ep-opposition.zip"'}
    return fetch


def _sync(tmp_path, rows, calls, active=()):
    return ip.sync(archive=tmp_path / "kept", concise_dir=tmp_path / "reports" / "concise",
                   reports=tmp_path / "reports", filing_data=tmp_path / "filing", searches=rows,
                   fetch=_fetcher(calls), active=set(active))


ROWS = [{"slug": "adhoc-aaa111", "email": "ahmed@intellentpatents.com", "subject": "EP-4446072-B1"}]


# ---------------------------------------------------------------------------------------------
# which builds exist
# ---------------------------------------------------------------------------------------------

def test_a_build_is_dated_by_its_own_clock_the_latest_one_wins(tmp_path):
    d = _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-23T06:52:05Z",
             packet_at="2026-09-20T01:00:00Z")
    b = ip.build_of("adhoc-aaa111", d.parent)
    assert ip._iso(b["built_at"]) == "2026-09-23T06:52:05Z"
    assert b["instrument"] == "ep_opposition"


def test_a_folder_with_no_finished_build_is_not_a_package(tmp_path):
    d = _pkg(tmp_path, "adhoc-aaa111", job="running")
    assert ip.build_of("adhoc-aaa111", d.parent) is None


# ---------------------------------------------------------------------------------------------
# copying them
# ---------------------------------------------------------------------------------------------

def test_each_finished_build_is_copied_once(tmp_path):
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-23T06:52:05Z")
    calls = []
    assert _sync(tmp_path, ROWS, calls)["added"] == 1
    assert _sync(tmp_path, ROWS, calls)["added"] == 0
    assert calls == ["adhoc-aaa111"]
    [v] = ip.versions("adhoc-aaa111", tmp_path / "kept")
    assert v["stamp"] == "20260923T065205Z"
    assert v["account"] == "ahmed@intellentpatents.com"
    assert v["subject"] == "EP-4446072-B1"
    assert (tmp_path / "kept" / "adhoc-aaa111" / v["file"]).read_bytes().startswith(b"PK")


def test_a_rebuild_is_a_second_zip_and_the_first_is_kept(tmp_path):
    """iptorch overwrites a package in place; the docket must still hold the old one."""
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-23T06:52:05Z")
    calls = []
    _sync(tmp_path, ROWS, calls)
    first = (tmp_path / "kept" / "adhoc-aaa111" / "20260923T065205Z.zip").read_bytes()
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-24T10:00:00Z")
    _sync(tmp_path, ROWS, calls)
    vs = ip.versions("adhoc-aaa111", tmp_path / "kept")
    assert [v["stamp"] for v in vs] == ["20260923T065205Z", "20260924T100000Z"]
    assert (tmp_path / "kept" / "adhoc-aaa111" / "20260923T065205Z.zip").read_bytes() == first


def test_a_build_iptorch_is_still_running_is_left_alone(tmp_path):
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-23T06:52:05Z")
    calls = []
    s = _sync(tmp_path, ROWS, calls, active={"adhoc-aaa111"})
    assert s["skipped_running"] == 1 and calls == []


def test_a_package_nobody_on_the_list_owns_is_never_read(tmp_path):
    _pkg(tmp_path, "adhoc-bbb222", built_at="2026-09-23T06:52:05Z")
    calls = []
    assert _sync(tmp_path, ROWS, calls)["seen"] == 0 and calls == []


def test_the_copy_built_before_a_hand_off_is_the_one_marked_sent(tmp_path):
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-23T06:52:05Z")
    calls = []
    _sync(tmp_path, ROWS, calls)
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-23T06:52:05Z",
         sent={"id": "ep-1", "pipeline": "ep-observation", "at": "2026-09-24T09:00:00Z",
               "by": "nimo@grabo.com", "url": "https://rotem.ai/patents/filing/#/sub/ep-observation/ep-1"})
    _sync(tmp_path, ROWS, calls)
    [v] = ip.versions("adhoc-aaa111", tmp_path / "kept")
    assert v["sent"]["id"] == "ep-1" and v["sent"]["by"] == "nimo@grabo.com"


def test_a_hand_off_older_than_every_copy_keeps_the_filing_apps_own_zip(tmp_path):
    """Sent on the 9th, rebuilt on the 11th, before this sync existed: the intake copy is the
    only surviving zip of the version that was handed over."""
    intake = tmp_path / "filing" / "ep_observations" / "ep-9" / "in"
    intake.mkdir(parents=True)
    (intake / "NOT-READY_EP4349543A1.zip").write_bytes(b"PK\x03\x04as-sent")
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-11T11:50:58Z",
         sent={"id": "ep-9", "pipeline": "ep-observation", "at": "2026-09-09T04:52:45Z",
               "by": "nimo@grabo.com"})
    calls = []
    _sync(tmp_path, ROWS, calls)
    _sync(tmp_path, ROWS, calls)
    vs = ip.versions("adhoc-aaa111", tmp_path / "kept")
    assert [v["origin"] for v in vs] == ["handed-copy", "iptorch"]
    assert vs[0]["sent"]["at"] == "2026-09-09T04:52:45Z"
    assert (tmp_path / "kept" / "adhoc-aaa111" / vs[0]["file"]).read_bytes() == b"PK\x03\x04as-sent"


# ---------------------------------------------------------------------------------------------
# onto the docket
# ---------------------------------------------------------------------------------------------

def test_a_package_lands_on_its_row_by_number_and_the_rest_are_listed_apart(tmp_path):
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-23T06:52:05Z", pub="EP-4446072-A1")
    _pkg(tmp_path, "adhoc-ccc333", built_at="2026-09-20T00:00:00Z", pub="US11867422B1")
    rows = ROWS + [{"slug": "adhoc-ccc333", "email": "nimo@grabo.com"}]
    _sync(tmp_path, rows, [])
    kept = ip.kept(tmp_path / "kept")
    cases = [{"publication": "EP4446072B1"}, {"publication": "EP4349543A1"}]
    ip.attach(cases, kept)
    assert [v["slug"] for v in cases[0]["iptorch"]] == ["adhoc-aaa111"]
    assert cases[0]["iptorch"][0]["instrument_label"].startswith("EP opposition")
    assert cases[1]["iptorch"] == []
    keys = links.pub_keys("EP4446072B1") | links.pub_keys("EP4349543A1")
    assert [v["slug"] for v in ip.unmatched(keys, kept)] == ["adhoc-ccc333"]


def test_a_download_path_cannot_leave_the_archive(tmp_path):
    _pkg(tmp_path, "adhoc-aaa111", built_at="2026-09-23T06:52:05Z")
    _sync(tmp_path, ROWS, [])
    assert ip.zip_path("adhoc-aaa111", "20260923T065205Z", tmp_path / "kept")
    assert ip.zip_path("..", "20260923T065205Z", tmp_path / "kept") is None
    assert ip.zip_path("adhoc-aaa111", "../../x", tmp_path / "kept") is None
    assert ip.zip_path("adhoc-aaa111", "20990101T000000Z", tmp_path / "kept") is None


def test_only_the_named_docket_owner_sees_them():
    assert ip.visible_to({"email": ip.DOCKET_OWNERS[0].upper()})
    assert not ip.visible_to({"email": "someone@example.com"})
    assert not ip.visible_to(None)


# ---------------------------------------------------------------------------------------------
# the three stages
# ---------------------------------------------------------------------------------------------

def _case(**kw):
    c = {"publication": "US20250026001A1", "office": "USPTO", "packages": [], "searches": [],
         "iptorch": [], "on_file": [], "our_filings": []}
    c.update(kw)
    return c


def test_a_built_package_is_stage_one_and_nothing_more():
    c = _case(iptorch=[{"built_at": "2026-09-19T05:06:03Z", "stamp": "x"}])
    observations.stages([c])
    assert c["stage"] == "built" and c["built_count"] == 1
    assert c["built_latest"] == "2026-09-19"
    assert c["submitted"] == [] and c["public"] == []


def test_our_own_receipt_is_stage_two_and_only_the_office_makes_stage_three():
    c = _case(on_file=[{"date": "2026-09-05", "whose": "ours", "origin": "receipt"}],
              our_filings=[{"status": "filed", "filed_on": "2026-09-05", "route_label": "1.290",
                            "package": "OBS.zip"}])
    observations.stages([c], have={"OBS.zip"})
    assert c["stage"] == "submitted"
    assert c["submitted"][0]["package"] == "OBS.zip"
    assert c["public"] == []
    c["on_file"].append({"date": "2026-09-05", "whose": "ours", "origin": "office",
                         "office_docs": [{"code": "IDS.3P", "description": "Third-Party Submission"}]})
    observations.stages([c], have={"OBS.zip"})
    assert c["stage"] == "public"
    assert c["public"][0]["office_docs"][0]["code"] == "IDS.3P"


def test_a_package_sent_to_the_filing_app_and_never_filed_says_so():
    c = _case(iptorch=[{"built_at": "2026-09-11T11:50:58Z", "stamp": "s",
                        "sent": {"id": "ep-9", "at": "2026-09-09T04:52:45Z", "by": "nimo@grabo.com"}}],
              packages=[{"id": "ep-9", "state": "built", "demo": False}])
    observations.stages([c])
    assert c["stage"] == "handed"
    [s] = c["submitted"]
    assert s["state"] == "handed" and s["date"] == "2026-09-09"
    assert "built" in s["evidence"]


def test_a_paper_somebody_else_filed_is_on_the_record_but_not_our_stage_three():
    c = _case(on_file=[{"date": "2026-07-01", "whose": "unknown", "origin": "office"}])
    observations.stages([c])
    assert c["stage"] == "none" and len(c["public"]) == 1
