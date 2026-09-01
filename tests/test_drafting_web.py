"""Authenticated Flask contracts for the versioned drafting workspace."""
import io
import re
import zipfile
from pathlib import Path

import pytest

import accounts
import auth
import draft_studio_service
import drafting
import webapp

USER = {
    "id": 91, "email": "drafter@example.test", "full_name": "Dana Drafter",
    "is_admin": False, "is_active": True, "email_on_completion": True,
    "session_version": 2,
    "default_applicant": "Example Labs", "default_inventors": "Dana Drafter",
}


def sections():
    return {
        "title": "Configurable Vacuum Lifting Tool",
        "cross_reference": "Not applicable.",
        "government_support": "Not applicable.",
        "field": "The disclosure relates to portable vacuum lifting tools.",
        "background": "Accessory readers are described in selected art [REF:US-11223344-B2].",
        "summary": "A handle identifies an attached base plate.",
        "drawing_descriptions": "FIG. 1 is a side elevation of the lifting apparatus.",
        "detailed_description": "FIG. 1 shows a battery-powered pump disposed in the handle.",
        "claims": "1. A lifting apparatus comprising a handle and an identifier reader.",
        "abstract": "A vacuum lifting tool identifies an attached base plate.",
    }


CARD = {
    "pub": "US-11223344-B2", "rank": 1, "title": "RFID tool accessory",
    "abstract": "A reader identifies an accessory.",
    "relevancy_opinion": "It describes identifier-based tool configuration.",
}


class FakeDraftService:
    def __init__(self):
        self.created = None
        self.selected = None
        self.queued = None

    def list_projects(self, principal, include_all=False):
        return [{"id": 7, "title": "RFID lifter", "status": "ready",
                 "latest_version_no": 1, "reference_count": 1,
                 "search_slug": "adhoc-owned", "full_name": USER["full_name"],
                 "email": USER["email"]}]

    def create_project(self, principal, **values):
        self.created = values
        return {"id": 7, "revision": 1} | values

    def create_project_with_references(self, principal, publication_numbers, **values):
        self.created = values
        self.selected = (7, list(publication_numbers), "atomic")
        return {"id": 7, "revision": 1, "reference_count": len(publication_numbers)} | values

    def select_references(self, principal, project_id, pubs, expected_revision=None):
        self.selected = (project_id, list(pubs), expected_revision)
        return {"id": project_id, "revision": 2, "reference_count": len(pubs)}

    def get_project(self, principal, project_id, include_versions=True):
        project = {
            "id": project_id, "user_id": USER["id"], "title": "RFID lifter",
            "search_slug": "adhoc-owned", "disclosure_text": "A sufficiently detailed inventor disclosure for the lifting tool.",
            "inventor_notes": "Inventor: Dana", "status": "ready", "revision": 2,
            "latest_version_no": 1,
            "references": [{"publication_number": "US-11223344-B2", "report_rank": 1,
                            "title": CARD["title"], "source_url": "https://patents.google.com/"}],
        }
        if include_versions:
            project["versions"] = [{"version_no": 1, "status": "draft", "model_name": "test",
                                    "created_at": "2026-08-01", "sections": sections()}]
            project["jobs"] = [{"id": 12, "project_id": project_id, "status": "complete",
                                "attempts": 1, "max_attempts": 3, "last_error": None}]
        return project

    def get_version(self, principal, project_id, version_no):
        return {"project_id": project_id, "version_no": version_no, "status": "draft",
                "sections": sections(), "markdown": drafting.render_application_markdown(sections())}

    def queue_generation(self, principal, project_id, **values):
        self.queued = (project_id, values)
        return {"id": 13, "project_id": project_id, "status": "queued"}

    def save_edited_version(self, principal, project_id, sections_value, base_version_no=None):
        return {"project_id": project_id, "version_no": 2, "status": "draft",
                "sections": sections_value, "base_version_no": base_version_no}


@pytest.fixture()
def draft_client(monkeypatch):
    service = FakeDraftService()
    webapp.app.config.update(TESTING=True, FORCE_AUTH=True, FORCE_ACCOUNTS=True)
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    monkeypatch.setattr(accounts, "get_user", lambda uid: dict(USER) if int(uid) == USER["id"] else None)
    monkeypatch.setattr(accounts, "list_searches", lambda *a, **k: [{
        "slug": "adhoc-owned", "title": "RFID lifter search", "query": "Detailed RFID lifter disclosure",
        "mode": "novelty", "search_focus": "claims", "status": "complete",
        "updated_at": "2026-08-01",
    }])
    monkeypatch.setattr(accounts, "get_search", lambda uid, slug: {
        "slug": slug, "title": "RFID lifter search", "status": "complete",
    })
    #  The double must answer everything the hot path asks of a report file, not just exists().
    #  _report_partial now stats the file to key its mtime cache and guards the missing-file case
    #  with `except OSError`; a stub without .stat() raises AttributeError instead, which escapes
    #  that guard and 502s the route under test for a reason that has nothing to do with drafting.
    #  Raising FileNotFoundError is the honest stand-in for "no report on disk yet", which is
    #  exactly the state this test puts the studio in.
    class _NoReportFile:
        def exists(self):
            return True

        def stat(self):
            raise FileNotFoundError("no report written yet")

        def read_text(self, *a, **k):
            raise FileNotFoundError("no report written yet")

    monkeypatch.setattr(webapp, "report_path", lambda slug: _NoReportFile())
    monkeypatch.setattr(webapp, "_drafting_service", lambda: service)
    monkeypatch.setattr(webapp, "_draft_report_loader",
                        lambda principal, slug, owner: {"query": "Detailed RFID lifter disclosure",
                                                       "query_document": {
                                                           "source": "upload",
                                                           "label": "rfid-lifter.txt",
                                                           "disclosure_text": "Verbatim full uploaded disclosure with all four claims.",
                                                           "n_claims": 4,
                                                       },
                                                       "cards": [dict(CARD)]})
    auth.reset_limits()
    client = webapp.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"]
        session["csrf_token"] = "csrf-draft"
    try:
        yield client, service
    finally:
        for key in ("FORCE_AUTH", "FORCE_ACCOUNTS"):
            webapp.app.config.pop(key, None)
        auth.reset_limits()


def test_draft_library_and_intake_render(draft_client):
    client, _service = draft_client
    library = client.get("/drafts")
    assert library.status_code == 200
    assert client.get("/drafts/").status_code == 200
    library_body = library.get_data(as_text=True)
    assert "US patent drafts" in library_body
    assert "Classic one-shot draft" not in library_body
    intake = client.get("/drafts/new?search_slug=adhoc-owned")
    assert intake.status_code == 302
    assert intake.headers["Location"].endswith("/drafts/start?search_slug=adhoc-owned")


def test_anonymous_studio_redirects_to_login_instead_of_rendering_not_found(
        draft_client, monkeypatch):
    client, _service = draft_client
    # Production nginx reaches Flask over loopback. The broad read-only loopback bypass must not
    # turn a missing named session into a misleading drafting 403 page.
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", True)
    with client.session_transaction() as session:
        session.clear()

    response = client.get("/drafts/7/studio")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert "next=/drafts/7/studio" in response.headers["Location"]


def test_conversational_intake_carries_search_text_and_prior_art(draft_client):
    client, _service = draft_client
    intake = client.get("/drafts/start?search_slug=adhoc-owned")
    assert intake.status_code == 200
    body = intake.get_data(as_text=True)
    assert 'name="search_slug" value="adhoc-owned"' in body
    assert "Verbatim full uploaded disclosure with all four claims." in body
    assert 'name="pubs" value="US-11223344-B2" checked' in body
    assert "References from adhoc-owned" in body


def test_conversational_intake_can_start_from_scratch(draft_client):
    client, _service = draft_client
    intake = client.get("/drafts/start")
    assert intake.status_code == 200
    body = intake.get_data(as_text=True)
    assert 'name="search_slug" value=""' in body
    assert "Describe the invention" in body
    assert "Detailed RFID lifter disclosure" not in body


def test_intake_uses_profile_defaults_and_specific_drafting_choices(draft_client):
    client, _service = draft_client
    body = client.get("/drafts/start").get_data(as_text=True)
    assert 'name="applicant"' in body and 'value="Example Labs"' in body
    #  One card per inventor since the intake started taking what 37 CFR 1.76 asks for, and a
    #  name on that paper is a GIVEN name and a family name, not one string. The profile's saved
    #  inventor still fills the first card; it is now split across two fields to get there.
    assert re.search(r'name="inventor_0_given_name"[^>]*value="Dana"', body)
    assert re.search(r'name="inventor_0_family_name"[^>]*value="Drafter"', body)
    assert "Anything else the drafter should know" not in body
    assert 'name="priority_status"' in body
    assert 'name="claim_strategy"' in body
    assert 'name="government_support"' in body
    assert chr(0x2014) not in body
    assert 'value="unknown"' not in body
    assert re.search(r'name="priority_status" value="none"\s+checked', body)
    assert re.search(r'name="government_support" value="none"\s+checked', body)


def test_unsupplied_filing_facts_never_create_notes_or_follow_up_requests():
    notes = webapp._structured_drafting_notes({})
    assert notes.count("Not applicable.") == 2
    assert not re.search(r"drafting note|ask for|not confirmed|placeholder", notes, re.IGNORECASE)


@pytest.mark.parametrize("values", [
    {"priority_status": "claim"},
    {"government_support": "yes"},
])
def test_selected_priority_or_government_support_requires_filing_details(values):
    with pytest.raises(drafting.DraftingValidationError):
        webapp._structured_drafting_notes(values)


def test_intake_uses_profile_name_when_no_inventor_default_exists(draft_client, monkeypatch):
    """The inventors are CARDS now, one per inventor, not one textarea of names.

    The defaulting is the thing worth pinning and it is unchanged: with no saved inventor, the
    first card opens on the profile's own name, split into the given and family names the
    application data sheet and the declaration are printed from.
    """
    client, _service = draft_client
    user = {**USER, "default_inventors": ""}
    monkeypatch.setattr(accounts, "get_user", lambda uid: dict(user))
    body = client.get("/drafts/start").get_data(as_text=True)
    assert re.search(r'name="inventor_0_given_name"[^>]*value="Dana"', body)
    assert re.search(r'name="inventor_0_family_name"[^>]*value="Drafter"', body)


def test_intake_uses_profile_name_when_no_applicant_default_exists(draft_client, monkeypatch):
    client, _service = draft_client
    user = {**USER, "default_applicant": "", "organization": ""}
    monkeypatch.setattr(accounts, "get_user", lambda uid: dict(user))
    body = client.get("/drafts/start").get_data(as_text=True)
    assert 'name="applicant"' in body
    assert 'value="Dana Drafter"' in body


def test_submission_uses_profile_party_defaults_when_form_fields_are_blank(
        draft_client, monkeypatch):
    client, _service = draft_client
    user = {**USER, "default_applicant": "", "default_inventors": "", "organization": ""}
    monkeypatch.setattr(accounts, "get_user", lambda uid: dict(user))

    class Studio:
        created = None

        def create(self, principal, **values):
            self.created = values
            return {"id": 45}

    studio = Studio()
    monkeypatch.setattr(webapp, "_studio", lambda: studio)
    response = client.post("/drafts/start", data={
        "csrf_token": "csrf-draft",
        "title": "Self-centering clamp",
        "disclosure_text": "A clamp has synchronized jaws that move radially toward a pipe.",
        "input_kind": "description",
        "applicant": "",
        "inventors": "",
    })

    assert response.status_code == 302
    assert studio.created["applicant"] == "Dana Drafter"
    assert studio.created["inventors"] == "Dana Drafter"


def test_completed_search_can_start_drafting_without_repeating_intake(draft_client, monkeypatch):
    client, _service = draft_client

    class Studio:
        created = None

        def create(self, principal, **values):
            self.created = values
            return {"id": 44}

    studio = Studio()
    monkeypatch.setattr(webapp, "_studio", lambda: studio)
    response = client.post("/drafts/start", data={
        "csrf_token": "csrf-draft", "direct": "1", "search_slug": "adhoc-owned",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/drafts/44/studio?created=1")
    assert studio.created["disclosure_text"] == \
        "Verbatim full uploaded disclosure with all four claims."
    assert studio.created["publication_numbers"] == ["US-11223344-B2"]
    assert studio.created["applicant"] == "Example Labs"
    assert studio.created["inventors"] == "Dana Drafter"


def test_studio_search_runs_in_background_and_can_import_without_navigation(
        draft_client, monkeypatch, tmp_path):
    client, _service = draft_client

    class Studio:
        recorded = None
        imported = None

        def search_material(self, principal, project_id):
            return {"title": "Current draft", "query": "A detailed current patent draft " * 8}

        def record_search(self, principal, project_id, **values):
            self.recorded = (project_id, values)
            return {"slug": values["slug"], "status": values["status"],
                    "imported_count": 0, "created_at": "2026-08-09"}

        def import_search(self, principal, project_id, slug, pubs):
            self.imported = (project_id, slug, pubs)
            return len(pubs)

    studio = Studio()
    monkeypatch.setattr(webapp, "_studio", lambda: studio)
    monkeypatch.setattr(webapp, "REPORTS", tmp_path)
    monkeypatch.setattr(webapp, "ensure_report", lambda *a, **k: ("running", None))
    monkeypatch.setattr(webapp.accounts, "record_search", lambda *a, **k: {})
    monkeypatch.setattr(webapp, "_draft_report_loader", lambda *a, **k: {
        "cards": [{"pub": "US-11223344-B2"}, {"pub": "EP-1234567-A1"}]})

    started = client.post("/drafts/7/studio/search", json={},
                          headers={"X-CSRF-Token": "csrf-draft"})
    assert started.status_code == 200 and started.get_json()["status"] == "running"
    slug = started.get_json()["slug"]
    assert studio.recorded[0] == 7
    imported = client.post(f"/drafts/7/studio/search/{slug}/import", json={},
                           headers={"X-CSRF-Token": "csrf-draft"})
    assert imported.get_json()["imported"] == 2
    assert studio.imported == (7, slug, ["US-11223344-B2", "EP-1234567-A1"])


def test_the_only_drawing_route_is_an_upload(draft_client, monkeypatch):
    """Uploading and deleting a sheet, and nothing that makes one.

    The three routes this replaced - generate, edit-a-region, photo-to-sketch - are the whole of
    the removed drawing lane, and the assertion that they are GONE matters as much as the one
    that says upload works: a route left behind is a route the front end can still be talked
    into calling.
    """
    client, _service = draft_client

    class Studio:
        calls = []

        def upload_figure(self, principal, project_id, **values):
            self.calls.append(("upload", project_id, values))
            return {"figure_id": 10, "label": values.get("label") or "FIG. 1"}

        def delete_figure(self, principal, project_id, figure_id):
            self.calls.append(("delete", project_id, figure_id))

    studio = Studio()
    monkeypatch.setattr(webapp, "_studio", lambda: studio)
    uploaded = client.post("/drafts/7/studio/figure/upload", data={
        "image": (io.BytesIO(b"png bytes"), "sheet.png"), "label": "FIG. 2",
        "csrf_token": "csrf-draft",
    })
    assert uploaded.status_code == 200
    assert studio.calls[-1][0] == "upload"
    assert studio.calls[-1][2]["image"] == b"png bytes"
    assert studio.calls[-1][2]["label"] == "FIG. 2"

    deleted = client.post("/drafts/7/studio/figure/9/delete", json={},
                          headers={"X-CSRF-Token": "csrf-draft"})
    assert deleted.status_code == 200 and studio.calls[-1] == ("delete", 7, 9)

    routes = {rule.rule for rule in webapp.app.url_map.iter_rules()}
    assert "/drafts/<int:project_id>/studio/figure" not in routes
    assert "/drafts/<int:project_id>/studio/figure/<int:figure_id>/manual" not in routes
    assert "/drafts/<int:project_id>/studio/photo-to-sketch" not in routes
    assert "/drafts/<int:project_id>/studio/drawings" not in routes
    assert not any("figure-compiler" in rule for rule in routes)


def test_figure_version_actions_cannot_cross_between_the_users_projects(
        draft_client, monkeypatch):
    """A figure id in a URL names a figure, not a permission. It has to be inside THIS project."""
    client, _service = draft_client
    monkeypatch.setattr(webapp.draft_figures, "get_figure", lambda figure_id, user_id: {
        "id": figure_id, "user_id": user_id, "project_id": 8,
    })

    class Studio(draft_studio_service.StudioService):
        def _project(self, _principal, project_id):
            return {"id": project_id, "user_id": USER["id"]}

    monkeypatch.setattr(webapp, "_studio", lambda: Studio(object(), repository=object()))
    response = client.post(
        "/drafts/7/figures/9/activate", json={"version_no": 1},
        headers={"X-CSRF-Token": "csrf-draft"})
    assert response.status_code == 404


def test_figure_version_activation_is_the_users_own_choice(draft_client, monkeypatch):
    """Choosing which of your own uploads is the live one is a decision, not a check.

    The gate that stood here compared a version's stored pixel audit against a hash of the current
    drawing specification. That was right while the product generated and inspected its own
    sheets. It uploads them now, so a version that "did not pass the current drawing
    specification" is a sentence about a check that no longer exists.
    """
    client, _service = draft_client
    chosen = {}

    class Studio:
        def activate_figure_version(self, principal, project_id, figure_id, version_no):
            chosen.update({"project": project_id, "figure": figure_id, "version": version_no})
            return version_no

    monkeypatch.setattr(webapp, "_studio", lambda: Studio())
    response = client.post(
        "/drafts/7/figures/9/activate", json={"version_no": 3},
        headers={"X-CSRF-Token": "csrf-draft"})
    assert response.status_code == 200 and response.get_json()["version_no"] == 3
    assert chosen == {"project": 7, "figure": 9, "version": 3}


def test_the_studio_ui_has_a_terminal_and_no_way_to_draw():
    """What the page offers, read from the page.

    Half of this is the absence of things. A sketch canvas, a photo-to-sketch box or a Draw button
    left in the bundle is a control somebody can still reach, whatever the routes behind it now
    answer, so their names are asserted gone rather than assumed gone.
    """
    root = Path(__file__).resolve().parents[1]
    script = (root / "static" / "draft_studio.js").read_text()
    template = (root / "templates" / "draft_studio.html").read_text()
    css = (root / "static" / "draft_studio.css").read_text()

    #  Nothing draws.
    for absent in ("photo-to-sketch", "Make line drawing", "Draw this figure", "Redraw",
                   "AI fix selected area", "Delete selected area", "Edit by hand",
                   "setupCanvasEditor", "discardDrawingEditor", "figure-compiler",
                   "Recheck drawings"):
        assert absent not in script, absent
    #  Uploading is the whole of the drawing feature.
    assert "studio/figure/upload" in script
    assert "Upload this sheet" in script and "Add a drawing" in script

    #  The agent is a terminal, and it is the ONE place a model is chosen: the composer's own
    #  "Server default" picker is gone with the turn queue it configured.
    assert "renderRawText" in script and "applyRawFilter" in script
    assert "terminal/tail" in script and "terminal/interrupt" in script
    assert "termModel" in template and "termEffort" in template
    assert "Server default" not in template and "stModel" not in template
    #  ...and none of the fleet controls the operators' dashboard carries beside it.
    for absent in ("Auto-push", "Idle nudge", "ADHD", "Clean view", "autopush"):
        assert absent not in template, absent
        assert absent not in script, absent

    #  Searching from the draft is ONE control now, in the Research panel under the draft, with
    #  an effort setting. It used to be three buttons in the Sources tab offering what read as
    #  the same thing, so Sources holds what the draft already has and nothing about looking for
    #  more.
    assert "researchPanel" in template and "rsEffort" in script
    assert "Use to redraft" in script
    #  The results are the report's own cards, fetched already rendered. A second card design in
    #  this bundle would be a second thing to keep in step with the report page. They come from
    #  THIS app's route: /api/cards belongs to the search app at the root of the domain, which
    #  keeps a different reports directory and cannot see a report the drafting app generated.
    assert "/research/${encodeURIComponent(slug)}/cards" in script
    assert "bindStreamedCards" in script
    assert "refcard" not in template, "the studio must not grow its own card markup"
    for absent in ("Search current draft", "importsearch", "quickartBody", "Run a quick pass"):
        assert absent not in script, absent
    assert "hashchange" in script
    assert "cache: 'no-store'" in script
    assert "refreshSerial" in script
    #  The terminal is sized to fit beside the draft and can be dragged, rather than filling the
    #  column the way the conversation panel did.
    assert "raw-resize-handle" in template and "startTermResize" in script
    assert ".raw-output {" in css


def test_report_draft_action_is_a_csrf_protected_direct_post():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "report.html").read_text()
    assert 'method="post" action="{{ request.script_root }}/drafts/start"' in template
    assert 'name="direct" value="1"' in template
    assert 'name="csrf_token"' in template


def test_legacy_create_redirects_to_the_gated_studio_intake(draft_client):
    client, service = draft_client
    response = client.post("/drafts/new", data={
        "csrf_token": "csrf-draft", "search_slug": "adhoc-owned", "title": "RFID lifter",
        "disclosure_text": "A detailed inventor disclosure with enough technical content to draft.",
        "inventor_notes": "Inventor: Dana", "pubs": ["US-11223344-B2"],
    })
    assert response.status_code == 307
    assert response.headers["Location"].endswith("/drafts/start")
    assert service.created is None


def test_draft_workspace_status_and_queue_are_authenticated_and_csrf_protected(draft_client, monkeypatch):
    client, service = draft_client
    page = client.get("/drafts/7")
    assert page.status_code == 302
    assert page.headers["Location"].endswith("/drafts/7/studio")
    denied = client.post("/drafts/7/generate", data={"instructions": "Apparatus claims"})
    assert denied.status_code == 400

    class Studio:
        sent = None

        def send_to_agent(self, principal, project_id, message):
            self.sent = (project_id, message)
            return {"sent": True}

    studio = Studio()
    monkeypatch.setattr(webapp, "_studio", lambda: studio)
    queued = client.post("/drafts/7/generate", data={
        "csrf_token": "csrf-draft", "instructions": "Apparatus claims",
        "idempotency_key": "one-click",
    })
    assert queued.status_code == 302
    assert queued.headers["Location"].endswith("/drafts/7/studio")
    #  The legacy one-click door now types into this draft's agent. One drafting agent per
    #  project and one way to reach it, rather than a second queue behind the same button.
    assert studio.sent == (7, "Apparatus claims")
    assert service.queued is None
    status = client.get("/api/drafts/7/status")
    assert status.status_code == 200
    payload = status.get_json()
    assert payload["latest_version_no"] == 1 and "system_prompt" not in str(payload)

    original = service.get_project
    service.get_project = lambda principal, project_id, include_versions=True: {
        **original(principal, project_id, include_versions), "status": "generating",
        "jobs": [{"id": 14, "status": "running", "attempts": 1, "max_attempts": 3}],
    }
    assert client.get("/api/drafts/7/status").get_json()["ready_url"] is None


def test_draft_markdown_and_docx_exports(draft_client):
    client, _service = draft_client
    markdown = client.get("/drafts/7/download/md?version=1")
    assert markdown.status_code == 200
    assert b"WORKING DRAFT" not in markdown.data
    assert b"[REF:" not in markdown.data
    assert b"U.S. Patent No. 11,223,344" in markdown.data
    word = client.get("/drafts/7/download/docx?version=1")
    assert word.status_code == 200 and len(word.data) > 10_000
    with zipfile.ZipFile(io.BytesIO(word.data)) as archive:
        document_xml = archive.read("word/document.xml")
    assert b"CLAIMS" in document_xml and b"ABSTRACT" in document_xml
    pdf = client.get("/drafts/7/download/pdf?version=1")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF") and len(pdf.data) > 3_000
    printed = client.get("/drafts/7/print?version=1")
    assert printed.status_code == 200
    assert b"[REF:" not in printed.data
    assert b"U.S. Patent No. 11,223,344" in printed.data


def test_legacy_section_edits_and_restore_cannot_publish_unreviewed_versions(draft_client):
    client, service = draft_client
    service.save_edited_version = lambda *args, **kwargs: pytest.fail(
        "legacy route published a version without the filing gate")
    edited = client.post("/drafts/7/versions", data={
        "csrf_token": "csrf-draft", "base_version_no": "1", **sections(),
    })
    assert edited.status_code == 302
    assert edited.headers["Location"].endswith("/drafts/7/studio")
    restored = client.post("/drafts/7/versions/1/restore", data={"csrf_token": "csrf-draft"})
    assert restored.status_code == 302
    assert restored.headers["Location"].endswith("/drafts/7/studio")
