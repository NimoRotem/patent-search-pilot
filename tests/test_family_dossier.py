"""The file wrapper, which no patent-corpus search can reach.

Every fixture below is the shape the real USPTO ODP API returned on 2026-08-16 for U.S. App.
18/915,337 — the application a patent attorney filed a preissuance submission against. No test
here touches the network.
"""
import family_dossier as FD

SEARCH = {"patentFileWrapperDataBag": [{
    "applicationNumberText": "18/915,337",
    "applicationMetaData": {
        "applicationStatusDescriptionText": "Docketed New Case - Ready for Examination",
        "inventionTitle": "Portable vacuum gripper",
        "earliestPublicationNumber": "US20250033224A1", "patentNumber": None,
        "filingDate": "2024-10-14"}}]}

CONTINUITY = {
    "18915337": {"parentContinuityBag": [
        {"claimParentageTypeCodeDescriptionText": "is a Continuation of",
         "parentApplicationStatusDescriptionText": "Patented Case",
         "parentApplicationNumberText": "18513573", "childApplicationNumberText": "18915337",
         "parentPatentNumber": "12115659", "parentApplicationFilingDate": "2023-11-19"}]},
    "18513573": {"parentContinuityBag": [
        {"claimParentageTypeCodeDescriptionText": "is a Continuation of",
         "parentApplicationStatusDescriptionText":
             "Abandoned  --  Failure to Respond to an Office Action",
         "parentApplicationNumberText": "17724791", "childApplicationNumberText": "18513573",
         "parentApplicationFilingDate": "2022-04-20"}]},
}

DOCS = {
    "17724791": {"documentBag": [
        {"documentCode": "CTNF", "documentCodeDescriptionText": "Non-Final Rejection",
         "officialDate": "2025-09-16T00:00:00", "documentIdentifier": "X1"},
        {"documentCode": "892",
         "documentCodeDescriptionText": "List of references cited by examiner",
         "officialDate": "2025-09-16T00:00:00", "documentIdentifier": "X2"},
        {"documentCode": "OATH", "documentCodeDescriptionText": "Oath or Declaration filed",
         "officialDate": "2022-04-20T00:00:00", "documentIdentifier": "X3"}]},
    "18513573": {"documentBag": [
        {"documentCode": "CTNF", "documentCodeDescriptionText": "Non-Final Rejection",
         "officialDate": "2024-01-24T00:00:00", "documentIdentifier": "Y1"}]},
}


def _fake_call(monkeypatch):
    def call(path, body=None, log=print):
        if path.endswith("/search"):
            return SEARCH
        #  "patent/applications/<app>/continuity" -> the app is the SECOND-to-last part.
        parts = path.strip("/").split("/")
        app = parts[-2] if len(parts) >= 2 else ""
        if path.endswith("/continuity"):
            return CONTINUITY.get(app, {})
        if path.endswith("/documents"):
            return DOCS.get(app, {"documentBag": []})
        return {}
    monkeypatch.setattr(FD, "_call", call)
    monkeypatch.setattr(FD, "KEY", "test-key")
    monkeypatch.setattr(FD, "ENABLED", True)


def test_it_walks_more_than_one_hop_to_reach_the_office_action(monkeypatch):
    """THE CASE THIS EXISTS FOR. The attorney's Document 6 was the Non-Final Rejection in
    17/724,791 — TWO hops above the subject, because the subject's parent was itself a
    continuation and the abandonment was above that. A one-hop lookup finds nothing."""
    _fake_call(monkeypatch)
    d = FD.dossier(publication="US20250033224A1", log=lambda *a: None)
    apps = {r["app"] for r in d["rejections"]}
    assert "17724791" in apps, d["rejections"]
    assert {f["app"] for f in d["family"]} == {"18513573", "17724791"}


def test_the_examiners_own_citation_list_is_surfaced(monkeypatch):
    """The 892 is a reference list chosen by a professional who read the application. It beats
    anything a model guesses as a query-by-example seed, and it is authoritative as an eval set."""
    _fake_call(monkeypatch)
    d = FD.dossier(publication="US20250033224A1", log=lambda *a: None)
    assert any(r["code"] == "892" for r in d["citation_lists"])


def test_a_granted_sibling_is_reported(monkeypatch):
    """US 12,115,659 is the double-patenting reference the examiner used. It is in the family and
    nowhere in a prior-art search."""
    _fake_call(monkeypatch)
    d = FD.dossier(publication="US20250033224A1", log=lambda *a: None)
    assert [f["patent"] for f in d["siblings_granted"]] == ["12115659"]


def test_abandonment_reaches_the_summary(monkeypatch):
    _fake_call(monkeypatch)
    d = FD.dossier(publication="US20250033224A1", log=lambda *a: None)
    s = FD.summarise(d)
    assert "ABANDONED" in s and "12115659" in s and "2025-09-16" in s


def test_no_key_is_a_reason_not_a_crash(monkeypatch):
    """This runs beside a search that already has an answer. An unreachable USPTO must cost its own
    findings and nothing else.

    Both the constant AND the environment have to be cleared: `_key()` deliberately falls back to
    os.environ so a key loaded from .env after import still works, which means clearing the
    constant alone does not describe a keyless machine — on the live host it silently passes while
    reading the real key.
    """
    monkeypatch.setattr(FD, "KEY", "")
    monkeypatch.delenv("USPTO_ODP_KEY", raising=False)
    monkeypatch.delenv("ODP_API_KEY", raising=False)
    d = FD.dossier(publication="US20250033224A1", log=lambda *a: None)
    assert d["error"] == "no USPTO_ODP_KEY"
    assert d["rejections"] == [] and d["family"] == []
    assert FD.summarise(d) == ""


def test_application_numbers_are_normalised():
    assert FD._norm_app("18/915,337") == "18915337"
    assert FD._norm_app(" 17724791 ") == "17724791"
    assert FD._norm_app(None) == ""
