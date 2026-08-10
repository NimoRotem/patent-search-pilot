"""Authenticated web and SPA contracts for the filing-drawing compiler."""
from pathlib import Path

import pytest

import accounts
import auth
import webapp

USER = {"id": 91, "email": "drafter@example.test", "full_name": "Dana Drafter",
        "is_admin": False, "is_active": True, "session_version": 2}


class FakeCompiler:
    def __init__(self):
        self.calls = []

    def _state(self, stage="MODEL_RECONCILED"):
        return {"run": {"id": 3, "stage": stage, "draft_version_no": 4,
                        "ruleset": "uspto-letter-2026.1"},
                "pir": {"entities": [{"id": "entity-10", "reference": "10",
                                        "name": "controller", "source_span_ids": ["s1"]}],
                        "relations": [], "claim_coverage": [], "reference_conflicts": [],
                        "hard_blockers": []},
                "manifest": None, "package": None, "validation": None, "artifacts": []}

    def state(self, principal, project_id):
        self.calls.append(("state", principal.user_id, project_id))
        return self._state()

    def start(self, principal, project_id, **values):
        self.calls.append(("start", principal.user_id, project_id, values))
        return self._state()

    def approve_model(self, principal, project_id):
        self.calls.append(("approve_model", principal.user_id, project_id))
        state = self._state("FIGURES_PLANNED")
        state["manifest"] = {"figures": []}
        return state

    def resolve_model_conflict(self, principal, project_id, **values):
        self.calls.append(("resolve_model", principal.user_id, project_id, values))
        return self._state()

    def approve_manifest(self, principal, project_id):
        self.calls.append(("approve_manifest", principal.user_id, project_id))
        return self._state("MANIFEST_APPROVED")

    def compile(self, principal, project_id):
        self.calls.append(("compile", principal.user_id, project_id))
        state = self._state("FINAL_REVIEW")
        state["validation"] = {"approved_for_export": True, "issues": [], "hard_blockers": 0}
        return state

    def patch(self, principal, project_id, patch):
        self.calls.append(("patch", principal.user_id, project_id, patch))
        return self.compile(principal, project_id)

    def approve_final(self, principal, project_id):
        self.calls.append(("approve_final", principal.user_id, project_id))
        return self._state("APPROVED")

    def export(self, principal, project_id, format_name, sheet=1):
        self.calls.append(("export", principal.user_id, project_id, format_name, sheet))
        return b"%PDF-compiler" if format_name == "pdf" else b"<svg>compiler</svg>"


@pytest.fixture()
def compiler_client(monkeypatch):
    service = FakeCompiler()
    webapp.app.config.update(TESTING=True, FORCE_AUTH=True, FORCE_ACCOUNTS=True)
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    monkeypatch.setattr(accounts, "get_user",
                        lambda uid: dict(USER) if int(uid) == USER["id"] else None)
    monkeypatch.setattr(webapp, "_figure_compiler", lambda: service)
    auth.reset_limits()
    client = webapp.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = USER["id"]
        session["session_version"] = USER["session_version"]
        session["csrf_token"] = "csrf-figures"
    try:
        yield client, service
    finally:
        for key in ("FORCE_AUTH", "FORCE_ACCOUNTS"):
            webapp.app.config.pop(key, None)
        auth.reset_limits()


def test_compiler_state_and_every_mutation_are_account_owned_and_csrf_protected(compiler_client):
    client, service = compiler_client
    assert client.get("/api/drafts/7/figure-compiler").status_code == 200

    endpoints = [
        ("/drafts/7/figure-compiler/start", {"ruleset": "pct-a4-2026.1"}),
        ("/drafts/7/figure-compiler/model/approve", {}),
        ("/drafts/7/figure-compiler/model/resolve", {
            "conflict_id": "conflict-1", "choice": "controller",
        }),
        ("/drafts/7/figure-compiler/manifest/approve", {}),
        ("/drafts/7/figure-compiler/compile", {}),
        ("/drafts/7/figure-compiler/patch", {
            "type": "move_label", "figure_id": "figure-1", "reference": "10",
            "x": 400, "y": 500,
        }),
        ("/drafts/7/figure-compiler/approve", {}),
    ]
    for endpoint, payload in endpoints:
        denied = client.post(endpoint, json=payload)
        assert denied.status_code == 400
        allowed = client.post(endpoint, json=payload,
                              headers={"X-CSRF-Token": "csrf-figures"})
        assert allowed.status_code == 200, allowed.get_data(as_text=True)
        assert allowed.get_json()["ok"] is True
    assert all(call[1] == USER["id"] for call in service.calls)


def test_approved_svg_and_pdf_exports_have_safe_download_contracts(compiler_client):
    client, service = compiler_client
    svg = client.get("/drafts/7/figure-compiler/export.svg?sheet=2")
    pdf = client.get("/drafts/7/figure-compiler/export.pdf")

    assert svg.status_code == 200 and svg.mimetype == "image/svg+xml"
    assert "attachment" in svg.headers["Content-Disposition"]
    assert pdf.status_code == 200 and pdf.mimetype == "application/pdf"
    assert "attachment" in pdf.headers["Content-Disposition"]
    assert ("export", 91, 7, "svg", 2) in service.calls


def test_studio_has_hash_routed_compiler_pane_and_no_page_navigation():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "draft_studio.html").read_text()
    script = (root / "static" / "draft_studio.js").read_text()

    assert 'data-pane="compiler"' in template
    assert 'id="pane-compiler"' in template
    assert "'compiler'" in script
    assert "renderCompiler" in script
    assert "#/compiler" in script
    assert "compilerSvgUrl" in script
    assert "move_entity" in script
    assert "reroute_leader" in script
    assert "delete_visible_entity" in script
    assert '<div class="compilersvg">${sheet.svg}</div>' not in script
