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
