"""Durable workflow service contracts for the patent figure compiler."""
from copy import deepcopy

import pytest

import drafting
import figure_compiler
import figure_compiler_service

SECTIONS = {
    "title": "Valve controller",
    "drawing_descriptions": "FIG. 1 is a block diagram of the valve controller.",
    "detailed_description": (
        "A valve controller 10 contains a processor 12 connected to a valve driver 14."
    ),
    "claims": (
        "1. A valve controller comprising a processor connected to a valve driver."
    ),
}
NUMERALS = [
    {"numeral": "10", "part": "valve controller"},
    {"numeral": "12", "part": "processor"},
    {"numeral": "14", "part": "valve driver"},
]
FIGURES = [{
    "label": "FIG. 1", "caption": "block diagram of the valve controller",
    "numerals": ["10 valve controller", "12 processor", "14 valve driver"],
}]


class FakeDraftingService:
    def __init__(self, numerals=None):
        self.numerals = deepcopy(NUMERALS if numerals is None else numerals)
        self.access_checks = 0

    def get_project(self, principal, project_id, include_versions=True):
        principal.require_active()
        self.access_checks += 1
        if principal.user_id != 91:
            raise drafting.DraftingNotFound("Draft not found")
        version = {
            "project_id": project_id, "version_no": 4, "sections": deepcopy(SECTIONS),
            "numerals": deepcopy(self.numerals), "figure_specs": deepcopy(FIGURES),
        }
        return {"id": project_id, "user_id": 91, "latest_version_no": 4,
                "versions": [version] if include_versions else []}

    def get_version(self, principal, project_id, version_no):
        self.get_project(principal, project_id, include_versions=False)
        return {"project_id": project_id, "version_no": version_no,
                "sections": deepcopy(SECTIONS), "numerals": deepcopy(self.numerals),
                "figure_specs": deepcopy(FIGURES)}


class MemoryRepository:
    def __init__(self):
        self.runs = []
        self.artifacts = []
        self.patches = []

    def create_run(self, project_id, owner_user_id, draft_version_no, ruleset):
        for run in self.runs:
            run["active"] = False
        run = {"id": len(self.runs) + 1, "project_id": project_id,
               "owner_user_id": owner_user_id, "draft_version_no": draft_version_no,
               "ruleset": ruleset, "stage": "INGESTED", "active": True}
        self.runs.append(run)
        return deepcopy(run)

    def latest_run(self, project_id):
        return deepcopy(next((run for run in reversed(self.runs)
                              if run["project_id"] == project_id and run["active"]), None))

    def set_stage(self, run_id, stage):
        run = next(row for row in self.runs if row["id"] == run_id)
        run["stage"] = stage
        return deepcopy(run)

    def save_artifact(self, run_id, artifact_type, payload, created_by_user_id, state="draft",
                      parent_artifact_id=None):
        prior = [row for row in self.artifacts
                 if row["run_id"] == run_id and row["artifact_type"] == artifact_type]
        for row in prior:
            if row["state"] == "draft":
                row["state"] = "superseded"
        item = {"id": len(self.artifacts) + 1, "run_id": run_id,
                "artifact_type": artifact_type, "version_no": len(prior) + 1,
                "state": state, "payload": deepcopy(payload),
                "content_sha256": figure_compiler.content_hash(payload),
                "created_by_user_id": created_by_user_id,
                "parent_artifact_id": parent_artifact_id}
        self.artifacts.append(item)
        return deepcopy(item)

    def latest_artifact(self, run_id, artifact_type, state=None):
        rows = [row for row in self.artifacts if row["run_id"] == run_id
                and row["artifact_type"] == artifact_type
                and (state is None or row["state"] == state)]
        return deepcopy(rows[-1] if rows else None)

    def artifacts_for_run(self, run_id):
        return deepcopy([row for row in self.artifacts if row["run_id"] == run_id])

    def record_patch(self, run_id, package_artifact_id, patch, created_by_user_id):
        item = {"id": len(self.patches) + 1, "run_id": run_id,
                "package_artifact_id": package_artifact_id, "patch": deepcopy(patch),
                "created_by_user_id": created_by_user_id}
        self.patches.append(item)
        return deepcopy(item)


def service(numerals=None):
    drafts, repository = FakeDraftingService(numerals), MemoryRepository()
    return figure_compiler_service.FigureCompilerService(drafts, repository), drafts, repository


def test_full_three_gate_workflow_persists_versioned_artifacts_and_exports():
    compiler, drafts, repository = service()
    principal = drafting.Principal(91)

    started = compiler.start(principal, 7, ruleset="uspto-letter-2026.1")
    assert started["run"]["stage"] == "MODEL_RECONCILED"
    assert started["pir"]["entities"]
    assert drafts.access_checks == 1

    planned = compiler.approve_model(principal, 7)
    assert planned["run"]["stage"] == "FIGURES_PLANNED"
    assert planned["manifest"]["figures"]
    assert repository.latest_artifact(1, "canonical_model", "approved")

    manifest = compiler.approve_manifest(principal, 7)
    assert manifest["run"]["stage"] == "MANIFEST_APPROVED"
    assert repository.latest_artifact(1, "figure_manifest", "approved")

    compiled = compiler.compile(principal, 7)
    assert compiled["run"]["stage"] == "FINAL_REVIEW"
    assert compiled["validation"]["approved_for_export"] is True
    assert compiled["package"]["sheets"][0]["svg"].startswith("<svg")

    final = compiler.approve_final(principal, 7)
    assert final["run"]["stage"] == "APPROVED"
    assert repository.latest_artifact(1, "compiled_package", "approved")

    svg = compiler.export(principal, 7, "svg", sheet=1)
    pdf = compiler.export(principal, 7, "pdf")
    assert svg.startswith(b"<svg")
    assert pdf.startswith(b"%PDF-")
    assert compiler.state(principal, 7)["run"]["stage"] == "EXPORTED"


def test_compile_cannot_skip_the_manifest_approval_gate():
    compiler, _drafts, _repository = service()
    principal = drafting.Principal(91)
    compiler.start(principal, 7)
    compiler.approve_model(principal, 7)

    with pytest.raises(figure_compiler.ApprovalRequired, match="manifest"):
        compiler.compile(principal, 7)


def test_start_rejects_a_malformed_draft_version_as_a_safe_validation_error():
    compiler, _drafts, _repository = service()
    with pytest.raises(figure_compiler.FigureCompilerError, match="Draft version"):
        compiler.start(drafting.Principal(91), 7, version_no="not-a-number")


def test_patch_is_a_new_audited_package_version_and_is_revalidated():
    compiler, _drafts, repository = service()
    principal = drafting.Principal(91)
    compiler.start(principal, 7)
    compiler.approve_model(principal, 7)
    compiler.approve_manifest(principal, 7)
    before = compiler.compile(principal, 7)["package"]

    after = compiler.patch(principal, 7, {
        "type": "move_label", "figure_id": "figure-1", "reference": "12",
        "x": 510, "y": 410, "reason": "Clear the leader crossing",
    })

    assert after["package"]["artifact_version"] == before["artifact_version"] + 1
    assert len(repository.patches) == 1
    assert after["validation"]["approved_for_export"] is True


def test_final_approval_revalidates_the_exact_current_package():
    compiler, _drafts, repository = service()
    principal = drafting.Principal(91)
    compiler.start(principal, 7)
    compiler.approve_model(principal, 7)
    compiler.approve_manifest(principal, 7)
    compiler.compile(principal, 7)
    package_row = next(row for row in repository.artifacts
                       if row["artifact_type"] == "compiled_package" and row["state"] == "draft")
    package_row["payload"]["figures"][0]["entities"].append({
        "entity_id": "entity-999", "reference": "999", "name": "stored injection",
        "source_span_ids": ["span-forged"], "x": 100, "y": 100, "width": 50, "height": 50,
        "shape": "box",
    })

    with pytest.raises(figure_compiler.CompilationBlocked, match="hard blocker"):
        compiler.approve_final(principal, 7)


def test_material_registry_conflict_blocks_model_approval():
    compiler, _drafts, _repository = service(
        NUMERALS + [{"numeral": "12", "part": "battery"}])
    principal = drafting.Principal(91)
    started = compiler.start(principal, 7)
    assert started["pir"]["reference_conflicts"]

    with pytest.raises(figure_compiler.CompilationBlocked, match="canonical model"):
        compiler.approve_model(principal, 7)

    conflict = started["pir"]["reference_conflicts"][0]
    reconciled = compiler.resolve_model_conflict(
        principal, 7, conflict_id=conflict["id"], choice="processor")
    assert reconciled["pir"]["reference_conflicts"][0]["status"] == "resolved"
    assert compiler.approve_model(principal, 7)["run"]["stage"] == "FIGURES_PLANNED"


def test_every_service_operation_rechecks_project_ownership():
    compiler, drafts, _repository = service()
    owner = drafting.Principal(91)
    compiler.start(owner, 7)

    with pytest.raises(drafting.DraftingNotFound):
        compiler.state(drafting.Principal(92), 7)
    assert drafts.access_checks == 2


def test_approved_run_cannot_be_regressed_or_edited_by_direct_api_calls():
    compiler, _drafts, _repository = service()
    principal = drafting.Principal(91)
    compiler.start(principal, 7)
    compiler.approve_model(principal, 7)
    compiler.approve_manifest(principal, 7)
    compiler.compile(principal, 7)
    compiler.approve_final(principal, 7)

    with pytest.raises(figure_compiler.CompilationBlocked, match="approved and locked"):
        compiler.compile(principal, 7)
    with pytest.raises(figure_compiler.CompilationBlocked, match="approved and locked"):
        compiler.patch(principal, 7, {
            "type": "move_label", "figure_id": "figure-1", "reference": "12",
            "x": 510, "y": 410,
        })
    with pytest.raises(figure_compiler.CompilationBlocked, match="approved and locked"):
        compiler.approve_model(principal, 7)

    assert compiler.export(principal, 7, "svg").startswith(b"<svg")
    assert compiler.export(principal, 7, "pdf").startswith(b"%PDF-")


def test_state_recomputes_an_unapproved_package_when_validator_version_changes(monkeypatch):
    compiler, _drafts, repository = service()
    principal = drafting.Principal(91)
    compiler.start(principal, 7)
    compiler.approve_model(principal, 7)
    compiler.approve_manifest(principal, 7)
    compiler.compile(principal, 7)
    report = next(row for row in reversed(repository.artifacts)
                  if row["artifact_type"] == "validation_report")
    report["payload"]["validator_version"] = "figure-validator-old"
    original = figure_compiler.validate_package
    calls = []

    def observed(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(figure_compiler, "validate_package", observed)
    current = compiler.state(principal, 7)

    assert calls == [True]
    assert current["validation"]["validator_version"] == figure_compiler.VALIDATOR_VERSION
