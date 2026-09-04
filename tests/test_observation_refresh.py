"""The live sweep, against captured office payloads rather than the offices.

Every fixture here is a trimmed capture of a real 2026-09-04 response, so what is asserted is the
wire format as three offices actually serve it, not as their documentation describes it. The two
that cost the most to learn are pinned deliberately:

  * the EP B1 publication date IS the mention of grant, and it is the only date the nine months of
    Art. 99 can be counted from;
  * INPADOC legal is FRESHER than the bibliographic search. DE 10 2024 133 318 granted on
    20 August 2026 and the biblio search still returned only the A1 two weeks later, so grant is
    read off the legal family and never off a search.
"""
import datetime

import pytest

import observation_refresh as refresh


def _ops(payload):
    """Answer every OPS call with one payload."""
    return lambda path: (200, payload)


# ---------------------------------------------------------------------------------------------
# EPO register
# ---------------------------------------------------------------------------------------------

EP_GRANTED = {"ops:world-patent-data": {"ops:register-search": {"reg:register-documents": {
    "reg:register-document": {
        "@status": "The patent has been granted",
        "reg:ep-patent-statuses": {"reg:ep-patent-status": [
            {"@change-date": "20251128", "@status-code": "8", "$": "The patent has been granted"},
            {"@change-date": "20250731", "@status-code": "12", "$": "Grant of patent is intended"}]},
        "reg:bibliographic-data": {
            "reg:application-reference": {"reg:document-id": {"reg:doc-number": {"$": "24159908"}}},
            "reg:publication-reference": [
                {"reg:document-id": {"reg:country": {"$": "EP"}, "reg:doc-number": {"$": "4446072"},
                                     "reg:kind": {"$": "A1"}, "reg:date": {"$": "20241016"}}},
                {"reg:document-id": {"reg:country": {"$": "EP"}, "reg:doc-number": {"$": "4446072"},
                                     "reg:kind": {"$": "B1"}, "reg:date": {"$": "20251231"}}}]}}}}}}

EP_INTENDED = {"ops:world-patent-data": {"ops:register-search": {"reg:register-documents": {
    "reg:register-document": {
        "@status": "Grant of patent is intended",
        "reg:ep-patent-statuses": {"reg:ep-patent-status": [
            {"@change-date": "20260722", "@status-code": "12", "$": "Grant of patent is intended"},
            {"@change-date": "20240308", "@status-code": "16", "$": "The application has been published"}]},
        "reg:bibliographic-data": {
            "reg:application-reference": {"reg:document-id": {"reg:doc-number": {"$": "23199212"}}},
            "reg:publication-reference": {
                "reg:document-id": {"reg:country": {"$": "EP"}, "reg:doc-number": {"$": "4349543"},
                                    "reg:kind": {"$": "A1"}, "reg:date": {"$": "20240403"}}}}}}}}}


def test_the_b1_publication_is_the_mention_of_grant(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(EP_GRANTED))
    got = refresh.ep_case("EP4446072B1")
    assert got["posture"] == "granted"
    assert got["grant_published"] == "2025-12-31"
    assert got["opposition_deadline"] == "2026-09-30"
    assert got["deadline"] == "2026-09-30"
    assert got["deadline_kind"] == "hard"
    assert got["granted_as"] == "EP4446072B1"


def test_an_intention_to_grant_is_pending_with_no_deadline_and_a_warning(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(EP_INTENDED))
    got = refresh.ep_case("EP4349543A1")
    assert got["posture"] == "pending"
    assert got["deadline"] is None
    assert got["closing_soon"] is True
    #  Rule 71(3) on 22 July, so grant lands around five months later.
    assert got["opposition_opens_est"] == "2026-12-22"
    assert got["register_updated"] == "2026-07-22"


def test_an_unreachable_register_reports_rather_than_inventing(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", lambda path: (503, {}))
    assert "_error" in refresh.ep_case("EP4446072B1")


# ---------------------------------------------------------------------------------------------
# INPADOC legal, which is how the German half is read now
# ---------------------------------------------------------------------------------------------

def _de_member(kind, date, legal=()):
    return {"publication-reference": {"document-id": [
                {"@document-id-type": "docdb", "country": {"$": "DE"},
                 "doc-number": {"$": "102024133318"}, "kind": {"$": kind}, "date": {"$": date}}]},
            "ops:legal": [dict(l) for l in legal]}


DE_GRANTED = {"ops:world-patent-data": {"ops:patent-family": {"ops:family-member": [
    _de_member("A1", "20260521", [
        {"@code": "R012", "@desc": "REQUEST FOR EXAMINATION VALIDLY FILED",
         "ops:pre": {"$": "DE102024133318A  2024-11-14R012+REQUEST FOR EXAMINATION VALIDLY FILED"}},
        {"@code": "R018", "@desc": "GRANT DECISION BY EXAMINATION SECTION/EXAMINING DIVISION",
         "ops:pre": {"$": "DE102024133318A  2026-05-08R018+GRANT DECISION"}}]),
    _de_member("B4", "20260820")]}}}

#  One publication, one event, and that event is NOT R012: the shape that says nobody has asked
#  for examination yet, which is the only shape that makes § 44(2) worth paying for.
DE_PENDING_UNEXAMINED = {"ops:world-patent-data": {"ops:patent-family": {"ops:family-member":
    _de_member("A1", "20250612", [
        {"@code": "R163", "@desc": "REQUEST FOR CHANGE HAS GONE ABANDONED",
         "ops:pre": {"$": "DE102024133318A  2025-06-12R163 SOMETHING"}}])}}}


def test_a_german_grant_is_read_off_the_legal_family_not_a_search(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(DE_GRANTED))
    got = refresh.de_case("DE102024133318A1")
    assert got["posture"] == "granted"
    assert got["grant_published"] == "2026-08-20"
    assert got["granted_as"] == "DE102024133318B4"
    assert got["opposition_deadline"] == "2027-05-20"
    assert got["exam_requested"] is True


def test_no_request_for_examination_on_the_file_leaves_44_2_open(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(DE_PENDING_UNEXAMINED))
    got = refresh.de_case("DE102024133318A1")
    assert got["posture"] == "pending"
    assert got["exam_requested"] is False
    assert got["deadline"] is None


def test_a_family_member_in_another_country_never_moves_this_row(monkeypatch):
    """The single most dangerous shape in the legal payload: the family carries the EP and US
    siblings too, and reading a grant off one of those would put a nine-month clock on a German
    application that is still in examination."""
    foreign = {"publication-reference": {"document-id": [
        {"@document-id-type": "docdb", "country": {"$": "EP"}, "doc-number": {"$": "4349543"},
         "kind": {"$": "B1"}, "date": {"$": "20260101"}}]}, "ops:legal": []}
    payload = {"ops:world-patent-data": {"ops:patent-family": {"ops:family-member": [
        _de_member("A1", "20250612"), foreign]}}}
    monkeypatch.setattr(refresh, "_ops_json", _ops(payload))
    got = refresh.de_case("DE102024133318A1")
    assert got["posture"] == "pending"
    assert "grant_published" not in got


#  Four Schmalz applications turned out to be in exactly these two states on 2026-09-04, which is
#  why both are pinned: the difference between them is whether the case is dead or merely quiet,
#  and DPMAregister reports BOTH as "anhängig".
DE_REFUSED_FINAL = {"ops:world-patent-data": {"ops:patent-family": {"ops:family-member":
    _de_member("A1", "20230713", [
        {"@code": "R012", "@desc": "REQUEST FOR EXAMINATION VALIDLY FILED",
         "ops:pre": {"$": "DE102024133318A  2022-01-07R012+X"}},
        {"@code": "R002", "@desc": "REFUSAL DECISION IN EXAMINATION/REGISTRATION PROCEEDINGS",
         "ops:pre": {"$": "DE102024133318A  2026-06-29R002+X"}},
        {"@code": "R003", "@desc": "REFUSAL DECISION NOW FINAL",
         "ops:pre": {"$": "DE102024133318A  2026-08-05R003+X"}}])}}}

DE_REFUSED_NOT_FINAL = {"ops:world-patent-data": {"ops:patent-family": {"ops:family-member":
    _de_member("A1", "20230713", [
        {"@code": "R002", "@desc": "REFUSAL DECISION IN EXAMINATION/REGISTRATION PROCEEDINGS",
         "ops:pre": {"$": "DE102024133318A  2026-06-29R002+X"}}])}}}


def test_a_final_refusal_kills_the_case(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(DE_REFUSED_FINAL))
    got = refresh.de_case("DE102024133318A1")
    assert got["posture"] == "lapsed"
    assert got["refused_on"] == "2026-06-29"
    assert "2026-08-05" in got["register_status"]


def test_a_refusal_that_is_not_final_leaves_the_case_pending_and_flagged(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(DE_REFUSED_NOT_FINAL))
    got = refresh.de_case("DE102024133318A1")
    assert got["posture"] == "pending"
    assert got["closing_soon"] is True
    assert got["refused_on"] == "2026-06-29"


def test_a_withdrawn_change_of_representative_does_not_kill_an_application(monkeypatch):
    """The loose version of this check read the word "withdraw" anywhere in the last few events
    and buried four live applications. The phrase list has to be specific."""
    payload = {"ops:world-patent-data": {"ops:patent-family": {"ops:family-member":
        _de_member("A1", "20230713", [
            {"@code": "R082", "@desc": "CHANGE OF REPRESENTATIVE WITHDRAWN",
             "ops:pre": {"$": "DE102024133318A  2026-01-05R082 X"}}])}}}
    monkeypatch.setattr(refresh, "_ops_json", _ops(payload))
    assert refresh.de_case("DE102024133318A1")["posture"] == "pending"


# ---------------------------------------------------------------------------------------------
# USPTO
# ---------------------------------------------------------------------------------------------

def _wrapper(status, status_date, pub, pub_date, events, patent=None, grant=None):
    return {"patentFileWrapperDataBag": [{
        "applicationNumberText": "19315746",
        "applicationMetaData": {
            "applicationStatusDescriptionText": status,
            "applicationStatusDate": status_date,
            "earliestPublicationNumber": pub, "earliestPublicationDate": pub_date,
            "filingDate": "2025-09-01", "patentNumber": patent, "grantDate": grant},
        "eventDataBag": [{"eventDate": d, "eventCode": c, "eventDescriptionText": t}
                         for d, c, t in events]}]}


DOCS = {"documentBag": [
    {"documentCode": "IDS.3P", "officialDate": "2026-08-02T00:00:00"},
    {"documentCode": "N417.PYMT", "officialDate": "2026-08-02T00:00:00"},
    {"documentCode": "M327", "officialDate": "2026-08-04T00:00:00"},
] + [{"documentCode": "3P.RELEVANCE", "officialDate": "2026-08-02T00:00:00"} for _ in range(10)]}


def _odp_stub(meta, docs):
    def call(path, body=None):
        if path.endswith("/documents"):
            return docs
        return meta
    return call


def test_the_window_closes_on_the_later_of_six_months_and_the_first_rejection(monkeypatch):
    meta = _wrapper("Non Final Action Mailed", "2026-07-28", "US20260090761A1", "2026-04-02",
                    [("2026-07-24", "CTNF", "Non-Final Rejection")])
    monkeypatch.setattr(refresh, "_odp", _odp_stub(meta, {"documentBag": []}))
    got = refresh.us_case("18880032")
    assert got["six_months"] == "2026-10-02"
    assert got["first_rejection"] == "2026-07-24"
    assert got["deadline"] == "2026-10-02"          # the six-month date, being the later one
    assert got["deadline_kind"] == "hard"


def test_no_rejection_leaves_the_window_open_ended(monkeypatch):
    meta = _wrapper("Docketed New Case - Ready for Examination", "2025-09-10",
                    "US20260109053A1", "2026-04-23", [])
    monkeypatch.setattr(refresh, "_odp", _odp_stub(meta, {"documentBag": []}))
    got = refresh.us_case("19315746")
    assert got["deadline"] == "2026-10-23"
    assert got["deadline_kind"] == "open_ended"
    assert got["posture"] == "pending"


def test_our_own_submission_is_read_out_of_the_file_wrapper(monkeypatch):
    meta = _wrapper("Docketed New Case - Ready for Examination", "2025-09-10",
                    "US20260109053A1", "2026-04-23", [])
    monkeypatch.setattr(refresh, "_odp", _odp_stub(meta, DOCS))
    got = refresh.us_case("19315746")
    subs = got["our_submissions"]
    assert len(subs) == 1
    assert subs[0]["date"] == "2026-08-02"
    assert subs[0]["documents"] == 10
    #  The office files each concise description twice, as filed and as its own scan, so the
    #  document count is roughly double the number of references actually cited.
    assert subs[0]["references_about"] == 5
    assert subs[0]["fee_paid"] is True
    assert "IDS.3P" in subs[0]["evidence"]
    assert "twice" in subs[0]["evidence"]


def test_a_lone_office_letter_is_not_counted_as_a_submission(monkeypatch):
    meta = _wrapper("Docketed New Case", "2025-09-10", "US20260109053A1", "2026-04-23", [])
    monkeypatch.setattr(refresh, "_odp", _odp_stub(meta, {"documentBag": [
        {"documentCode": "M327", "officialDate": "2026-08-04T00:00:00"}]}))
    assert refresh.us_case("19315746")["our_submissions"] == []


def test_a_passed_six_month_date_with_no_rejection_leaves_no_deadline_not_a_dead_one(monkeypatch):
    """Published March 2024, so the six-month date went by in 2024 and no rejection ever issued.
    Carrying that date forward printed "closed" beside a window the same page called open."""
    meta = _wrapper("Ex parte Quayle Action Mailed", "2026-08-26", "US20240316792A1",
                    "2024-09-26", [("2026-08-26", "CTEQ", "Ex parte Quayle Action")])
    monkeypatch.setattr(refresh, "_odp", _odp_stub(meta, {"documentBag": []}))
    got = refresh.us_case("18577905")
    assert got["posture"] == "pending"
    assert got["deadline"] is None
    assert got["deadline_kind"] == "open_ended"
    assert got["closing_soon"] is True
    assert "Quayle" in got["closing_note"]


def test_an_abandoned_application_has_no_window(monkeypatch):
    meta = _wrapper("Abandoned  --  Failure to Respond to an Office Action", "2026-01-01",
                    "US20240047710A1", "2024-02-08", [])
    monkeypatch.setattr(refresh, "_odp", _odp_stub(meta, {"documentBag": []}))
    got = refresh.us_case("18266905")
    assert got["posture"] == "lapsed"
    assert got["deadline"] is None


# ---------------------------------------------------------------------------------------------
# the merge, which is where a refresh could do real damage
# ---------------------------------------------------------------------------------------------

def test_a_sweep_only_writes_register_fields(monkeypatch):
    row = {"publication": "EP4446072B1", "office": "EPO", "posture": "pending",
           "counsel_report": "commissioned", "priority": "1 - critical",
           "user_note": "counsel is drafting"}
    monkeypatch.setattr(refresh, "_ops_json", _ops(EP_GRANTED))
    out = refresh.sweep([row], discover=False)
    patch = out["patches"]["EP4446072B1"]
    assert patch["posture"] == "granted"
    assert "counsel_report" not in patch
    assert "priority" not in patch
    assert "user_note" not in patch
    assert patch["refreshed_at"] == datetime.date.today().isoformat()


def test_a_flag_that_has_stopped_being_true_is_cleared(monkeypatch):
    """The sticky-field bug. A refusal became final, so the case is dead and nothing about it is
    closing; the `closing_soon` an earlier sweep set while it was merely wobbling has to go, or
    the docket keeps a dead case pinned to the top of the page."""
    row = {"publication": "DE102024133318A1", "office": "DPMA", "posture": "pending",
           "closing_soon": True, "decision_on": "2026-05-08", "deadline": "2026-08-08"}
    monkeypatch.setattr(refresh, "_ops_json", _ops(DE_REFUSED_FINAL))
    patch = refresh.sweep([row], discover=False)["patches"]["DE102024133318A1"]
    assert patch["posture"] == "lapsed"
    assert patch["closing_soon"] is None
    assert patch["decision_on"] is None
    assert patch["deadline"] is None


def test_the_change_log_names_what_moved_and_stays_quiet_otherwise(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(EP_GRANTED))
    moved = refresh.sweep([{"publication": "EP4446072B1", "office": "EPO",
                            "posture": "pending"}], discover=False)
    assert any("posture moved from pending to granted" in c for c in moved["changes"])
    still = refresh.sweep([{"publication": "EP4446072B1", "office": "EPO", "posture": "granted",
                            "register_status": "The patent has been granted",
                            "deadline": "2026-09-30", "grant_published": "2025-12-31"}],
                          discover=False)
    assert still["changes"] == []


def test_one_office_failing_does_not_lose_the_others(monkeypatch):
    def flaky(path):
        if "register" in path:
            return 500, {}
        return 200, DE_GRANTED
    monkeypatch.setattr(refresh, "_ops_json", flaky)
    out = refresh.sweep([{"publication": "EP4446072B1", "office": "EPO"},
                         {"publication": "DE102024133318A1", "office": "DPMA"}], discover=False)
    assert "DE102024133318A1" in out["patches"]
    assert "EP4446072B1" not in out["patches"]
    assert any("EP4446072B1" in e for e in out["errors"])


@pytest.mark.parametrize("pub,expect", [
    ("US20260109053A1", "US20260109053"), ("DE102024133318A1", "DE102024133318"),
    ("EP4446072B1", "EP4446072"), ("EP4792992A2", "EP4792992"), ("EP4446072", "EP4446072")])
def test_the_kind_code_is_stripped_because_ops_404s_on_it(pub, expect):
    assert refresh._epodoc(pub) == expect


# ---------------------------------------------------------------------------------------------
# the European file: what is already on it, and giving a new case a readable name
# ---------------------------------------------------------------------------------------------

def _steps(*steps):
    return {"ops:world-patent-data": {"ops:register-search": {"reg:register-documents": {
        "reg:register-document": {"reg:procedural-data": {"reg:procedural-step": [
            {"reg:procedural-step-code": {"$": c},
             "reg:procedural-step-text": [{"@step-text-type": "STEP_DESCRIPTION", "$": d}],
             "reg:procedural-step-date": [{"@step-date-type": "DATE_OF_DISPATCH",
                                           "reg:date": {"$": when}}]}
            for c, d, when in steps]}}}}}}


def test_observations_already_on_the_european_file_are_found_and_not_claimed(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(_steps(
        ("OBSE", "Observations by third parties", "20260701"),
        ("RFEE", "Renewal fee payment", "20260101"))))
    got = refresh.ep_procedural("EP4054810A1")
    assert len(got["file_events"]) == 1
    ev = got["file_events"][0]
    assert ev["date"] == "2026-07-01"
    #  The register does not say whose they are, and Art. 115 permits anonymity, so the sweep
    #  must never report them as ours.
    assert ev["whose"] == "unknown"
    assert "opposition_pending" not in got


def test_a_pending_opposition_is_read_off_the_procedural_file(monkeypatch):
    monkeypatch.setattr(refresh, "_ops_json", _ops(_steps(
        ("OPPO", "Opposition filed", "20260601"))))
    assert refresh.ep_procedural("EP3995267B1")["opposition_pending"] is True


BIBLIO = {"ops:world-patent-data": {"exchange-documents": {"exchange-document": {
    "@family-id": "98737667",
    "bibliographic-data": {
        "invention-title": [
            {"@lang": "de", "$": "HANDHABUNGSANLAGE MIT TRÄGERSTRUKTUR"},
            {"@lang": "en", "$": "HANDLING SYSTEM WITH SUPPORT STRUCTURE"}],
        "parties": {"applicants": {"applicant": [
            {"@data-format": "epodoc", "applicant-name": {"name": {"$": "SCHMALZ J GMBH [DE]"}}},
            {"@data-format": "original", "applicant-name": {"name": {"$": "J. Schmalz GmbH"}}}]}},
        "application-reference": {"document-id": [
            {"@document-id-type": "docdb", "country": {"$": "EP"},
             "doc-number": {"$": "26158614"}}]}}}}}}


def test_a_newly_found_case_gets_a_name_a_person_can_read(monkeypatch):
    """It arrived on the docket as "EP4792992A2", title and all, which is unreadable in a list of
    a hundred rows."""
    monkeypatch.setattr(refresh, "_ops_json", _ops(BIBLIO))
    got = refresh.biblio_for("EP4792992A2")
    assert got["title"] == "HANDLING SYSTEM WITH SUPPORT STRUCTURE"
    assert got["applicant"] == "J. Schmalz GmbH"          # the original form, not the DOCDB one
    assert got["application"] == "EP26158614"
