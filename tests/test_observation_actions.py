"""The instrument table: which door is open against one case, at one office, on one day.

The arithmetic here is the whole point of the feature, so it is pinned rather than eyeballed. The
cases that earn a test are the ones where the rule is counter-intuitive: 1.290 closing on the
LATER of two dates, a protest closing EARLIER than the submission it is usually confused with,
Sec. 44(2) expiring seven years after filing, and the nine months of an opposition running from a
publication rather than from a decision.
"""
import datetime

import pytest

import observation_actions as acts


TODAY = datetime.date(2026, 9, 4)


def stages(entries):
    return {e["stage"]: e for e in entries}


# ---------------------------------------------------------------------------------------------
# the shape of the answer
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("office", ["USPTO", "EPO", "DPMA", "WIPO (PCT)"])
def test_every_office_answers_every_stage(office):
    got = acts.actions_for({"office": office, "posture": "pending"}, TODAY)
    assert stages(got).keys() == {s for s, _ in acts.STAGES}


def test_an_unknown_office_is_empty_not_an_exception():
    assert acts.actions_for({"office": "JPO", "posture": "pending"}, TODAY) == []


def test_the_most_actionable_entry_sorts_first():
    row = {"office": "EPO", "posture": "granted", "grant_published": "2026-06-01"}
    got = acts.actions_for(row, TODAY)
    assert got[0]["stage"] == "post_grant_now"
    assert got[0]["status"] in ("open", "closing")


# ---------------------------------------------------------------------------------------------
# United States
# ---------------------------------------------------------------------------------------------

def test_1290_closes_on_the_later_of_six_months_and_the_first_rejection():
    #  Published Feb, so six months is August; the rejection came in October. "Later" means the
    #  window is still open in September, which is the reading that keeps catching people out.
    row = {"office": "USPTO", "posture": "pending", "pubDate": "2026-02-10",
           "six_months": "2026-08-10", "first_rejection": "2026-10-01"}
    a = stages(acts.actions_for(row, TODAY))["pre_grant_passive"]
    assert a["deadline"] == "2026-10-01"
    assert a["status"] in ("open", "closing")
    assert a["days_left"] == 27


def test_1290_with_no_rejection_yet_runs_past_the_six_month_date():
    row = {"office": "USPTO", "posture": "pending", "pubDate": "2026-04-23",
           "six_months": "2026-10-23"}
    a = stages(acts.actions_for(row, TODAY))["pre_grant_passive"]
    assert a["status"] == "open"
    assert a["deadline"] == "2026-10-23"
    assert "notice of allowance would shut it" in a["note"]


def test_a_six_month_date_gone_by_with_no_rejection_is_still_open():
    """The reading that costs a window. 1.290(b) closes on the LATER of the six-month date and
    the first rejection, so a passed six-month date with no rejection closes nothing at all."""
    row = {"office": "USPTO", "posture": "pending", "pubDate": "2026-01-05",
           "six_months": "2026-07-05"}
    a = stages(acts.actions_for(row, TODAY))["pre_grant_passive"]
    assert a["status"] == "open"
    assert a["deadline"] is None            # no countdown, because the rule has no date yet
    assert "still open" in a["note"]


def test_a_notice_of_allowance_shuts_1290_whatever_the_dates_say():
    row = {"office": "USPTO", "posture": "pending", "pubDate": "2026-01-05",
           "six_months": "2026-07-05", "allowance": "2026-08-20"}
    a = stages(acts.actions_for(row, TODAY))["pre_grant_passive"]
    assert a["status"] == "closed"


def test_a_lapsed_window_reads_closed_and_says_why():
    row = {"office": "USPTO", "posture": "pending", "pubDate": "2025-06-01",
           "six_months": "2025-12-01", "first_rejection": "2026-07-27"}
    a = stages(acts.actions_for(row, TODAY))["pre_grant_passive"]
    assert a["status"] == "closed"
    assert a["days_left"] < 0


def test_a_published_application_makes_1291_conditional_not_open():
    row = {"office": "USPTO", "posture": "pending", "pubDate": "2026-04-23"}
    a = stages(acts.actions_for(row, TODAY))["pre_grant_protest"]
    assert a["status"] == "conditional"
    assert "written consent" in a["note"]


def test_section_301_opens_only_once_the_patent_issues():
    pending = stages(acts.actions_for({"office": "USPTO", "posture": "pending"}, TODAY))
    granted = stages(acts.actions_for(
        {"office": "USPTO", "posture": "granted", "grant_date": "2026-08-01",
         "priority_date": "2022-01-01"}, TODAY))
    assert pending["post_grant_passive"]["status"] == "not_yet"
    assert granted["post_grant_passive"]["status"] == "open"
    assert granted["post_grant_passive"]["fee"] == "$0"


def test_pgr_runs_nine_months_from_issue_and_ipr_opens_when_it_shuts():
    row = {"office": "USPTO", "posture": "granted", "grant_date": "2026-08-01",
           "priority_date": "2022-01-01", "patent_number": "12345678"}
    got = stages(acts.actions_for(row, TODAY))
    assert got["post_grant_now"]["deadline"] == "2027-05-01"
    assert got["post_grant_now"]["status"] == "open"
    assert got["post_grant_later"]["opens"] == "2027-05-01"
    assert got["post_grant_later"]["status"] == "not_yet"


def test_a_pre_aia_patent_is_not_eligible_for_post_grant_review():
    row = {"office": "USPTO", "posture": "granted", "grant_date": "2026-08-01",
           "priority_date": "2011-05-04", "patent_number": "12345678"}
    got = stages(acts.actions_for(row, TODAY))
    assert got["post_grant_now"]["status"] == "na"
    assert got["post_grant_later"]["status"] == "open"


def test_the_us_has_no_way_to_force_examination():
    got = stages(acts.actions_for({"office": "USPTO", "posture": "pending"}, TODAY))
    assert got["force_exam"]["status"] == "na"


# ---------------------------------------------------------------------------------------------
# EPO
# ---------------------------------------------------------------------------------------------

def test_art_115_is_open_with_no_deadline_while_examination_runs():
    a = stages(acts.actions_for({"office": "EPO", "posture": "pending"}, TODAY))
    assert a["pre_grant_passive"]["status"] == "open"
    assert a["pre_grant_passive"]["deadline"] is None
    assert a["pre_grant_passive"]["fee"] == "€0"


def test_an_intention_to_grant_downgrades_art_115_to_check_first():
    row = {"office": "EPO", "posture": "pending", "closing_soon": True,
           "register_status": "Grant of patent is intended"}
    a = stages(acts.actions_for(row, TODAY))["pre_grant_passive"]
    assert a["status"] == "conditional"
    assert "Rule 71(3)" in a["note"]


def test_opposition_runs_nine_months_from_the_mention_of_grant():
    row = {"office": "EPO", "posture": "granted", "grant_published": "2025-12-03"}
    a = stages(acts.actions_for(row, TODAY))["post_grant_now"]
    assert a["deadline"] == "2026-09-03"
    #  One day past. The row that proves the countdown is computed and not remembered.
    assert a["status"] == "closed"
    assert a["days_left"] == -1


def test_an_opposition_still_inside_its_nine_months_is_open():
    row = {"office": "EPO", "posture": "granted", "grant_published": "2025-12-31"}
    a = stages(acts.actions_for(row, TODAY))["post_grant_now"]
    assert a["deadline"] == "2026-09-30"
    assert a["status"] == "closing"          # 26 days left, so it is flagged, not merely open
    assert a["fee"] == "€880"


def test_a_baseline_row_with_only_a_deadline_still_finds_its_opposition():
    """What every row looks like before the first refresh: the nine months are already computed
    into `deadline` and the grant date the module would rather use is simply absent."""
    row = {"office": "EPO", "posture": "granted", "deadline": "2026-09-30",
           "deadline_kind": "hard"}
    head = acts.headline(row, TODAY)
    assert head["label"] == "Opposition"
    assert head["days_left"] == 26
    de = {"office": "DPMA", "posture": "granted", "deadline": "2026-09-18",
          "deadline_kind": "hard"}
    assert acts.headline(de, TODAY)["label"] == "Einspruch"


def test_art_105_intervention_needs_a_pending_opposition():
    without = stages(acts.actions_for(
        {"office": "EPO", "posture": "granted", "grant_published": "2025-12-31"}, TODAY))
    with_opp = stages(acts.actions_for(
        {"office": "EPO", "posture": "granted", "grant_published": "2024-01-01",
         "opposition_pending": True}, TODAY))
    assert without["post_grant_later"]["status"] == "conditional"
    assert with_opp["post_grant_later"]["status"] == "open"


def test_post_grant_art_115_is_only_worth_it_with_a_proceeding_pending():
    row = {"office": "EPO", "posture": "granted", "grant_published": "2024-01-01"}
    a = stages(acts.actions_for(row, TODAY))["post_grant_passive"]
    assert a["status"] == "conditional"
    assert "opposition" in a["note"]


# ---------------------------------------------------------------------------------------------
# DPMA
# ---------------------------------------------------------------------------------------------

def test_a_third_party_can_force_german_examination_within_seven_years():
    row = {"office": "DPMA", "posture": "pending", "filing_date": "2023-06-01",
           "exam_requested": False}
    a = stages(acts.actions_for(row, TODAY))["force_exam"]
    assert a["status"] == "open"
    assert a["deadline"] == "2030-06-01"
    assert a["fee"] == "€350, or €150 after a § 43 search request"


def test_forcing_examination_is_spent_once_it_has_been_requested():
    row = {"office": "DPMA", "posture": "pending", "filing_date": "2023-06-01",
           "exam_requested": True}
    assert stages(acts.actions_for(row, TODAY))["force_exam"]["status"] == "closed"


def test_forcing_examination_is_spent_seven_years_after_filing():
    row = {"office": "DPMA", "posture": "pending", "filing_date": "2016-01-04",
           "exam_requested": False}
    assert stages(acts.actions_for(row, TODAY))["force_exam"]["status"] == "closed"


def test_an_unknown_examination_request_says_go_and_look():
    row = {"office": "DPMA", "posture": "pending", "filing_date": "2023-06-01"}
    a = stages(acts.actions_for(row, TODAY))["force_exam"]
    assert a["status"] == "conditional"
    assert "DPMAregister" in a["note"]


def test_einspruch_runs_nine_months_from_the_patentschrift():
    row = {"office": "DPMA", "posture": "granted", "grant_published": "2026-08-20"}
    a = stages(acts.actions_for(row, TODAY))["post_grant_now"]
    assert a["deadline"] == "2027-05-20"
    assert a["status"] == "open"
    assert a["fee"] == "€200"


def test_a_scheduled_grant_shuts_43_3_in_practice_and_names_the_opposition_window():
    row = {"office": "DPMA", "posture": "pending", "scheduled_grant": "2026-10-01"}
    got = stages(acts.actions_for(row, TODAY))
    assert got["pre_grant_passive"]["status"] == "conditional"
    assert got["post_grant_now"]["opens"] == "2026-10-01"
    assert "2027-07-01" in got["post_grant_now"]["note"]


def test_germany_has_no_passive_post_grant_route():
    row = {"office": "DPMA", "posture": "granted", "grant_published": "2026-08-20"}
    assert stages(acts.actions_for(row, TODAY))["post_grant_passive"]["status"] == "na"


# ---------------------------------------------------------------------------------------------
# the one-line answer the docket row carries
# ---------------------------------------------------------------------------------------------

def test_the_headline_is_the_soonest_thing_that_can_be_filed_today():
    row = {"office": "EPO", "posture": "granted", "grant_published": "2025-12-31"}
    head = acts.headline(row, TODAY)
    assert head["label"] == "Opposition"
    assert head["status"] == "closing"
    assert head["deadline"] == "2026-09-30"


def test_a_case_with_nothing_open_falls_back_to_what_needs_checking():
    row = {"office": "EPO", "posture": "granted", "grant_published": "2024-01-01"}
    head = acts.headline(row, TODAY)
    assert head["status"] == "conditional"


def test_a_dead_case_has_no_headline_at_all():
    row = {"office": "USPTO", "posture": "lapsed", "register_status": "Abandoned"}
    assert acts.headline(row, TODAY) is None


def test_plus_months_lands_on_the_month_end_rather_than_overflowing():
    assert acts.plus_months(datetime.date(2026, 5, 31), 9) == datetime.date(2027, 2, 28)
    assert acts.plus_months(datetime.date(2023, 5, 31), 9) == datetime.date(2024, 2, 29)
