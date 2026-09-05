"""Designs and trademarks: the instrument tables and the rows the sources become.

The fixtures are trimmed captures of what TMview, the EUIPO design API and the USPTO Open Data
Portal actually returned on 2026-09-05, so what is pinned is the wire format, not the brochure.
"""
import datetime

import observation_marks as marks

TODAY = datetime.date(2026, 9, 5)


# ---------------------------------------------------------------------------------------------
# trademarks
# ---------------------------------------------------------------------------------------------

def test_a_published_eu_application_is_open_to_observations_and_opposition():
    row = {"kind": "trademark", "office": "EUIPO", "office_code": "EM", "posture": "pending",
           "opposition_start": "2026-08-20"}
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    assert acts["pre_reg_observation"]["status"] == "open" and acts["pre_reg_observation"]["fee"] == "€0"
    assert acts["opposition"]["status"] == "open"
    assert acts["opposition"]["deadline"] == "2026-11-20"          # three months from publication
    assert acts["cancellation"]["status"] == "not_yet"


def test_a_registered_eu_mark_is_only_open_to_cancellation():
    row = {"kind": "trademark", "office": "EUIPO", "office_code": "EM", "posture": "registered",
           "opposition_start": "2020-07-09", "opposition_end": "2020-10-13"}
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    assert acts["pre_reg_observation"]["status"] == "closed"
    assert acts["opposition"]["status"] == "closed"
    assert acts["cancellation"]["status"] == "open" and "€630" in acts["cancellation"]["fee"]
    assert marks.headline(row, TODAY)["label"].startswith("Application for a declaration")


def test_a_us_application_published_for_opposition_has_thirty_days():
    row = {"kind": "trademark", "office": "USPTO", "office_code": "US", "posture": "pending",
           "publication_date": "2026-08-25"}
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    #  Nineteen days out is inside the thirty-day "closing" band the table colours by.
    assert acts["opposition"]["status"] == "closing" and acts["opposition"]["deadline"] == "2026-09-24"
    #  A letter of protest is still considered for thirty days after publication.
    assert acts["pre_reg_observation"]["status"] == "closing" and acts["pre_reg_observation"]["deadline"] == "2026-09-24"


def test_an_unpublished_us_application_takes_a_letter_of_protest_with_no_date():
    row = {"kind": "trademark", "office": "USPTO", "office_code": "US", "posture": "pending"}
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    assert acts["pre_reg_observation"]["status"] == "open" and acts["pre_reg_observation"]["deadline"] is None
    assert acts["opposition"]["status"] == "not_yet"


def test_a_german_registration_opens_the_widerspruch_on_publication():
    row = {"kind": "trademark", "office": "DPMA", "office_code": "DE", "posture": "registered",
           "registration_date": "2026-07-15"}
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    assert acts["opposition"]["status"] == "open" and acts["opposition"]["deadline"] == "2026-10-15"
    assert acts["cancellation"]["status"] == "open"


def test_an_ended_mark_has_nothing_left():
    row = {"kind": "trademark", "office": "USPTO", "office_code": "US", "posture": "lapsed"}
    assert all(a["status"] == "na" for a in marks.actions_for(row, TODAY))
    assert marks.headline(row, TODAY) is None


def test_posture_is_read_off_the_office_status_words():
    assert marks._tm_posture("Registered") == "registered"
    assert marks._tm_posture("Filed") == "pending"
    assert marks._tm_posture("Ended") == "lapsed"
    assert marks._tm_posture("Application published") == "pending"


TMVIEW_HIT = {"ST13": "US500000075354221", "tmName": "SCHMALZ", "tmOffice": "US",
              "tmOfficeURL": "http://tsdr.uspto.gov/#caseNumber=75354221",
              "applicationNumber": "75354221", "registrationNumber": "2525379",
              "tradeMarkStatus": "Registered", "niceClass": [7], "applicantName": ["J. Schmalz GmbH"],
              "applicationDate": "1997-09-09T12:00:00.000Z", "tradeMarkType": "Word",
              "registrationDate": "2002-01-01T12:00:00.000Z",
              "markImageURI": "https://www.tmdn.org/tmview/api/trademark/image/US500000075354221"}


def test_a_tmview_hit_becomes_a_docket_row_keyed_on_its_st13():
    row = marks.tm_row(TMVIEW_HIT, "Schmalz")
    assert row["kind"] == "trademark" and row["publication"] == "US500000075354221"
    assert row["office"] == "USPTO" and row["office_code"] == "US"
    assert row["title"] == "SCHMALZ" and row["applicant"] == "J. Schmalz GmbH"
    assert row["posture"] == "registered" and row["filing_date"] == "1997-09-09"
    assert row["classes"] == ["7"] and row["registration"] == "2525379"


TMVIEW_DETAIL = {"ST13": "EM500000018228224", "officeUrl": "https://euipo.europa.eu/eSearch/#details/trademarks/018228224",
                 "officeLastUpdateDate": "2026-09-05T00:00:00.000Z",
                 "applicants": [{"fullName": "GRABO LTD"}],
                 "publication": [{"identifier": "2020/128", "section": "A.1", "date": "2020-07-09T00:00:00.000Z"},
                                 {"identifier": "2020/200", "section": "B.2", "date": "2020-10-20T00:00:00.000Z"}],
                 "oppositions": [], "cancellations": [],
                 "tradeMark": {"markCurrentStatusCode": "Registered", "markCurrentStatusDate": "2020-10-20T00:00:00.000Z",
                               "applicationDate": "2020-04-22T00:00:00.000Z", "codeRegistrationDate": "2020-10-17T00:00:00.000Z",
                               "expiryDate": "2030-04-22T00:00:00.000Z", "niceClass": "7, 9",
                               "oppositionPeriodStartDate": "2020-07-09T00:00:00.000Z",
                               "oppositionPeriodEndDate": "2020-10-13T00:00:00.000Z"}}


def test_the_detail_record_supplies_the_opposition_period_and_the_publication(monkeypatch):
    monkeypatch.setattr(marks, "tmview_detail", lambda st13: TMVIEW_DETAIL)
    patch = marks.tm_refresh({"publication": "EM500000018228224"})
    assert patch["posture"] == "registered"
    assert patch["publication_date"] == "2020-07-09"
    assert patch["opposition_start"] == "2020-07-09" and patch["opposition_end"] == "2020-10-13"
    assert patch["classes"] == ["7", "9"] and patch["expiry_date"] == "2030-04-22"
    assert patch["applicant"] == "GRABO LTD" and patch["opposition_pending"] is False


# ---------------------------------------------------------------------------------------------
# designs
# ---------------------------------------------------------------------------------------------

def test_a_registered_community_design_is_open_to_invalidity_only():
    row = {"kind": "design", "office": "EUIPO", "posture": "registered"}
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    assert acts["pre_grant"]["status"] == "na" and acts["post_grant_now"]["status"] == "na"
    assert acts["invalidity"]["status"] == "open" and acts["invalidity"]["fee"] == "€350"


def test_a_pending_us_design_application_takes_a_1290_until_allowance():
    row = {"kind": "design", "office": "USPTO", "posture": "pending"}
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    assert acts["pre_grant"]["status"] == "open" and acts["pre_grant"]["deadline"] is None


def test_a_us_design_patent_has_nine_months_of_post_grant_review_then_the_rest():
    row = {"kind": "design", "office": "USPTO", "posture": "registered", "grant_date": "2026-03-03",
           "patent_number": "1012345"}
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    assert acts["post_grant_now"]["status"] == "open" and acts["post_grant_now"]["deadline"] == "2026-12-03"
    assert acts["invalidity"]["status"] == "open"
    old = dict(row, grant_date="2024-03-03")
    acts = {a["stage"]: a for a in marks.actions_for(old, TODAY)}
    assert acts["post_grant_now"]["status"] == "closed"


EUIPO_DESIGN = {"designNumber": "005888591-0001", "applicationNumber": "005888591",
                "locarnoClasses": ["08.08"], "applicants": [{"office": "EM", "identifier": "971213"}],
                "applicationDate": "2018-12-13", "registrationDate": "2018-12-13",
                "expiryDate": "2028-12-13", "status": "REGISTERED_AND_FULLY_PUBLISHED"}
EUIPO_DETAIL = {"designNumber": "005888591-0001", "status": "REGISTERED_AND_FULLY_PUBLISHED",
                "applicants": [{"name": "J. Schmalz GmbH"}], "designers": [{"name": "Kurt Schmalz"}],
                "locarnoClasses": ["08.08"], "publicationDefermentIndicator": True,
                "publications": [{"publicationDate": "2019-06-13"}],
                "productIndications": [{"language": "de", "terms": ["Greifvorrichtungen"]},
                                       {"language": "en", "terms": ["Grip devices"]}]}


def test_an_euipo_design_row_carries_its_english_indication_and_owner(monkeypatch):
    monkeypatch.setattr(marks, "euipo_design_detail", lambda num: EUIPO_DETAIL)
    row = marks.euipo_design_row(EUIPO_DESIGN, "Schmalz")
    assert row["publication"] == "RCD005888591-0001" and row["office"] == "EUIPO"
    assert row["title"] == "Grip devices" and row["applicant"] == "J. Schmalz GmbH"
    assert row["posture"] == "registered" and row["expiry_date"] == "2028-12-13"
    assert row["publication_date"] == "2019-06-13" and row["deferred"] is True
    assert row["inventors"] == ["Kurt Schmalz"]


ODP_DESIGN = {"applicationNumberText": "29996045", "applicationMetaData": {
    "applicationTypeLabelName": "Design", "firstApplicantName": "J. Schmalz GmbH",
    "applicantBag": [{"applicantNameText": "J. Schmalz GmbH"}], "inventionTitle": "SUCTION HEAD FOR A MATERIAL HANDLING LIFTER",
    "inventorBag": [{"inventorNameText": "Kurt Schmalz"}], "applicationStatusDescriptionText": "Patented Case",
    "filingDate": "2016-09-19", "grantDate": "2017-10-03", "patentNumber": "798021"}}


def test_a_us_design_from_the_portal_is_keyed_on_its_application_and_named_by_its_patent():
    row = marks.odp_design_row(ODP_DESIGN, "Schmalz")
    assert row["publication"] == "US29996045" and row["granted_as"] == "USD798021"
    assert row["posture"] == "registered" and row["grant_date"] == "2017-10-03"
    assert row["title"] == "Suction Head For A Material Handling Lifter"
    assert row["google"].endswith("/USD798021")


def test_discovery_keeps_only_marks_the_target_actually_owns(monkeypatch):
    stranger = dict(TMVIEW_HIT, ST13="US500000097435417", tmName="SCHMALZTECH", applicantName=["SchmalzTech, LLC"])
    monkeypatch.setattr(marks, "euipo_trademarks", lambda word: [])
    monkeypatch.setattr(marks, "tmview_search", lambda text, offices: [TMVIEW_HIT, stranger])
    target = {"name": "Schmalz", "assignees": ["J. Schmalz GmbH"], "offices": ["EP", "US"]}
    new, rejected, errors = marks.discover(target, "trademark", set())
    assert [r["publication"] for r in new] == ["US500000075354221"]
    assert rejected and "SCHMALZTECH" in rejected[0]
    assert errors == []


def test_the_reference_tables_cover_every_stage_for_every_office():
    for kind in ("trademark", "design"):
        matrix, offices = marks.reference_matrix(kind, TODAY)
        assert [r["stage"] for r in matrix] == [s for s, _ in marks.STAGES_FOR[kind]]
        for r in matrix:
            assert set(r["cells"]) == {code for code, _ in offices}


EUIPO_TM = {"applicationNumber": "018228224", "markKind": "INDIVIDUAL", "markFeature": "WORD",
            "markBasis": "EU_TRADEMARK", "niceClasses": [7, 9],
            "wordMarkSpecification": {"verbalElement": "GRABO"},
            "applicants": [{"office": "EM", "identifier": "1", "name": "GRABO LTD"}],
            "applicationDate": "2020-04-22", "registrationDate": "2020-10-17", "expiryDate": "2030-04-22",
            "publications": [{"bulletinNumber": "2020/128", "publicationSection": "A.1", "publicationDate": "2020-07-09"},
                             {"bulletinNumber": "2020/200", "publicationSection": "B.2", "publicationDate": "2020-10-20"}],
            "status": "REGISTERED"}


def test_an_euipo_trademark_record_becomes_a_row_with_its_opposition_period():
    row = marks.euipo_tm_row(EUIPO_TM, "Grabo")
    assert row["publication"] == "EM018228224" and row["office"] == "EUIPO" and row["office_code"] == "EM"
    assert row["title"] == "GRABO" and row["applicant"] == "GRABO LTD" and row["classes"] == ["7", "9"]
    assert row["posture"] == "registered"
    assert row["publication_date"] == "2020-07-09" and row["opposition_end"] == "2020-10-09"
    acts = {a["stage"]: a for a in marks.actions_for(row, TODAY)}
    assert acts["opposition"]["status"] == "closed" and acts["cancellation"]["status"] == "open"


def test_an_accepted_madrid_designation_counts_as_registered():
    assert marks._tm_posture("ACCEPTED") == "registered"
    assert marks._tm_posture("APPLICATION_PUBLISHED") == "pending"
    assert marks._tm_posture("OPPOSITION_PENDING") == "pending"


def test_eu_marks_come_from_the_api_and_the_rest_from_tmview(monkeypatch):
    monkeypatch.setattr(marks, "euipo_trademarks", lambda word: [EUIPO_TM])
    monkeypatch.setattr(marks, "tmview_search", lambda text, offices: [TMVIEW_HIT] if "US" in offices else [])
    target = {"name": "Both", "assignees": ["GRABO LTD", "J. Schmalz GmbH"], "offices": ["EP", "US"]}
    new, rejected, errors = marks.discover(target, "trademark", set())
    assert sorted(r["publication"] for r in new) == ["EM018228224", "US500000075354221"]
    assert errors == []


def test_a_challenge_page_from_tmview_is_reported_not_swallowed(monkeypatch):
    def boom(text, offices):
        raise ValueError("Expecting value: line 1 column 1")
    monkeypatch.setattr(marks, "euipo_trademarks", lambda word: [])
    monkeypatch.setattr(marks, "tmview_search", boom)
    new, rejected, errors = marks.discover({"name": "X", "assignees": ["Schmalz"], "offices": ["EP", "US", "DE"]},
                                           "trademark", set())
    assert new == [] and errors and "challenge page" in errors[0] and "USPTO" in errors[0]
