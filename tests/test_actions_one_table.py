"""One table: the action list, the filings and the missed windows live on the docket's rows now.

Everything here is plain dicts; no database, no board file (load_board is stubbed).
"""
import observations as obs


BOARD = {"title": "The eleven to act on", "lede": "Built 2026-09-09", "entries": [
    {"n": "1", "title": "DE 10 2021 119 687 B4 opposed. Confirm the fee", "due": "2026-10-18",
     "state": "filed", "state_label": "Faxed 2026-09-18 23:44:47, transmission ended 00:01:15",
     "cost": "EUR 200", "what": "Einspruch", "pubs": ["DE102021119687B4"], "links": [],
     "package": "DE-EINSPRUCH.zip"},
    {"n": "3", "title": "Draft two Einsprueche", "due": "2026-09-18", "state": "nothing",
     "state_label": "Nothing drafted", "pubs": ["DE102021119687B4", "DE102022111527B4"], "links": []},
    {"n": "9", "title": "Point the German feed at DPMAregister", "due": "", "state": "monitor",
     "state_label": "Watching", "pubs": [], "links": []},
]}


def _cases():
    return [{"publication": "DE102021119687B4", "office": "DPMA", "application": "102021119687"},
            {"publication": "DE102022111527B4", "office": "DPMA"},
            {"publication": "US20260034666A1", "office": "USPTO", "application": "19289212"},
            {"publication": "US20260109053A1", "office": "USPTO", "application": "19315746"}]


def test_every_action_lands_on_every_row_it_names(monkeypatch):
    monkeypatch.setattr(obs, "load_board", lambda: BOARD)
    cases = _cases()
    board = obs.board_for(cases, have={"DE-EINSPRUCH.zip"})
    de = cases[0]
    assert [b["n"] for b in de["boards"]] == ["1", "3"]
    assert de["board"]["n"] == "1"                         # the chip and the filter keep the first
    assert [b["n"] for b in cases[1]["boards"]] == ["3"]
    assert de["boards"][0]["filed_date"] == "2026-09-18"
    assert de["boards"][0]["cost"] == "EUR 200" and de["boards"][0]["package_available"]
    assert [e["on_docket"] for e in board["entries"]] == [1, 2, 0]


def test_an_action_that_says_filed_is_stage_two_on_its_row(monkeypatch):
    monkeypatch.setattr(obs, "load_board", lambda: BOARD)
    cases = _cases()
    obs.board_for(cases, have={"DE-EINSPRUCH.zip"})
    obs.stages(cases, have={"DE-EINSPRUCH.zip"})
    [sub] = cases[0]["submitted"]
    assert sub["state"] == "filed" and sub["date"] == "2026-09-18"
    assert sub["source"] == "the action list" and sub["package"] == "DE-EINSPRUCH.zip"
    assert cases[0]["stage"] == "submitted"
    #  An opposition is on the German register, so the row is not written off as invisible.
    assert cases[0]["office_blind"] == ""
    assert cases[1]["submitted"] == []                     # action 3 is work, not a filing


def test_a_filing_carries_its_whole_record_and_a_prepared_one_is_kept_apart():
    filed = {"id": "us-19315746-tps", "target": "US20260109053A1", "application": "19315746",
             "status": "filed", "filed_on": "2026-08-02", "route_label": "37 CFR 1.290",
             "official_fee_usd": 156, "counsel_fee_usd": 0, "references": 10,
             "references_list": [{"number": "US 1"}], "receipts": ["81314946"],
             "public": {"state": "published", "checked": "2026-09-09"}, "detail": "What went in"}
    held = {"id": "us-x-held", "target": "US20260109053A1", "status": "prepared_not_filed",
            "route_label": "37 CFR 1.290", "window_was": "2026-10-23"}
    cases = _cases()
    obs.attribute_filings(cases, [filed, held])
    obs.stages(cases, have=())
    row = cases[3]
    [sub] = row["submitted"]
    assert sub["filing"]["official_fee_usd"] == 156
    assert sub["filing"]["references_list"] == [{"number": "US 1"}]
    assert sub["filing"]["receipts"] == ["81314946"] and sub["receipts"] == 1
    assert sub["filing"]["public"]["state"] == "published"
    assert [f["id"] for f in row["not_filed"]] == ["us-x-held"]


def test_a_missed_window_goes_on_its_row_by_application_number():
    missed = [{"target": "US20260034666A1", "application": "19289212", "window_closed": "2026-08-05",
               "route_label": "37 CFR 1.290", "detail": "counsel never answered"}]
    cases = obs.attach_missed(_cases(), missed)
    assert cases[2]["missed"][0]["detail"] == "counsel never answered"
    assert all(not c["missed"] for i, c in enumerate(cases) if i != 2)


def test_what_has_no_row_becomes_a_marked_row_in_the_same_table(monkeypatch):
    monkeypatch.setattr(obs, "load_board", lambda: BOARD)
    nguyen = {"id": "us-17724791-tps-2023", "target": "US20220331993A1", "application": "17724791",
              "title": "Portable vacuum gripper", "target_owner": "Nhon Hoa Nguyen", "office": "USPTO",
              "status": "filed", "filed_on": "2023-04-01", "route_label": "37 CFR 1.290",
              "public": {"state": "published", "url": "https://example.test/ifw"}}
    cases = _cases()
    obs.attribute_filings(cases, [nguyen])
    board = obs.board_for(cases, have=set())
    extras = obs.extra_rows(cases, [nguyen], [], board)
    assert [e["extra"] for e in extras] == ["filing", "board"]
    obs.stages(cases + extras, have=())
    filing_row, board_row = extras
    assert filing_row["publication"] == "US20220331993A1" and filing_row["stage"] == "public"
    assert filing_row["submitted"][0]["filing"]["id"] == "us-17724791-tps-2023"
    assert board_row["publication"] == "action-9" and board_row["boards"][0]["title"].startswith("Point")
    #  Every extra row can be rendered from DETAIL_FIELDS alone.
    for e in extras:
        assert {"extra", "extra_note", "boards"} <= set(obs.DETAIL_FIELDS)
        assert e["extra_note"]
