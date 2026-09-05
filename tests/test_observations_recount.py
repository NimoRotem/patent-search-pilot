"""The countdown and the urgency band, which are derived on every read and never stored.

`recount` is where three things that used to disagree are reconciled: the date the last sweep
stored, the instrument that is actually open, and the register's own view of whether the case is
alive at all. Each of the tests below is a row that appeared on the live docket on 2026-09-04 and
was rendered wrongly by an earlier version of this function.
"""
import datetime

import observations


TODAY = datetime.date(2026, 9, 4)


def build(**row):
    """One row, taken through exactly the path `cases_for` takes it through."""
    import observation_actions
    row.setdefault("office", "USPTO")
    row["actions"] = observation_actions.actions_for(row, TODAY)
    row["action_headline"] = observation_actions.headline(row, TODAY)
    return observations.recount(row, TODAY)


def test_the_countdown_is_computed_from_today_not_remembered():
    row = build(office="EPO", posture="granted", deadline="2026-09-30", deadline_kind="hard",
                days_left=999, sort_key=999)
    assert row["days_left"] == 26
    assert row["state"] == "closing"
    assert row["sort_key"] == 26


def test_the_row_takes_its_window_from_the_instrument_that_is_open():
    """US 2024/0316792: the stored deadline was the Ex parte Quayle date, which the table printed
    as "closed, 9 days ago" beside a 1.290 window the same page reported as open."""
    row = build(posture="pending", pubDate="2024-09-26", six_months="2025-03-26",
                quayle="2026-06-26", deadline="2026-08-26", deadline_kind="open_ended",
                closing_soon=True)
    assert row["action_headline"]["status"] == "open"
    assert row["deadline"] is None
    assert row["days_left"] is None
    assert row["state"] == "closing"
    #  Undated but shutting: immediately after anything that closes today, not at the very end.
    assert row["sort_key"] == 1


def test_an_ordinary_undated_window_is_not_flagged_urgent():
    row = build(office="EPO", posture="pending")
    assert row["action_headline"]["status"] == "open"
    assert row["state"] == "open"
    assert row["sort_key"] == 9999


def test_a_refused_case_is_lapsed_even_though_no_date_passed():
    row = build(office="DPMA", posture="lapsed", closing_soon=True,
                register_status="zurückgewiesen, rechtskräftig seit 2026-08-05")
    assert row["state"] == "lapsed"
    assert row["sort_key"] == 9999
    assert observations.observation_actions.headline(row, TODAY) is None


def test_a_closed_window_keeps_the_date_it_closed_on():
    row = build(office="EPO", posture="granted", deadline="2026-09-03", deadline_kind="hard")
    assert row["deadline"] == "2026-09-03"
    assert row["days_left"] == -1
    assert row["state"] == "lapsed"


# ---------------------------------------------------------------------------------------------
# whose paper is already on the file
# ---------------------------------------------------------------------------------------------

def test_a_submission_is_only_called_ours_when_our_own_record_names_the_target():
    cases = [
        {"publication": "US20260109053A1", "application": "19315746",
         "our_submissions": [{"date": "2026-08-02", "instrument": "1.290"}]},
        {"publication": "EP4054810A1",
         "file_events": [{"date": "2026-07-01", "instrument": "Observations",
                          "whose": "unknown"}]},
    ]
    observations.attribute_filings(cases, [{"target": "US20260109053A1",
                                            "application": "19/315,746"}])
    assert cases[0]["on_file"][0]["whose"] == "ours"
    #  Nothing in our own record names this one, and Art. 115 observations may be anonymous.
    assert cases[1]["on_file"][0]["whose"] == "unknown"


def test_the_two_lists_are_merged_newest_first():
    case = {"publication": "X",
            "our_submissions": [{"date": "2026-01-01", "instrument": "a"}],
            "file_events": [{"date": "2026-08-01", "instrument": "b", "whose": "unknown"}]}
    observations.attribute_filings([case], [])
    assert [e["instrument"] for e in case["on_file"]] == ["b", "a"]


def test_the_register_history_reaches_the_page():
    """It was being fetched from INPADOC and stored on 61 German rows and shown to nobody, so
    every German deadline on the page was an assertion with its evidence left in the database."""
    assert "register_events" in observations.DETAIL_FIELDS


# ---------------------------------------------------------------------------------------------
# the filter of what can be filed, and which filings belong on which docket
# ---------------------------------------------------------------------------------------------

def test_the_can_file_filter_lists_each_open_instrument_once_with_its_count():
    """The filter names the instrument the way the row's own table does, "Immediate post-grant
    challenge: Einspruch (§ 59(1), § 21 PatG)", and counts the rows it is open on."""
    rows = [build(office="DPMA", posture="granted", grant_published="2026-08-20"),
            build(office="DPMA", posture="granted", grant_published="2026-07-01"),
            build(office="EPO", posture="pending")]
    opts = observations.can_file_options(rows)
    einspruch = [o for o in opts if o["instrument"] == "Einspruch"]
    assert len(einspruch) == 1
    assert einspruch[0]["group"] == "open"
    assert einspruch[0]["count"] == 2
    assert einspruch[0]["stage_label"] == "Immediate post-grant challenge"
    assert einspruch[0]["statute"] == "§ 59(1), § 21 PatG"
    #  Each row answers to the keys of the instruments open on it, and to nothing else.
    assert einspruch[0]["key"] in rows[0]["can_keys"]
    assert einspruch[0]["key"] not in rows[2]["can_keys"]
    art115 = [o for o in opts if o["instrument"] == "Third-party observations"]
    assert art115 and art115[0]["count"] == 1 and art115[0]["group"] == "open"


def test_a_weak_instrument_is_left_out_of_the_filter():
    """Post-grant Art. 115 observations with nothing pending are filed and never read. They stay
    on the row's own table and must not pad the filter."""
    row = build(office="EPO", posture="granted", grant_published="2024-01-01")
    opts = observations.can_file_options([row])
    assert not any(o["stage"] == "post_grant_passive" for o in opts)
    #  The Art. 105 intervention is conditional and weak on the same row: not offered either.
    assert not any(o["stage"] == "post_grant_later" for o in opts)


def test_open_instruments_sort_before_the_ones_that_need_checking():
    rows = [build(office="DPMA", posture="pending", filing_date="2020-01-01"),
            build(office="DPMA", posture="granted", grant_published="2026-08-20")]
    opts = observations.can_file_options(rows)
    groups = [o["group"] for o in opts]
    assert groups == sorted(groups, key=lambda g: {"open": 0, "check": 1}[g])
    assert "check" in groups            # the § 44(2) request with exam_requested unknown


def test_only_the_filings_that_name_a_row_belong_on_another_target():
    cases = [{"publication": "US20260109053A1", "application": "19315746"}]
    filings = [{"target": "US20260109053A1", "application": "19/315,746"},
               {"target": "US20250033224A1", "application": "18/915,337"}]
    assert observations.filings_on(cases, filings) == filings[:1]
    #  The shipped docket shows all of them: one names a parent application, not a row.
    assert observations.filings_on(cases, filings, everything=True) == filings


def test_names_are_cleaned_deduplicated_and_capped():
    got = observations._clean_names("Festo SE & Co. KG\n  festo se & co. kg \n\nFesto AG\x00")
    assert got == ["Festo SE & Co. KG", "Festo AG"]
    assert observations._clean_names(["x"] * 50) == ["x"]
    assert len(observations._clean_names(["n%d" % i for i in range(50)])) == observations.MAX_NAMES


def test_offices_and_lookback_fall_back_to_the_defaults():
    assert observations._clean_offices(["us", "nope", "EP"]) == ["EP", "US"]
    assert observations._clean_offices([]) == list(observations.OFFICE_CODES)
    assert observations._clean_lookback("24") == 24
    assert observations._clean_lookback("7") == observations.DEFAULT_LOOKBACK
    assert observations._clean_lookback(None) == observations.DEFAULT_LOOKBACK


def test_dates_are_printed_in_one_spelling_whatever_office_they_came_from():
    row = observations._tidy_dates({"filing_date": "20241119", "pubDate": "2026-05-20",
                                    "grant_date": "not a date"})
    assert row["filing_date"] == "2024-11-19"
    assert row["pubDate"] == "2026-05-20"
    assert row["grant_date"] == "not a date"


def test_people_stored_as_prose_become_names_without_addresses():
    """The shipped German rows carry inventors as one string, and joining a string joins its
    letters: the table printed "S, t, o, c, k" under a title."""
    got = observations._people("Stockburger, Ralf, 72293 Glatten, DE; Hofer, Frank, 72172 Sulz, DE")
    assert got == ["Stockburger, Ralf", "Hofer, Frank"]
    assert observations._people(["Valentin Stegmaier"]) == ["Valentin Stegmaier"]
    assert observations._people("TRUMPF Werkzeugmaschinen SE + Co. KG | J. Schmalz GmbH") == [
        "TRUMPF Werkzeugmaschinen SE + Co. KG", "J. Schmalz GmbH"]
    assert observations._people(None) == []
    row = observations._tidy_dates({"applicant": "J.Schmalz GmbH, 72293 Glatten, DE",
                                    "inventors": "Stockburger, Ralf, 72293 Glatten, DE",
                                    "ipc": "B25B 11/00 (2006.01)"})
    assert row["applicant_short"] == "J.Schmalz GmbH"
    assert row["inventors"] == ["Stockburger, Ralf"]
    assert row["ipc"] == "B25B 11/00"
