"""Two things the build itself refuses, rather than leaving them to be noticed after filing.

  UNREADABLE IS A HARD EXCLUSION. Two of the thirteen documents in a real packet were on that
  search's own "full text still unavailable after recovery" list, so every row filed for them
  rested on a two-hundred-word abstract. An unread reference scores HIGH on coverage, not low: a
  short text is mapped generously and every cell verifies against the abstract it came from.

  THE PACKET IS CHECKED AGAINST PATENT CENTER before it is called ready, and the check runs on the
  files that were actually written rather than on the intention that wrote them.
"""
import json
import time

import pytest

import webapp


@pytest.fixture()
def client(monkeypatch):
    webapp.app.config["TESTING"] = True
    monkeypatch.setattr(webapp, "_can_access_report", lambda slug: True)
    return webapp.app.test_client()


@pytest.fixture()
def report(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(webapp, "CONCISE_DIR", tmp_path / "concise")
    slug = "adhoc-testunread"
    cell = {"item": "claim 1[a]", "verdict": "disclosed", "grounding": "verified",
            "bar": "discloses", "quote": "a base element 141 having an elliptical track 148",
            "note": "The reference discloses a base element with peripheral openings.",
            "location": "paragraph p0012", "coord": {"para_no": "p0012"}, "confidence": 0.9}
    deep = {
        "subject_label": "US-20250033224-A1",
        "claims": [{"label": "claim 1[a]", "claim_no": 1, "independent": True,
                    "text": "a base element comprising one or more openings"}],
        "references": [
            {"pub": "US-11413727-B2", "title": "Vacuum Gripper", "rank": 1, "claims": [cell]},
            {"pub": "US-8991263-B2", "title": "Snubbing clamp", "rank": 2, "claims": [dict(cell)]},
        ],
    }
    (tmp_path / ("%s.deep.json" % slug)).write_text(json.dumps(deep))
    #  The search's own record that one of the two was never read in full.
    (tmp_path / ("%s.json" % slug)).write_text(json.dumps(
        {"deep_rank": {"not_readable": [{"pub": "US-8991263-B2"}]}}))
    (tmp_path / ("%s.meta.json" % slug)).write_text(json.dumps({"subject": "US-20250033224-A1"}))
    import concise_description as cd
    monkeypatch.setattr(cd, "phrase", lambda doc, tier="strong", model=None: doc)
    monkeypatch.setattr(cd, "_display", lambda pub, allow_fetch=True: {
        "title": "Vacuum Gripper", "inventors": ["Nimrod Rotem"],
        "publication_date": "2022-08-16", "priority_date": "2018-05-09"})
    monkeypatch.setattr(cd, "subject_facts", lambda label: {"efd": None, "assignees": []})
    monkeypatch.setattr(webapp, "_concise_source_text",
                        lambda pub: "The gripper has a base element 141 having an elliptical "
                                    "track 148 around its periphery.")
    webapp._CONCISE_JOBS.clear()
    yield slug
    for _ in range(300):
        if (webapp._concise_job(slug) or {}).get("state") != "running":
            break
        time.sleep(0.1)
    webapp._CONCISE_JOBS.clear()


def _post(client, slug, **extra):
    data = {"app_no": "18/915,337"}
    data.update(extra)
    return client.post("/report/%s/concise" % slug, data=data)


def test_a_document_whose_full_text_was_never_read_is_refused(client, report):
    r = _post(client, report, pubs=["US-11413727-B2", "US-8991263-B2"])
    assert r.status_code == 400
    body = r.get_data(as_text=True)
    assert "US-8991263-B2" in body
    assert "never read" in body and "abstract" in body
    #  and the picker is still on the page, so the selection can actually be fixed
    assert 'name="pubs"' in body


def test_the_refusal_lifts_when_the_practitioner_says_they_have_read_it(client, report):
    r = _post(client, report, pubs=["US-11413727-B2", "US-8991263-B2"],
              allow_unread=["US-8991263-B2"])
    assert r.status_code in (302, 303), r.get_data(as_text=True)[:400]


def test_a_readable_selection_is_untouched_by_the_gate(client, report):
    assert _post(client, report, pubs=["US-11413727-B2"]).status_code in (302, 303)


def test_the_row_carries_the_acknowledgement_box_rather_than_only_a_warning(client, report):
    body = client.get("/report/%s/concise" % report).get_data(as_text=True)
    assert 'name="allow_unread"' in body
    assert "I have read this document myself" in body


def test_the_conformance_sweep_reads_the_files_the_packet_actually_wrote(client, report,
                                                                        monkeypatch):
    """The audit paper is the LAST thing written, because everything before it is what the sweep
    inspects. A gate that ran before the files existed would pass an empty directory."""
    seen = {}
    import pdf_conform
    real = pdf_conform.check_paths
    monkeypatch.setattr(pdf_conform, "check_paths",
                        lambda paths: seen.setdefault("names", [p.name for p in paths])
                        and real(paths) or real(paths))
    _post(client, report, pubs=["US-11413727-B2"])
    end = time.time() + 30
    while time.time() < end:
        if (webapp._concise_job(report) or {}).get("state") in ("done", "failed"):
            break
        time.sleep(0.05)
    names = seen.get("names") or []
    assert names, "the sweep never ran"
    assert "01_DocumentList_and_Statements.pdf" in names
    assert any(n.startswith("ConciseDescription_") for n in names)
    assert "00_AUDIT.pdf" not in names, "the audit cannot be an input to itself"
    #  ...and it is written all the same, after the sweep.
    assert (webapp.CONCISE_DIR / report / "00_AUDIT.pdf").exists()
