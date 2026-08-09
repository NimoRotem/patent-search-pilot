"""Authenticated Flask contracts for the versioned drafting workspace."""
import io
import zipfile
from pathlib import Path

import pytest

import accounts
import auth
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
        "cross_reference": "[DRAFTING NOTE: Confirm related applications.]",
        "field": "The disclosure relates to portable vacuum lifting tools.",
        "background": "Accessory readers are described in selected art [REF:US-11223344-B2].",
        "summary": "A handle identifies an attached base plate.",
        "drawing_descriptions": "[DRAFTING NOTE: Supply figure descriptions.]",
        "detailed_description": "A battery-powered pump is disposed in the handle.",
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
    monkeypatch.setattr(webapp, "report_path", lambda slug: type("P", (), {"exists": lambda self: True})())
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
    assert "US patent drafts" in library.get_data(as_text=True)
    intake = client.get("/drafts/new?search_slug=adhoc-owned")
    assert intake.status_code == 200
    body = intake.get_data(as_text=True)
    assert "inventor disclosure is the source of truth" in body.lower()
    assert "US-11223344-B2" in body
    assert "Verbatim full uploaded disclosure with all four claims." in body
    assert "Full uploaded disclosure loaded" in body


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
    assert 'name="inventors"' in body and "Dana Drafter" in body
    assert "Anything else the drafter should know" not in body
    assert 'name="priority_status"' in body
    assert 'name="claim_strategy"' in body
    assert 'name="government_support"' in body


def test_intake_uses_profile_name_when_no_inventor_default_exists(draft_client, monkeypatch):
    client, _service = draft_client
    user = {**USER, "default_inventors": ""}
    monkeypatch.setattr(accounts, "get_user", lambda uid: dict(user))
    body = client.get("/drafts/start").get_data(as_text=True)
    assert '<textarea id="inventors" name="inventors"' in body
    assert '>Dana Drafter</textarea>' in body


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


def test_studio_exposes_manual_photo_and_delete_drawing_actions(draft_client, monkeypatch):
    client, _service = draft_client

    class Studio:
        calls = []

        def save_figure(self, principal, project_id, figure_id, png, instruction=""):
            self.calls.append(("save", project_id, figure_id, png, instruction))
            return {"figure_id": figure_id, "version_no": 2}

        def photo_to_sketch(self, principal, project_id, **values):
            self.calls.append(("photo", project_id, values))
            return {"figure_id": 10, "version_no": 1}

        def delete_figure(self, principal, project_id, figure_id):
            self.calls.append(("delete", project_id, figure_id))

    studio = Studio()
    monkeypatch.setattr(webapp, "_studio", lambda: studio)
    edited = client.post("/drafts/7/studio/figure/9/manual", data={
        "image": (io.BytesIO(b"png bytes"), "edited.png"), "instruction": "move 12",
        "csrf_token": "csrf-draft",
    })
    assert edited.status_code == 200 and studio.calls[-1][0] == "save"
    photo = client.post("/drafts/7/studio/photo-to-sketch", data={
        "image": (io.BytesIO(b"jpeg bytes"), "part.jpg"), "caption": "front view",
        "csrf_token": "csrf-draft",
    })
    assert photo.status_code == 200 and studio.calls[-1][0] == "photo"
    deleted = client.post("/drafts/7/studio/figure/9/delete", json={},
                          headers={"X-CSRF-Token": "csrf-draft"})
    assert deleted.status_code == 200 and studio.calls[-1] == ("delete", 7, 9)


def test_figure_version_actions_cannot_cross_between_the_users_projects(
        draft_client, monkeypatch):
    client, _service = draft_client
    monkeypatch.setattr(webapp.draft_figures, "get_figure", lambda figure_id, user_id: {
        "id": figure_id, "user_id": user_id, "project_id": 8,
    })
    response = client.post(
        "/drafts/7/figures/9/activate", json={"version_no": 1},
        headers={"X-CSRF-Token": "csrf-draft"})
    assert response.status_code == 404


def test_studio_ui_exposes_sketch_tools_and_reload_safe_navigation():
    root = Path(__file__).resolve().parents[1]
    script = (root / "static" / "draft_studio.js").read_text()
    css = (root / "static" / "style.css").read_text()
    assert "Select and move" in script and "Delete selected area" in script
    assert "AI fix selected area" in script and "photo-to-sketch" in script
    assert "Search current draft" in script and "importsearch" in script
    assert "hashchange" in script and "#/figures/" in script
    assert "cache: 'no-store'" in script
    assert "refreshSerial" in script
    assert ".chatstatus[hidden]{display:none!important}" in css


def test_report_draft_action_is_a_csrf_protected_direct_post():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "report.html").read_text()
    assert 'method="post" action="{{ request.script_root }}/drafts/start"' in template
    assert 'name="direct" value="1"' in template
    assert 'name="csrf_token"' in template


def test_create_draft_uses_server_report_and_selected_publications(draft_client):
    client, service = draft_client
    response = client.post("/drafts/new", data={
        "csrf_token": "csrf-draft", "search_slug": "adhoc-owned", "title": "RFID lifter",
        "disclosure_text": "A detailed inventor disclosure with enough technical content to draft.",
        "inventor_notes": "Inventor: Dana", "pubs": ["US-11223344-B2"],
    })
    assert response.status_code == 302 and response.headers["Location"].endswith("/drafts/7?created=1")
    assert service.created["search_slug"] == "adhoc-owned"
    assert service.selected == (7, ["US-11223344-B2"], "atomic")


def test_draft_workspace_status_and_queue_are_authenticated_and_csrf_protected(draft_client, monkeypatch):
    client, service = draft_client
    page = client.get("/drafts/7")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Application working draft" in body and "Download Word" in body
    denied = client.post("/drafts/7/generate", data={"instructions": "Apparatus claims"})
    assert denied.status_code == 400
    monkeypatch.setattr(webapp.draft_worker, "kick", lambda: None)
    queued = client.post("/drafts/7/generate", data={
        "csrf_token": "csrf-draft", "instructions": "Apparatus claims",
        "idempotency_key": "one-click",
    })
    assert queued.status_code == 302
    assert service.queued == (7, {"instructions": "Apparatus claims", "idempotency_key": "one-click"})
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
    assert b"WORKING DRAFT" in markdown.data and b"US-11223344-B2" in markdown.data
    word = client.get("/drafts/7/download/docx?version=1")
    assert word.status_code == 200 and len(word.data) > 10_000
    with zipfile.ZipFile(io.BytesIO(word.data)) as archive:
        document_xml = archive.read("word/document.xml")
    assert b"CLAIMS" in document_xml and b"ABSTRACT" in document_xml
    pdf = client.get("/drafts/7/download/pdf?version=1")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF") and len(pdf.data) > 3_000


def test_restore_version_creates_a_new_immutable_version(draft_client):
    client, _service = draft_client
    restored = client.post("/drafts/7/versions/1/restore", data={"csrf_token": "csrf-draft"})
    assert restored.status_code == 302
    assert "version=2" in restored.headers["Location"]
