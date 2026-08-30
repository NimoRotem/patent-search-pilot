"""Owned persistence and workflow gates for :mod:`figure_compiler`."""
from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

import draft_workspace
import drafting
import figure_compiler

try:
    import db
except ModuleNotFoundError:  # pure/service tests can provide an in-memory repository
    db = None


_MIGRATION = Path(__file__).resolve().parents[1] / "sql" / "007_figure_compiler.sql"
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def _json(value: Any, fallback: Any) -> Any:
    if value is None:
        return copy.deepcopy(fallback)
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return copy.deepcopy(fallback)


def ensure_schema(force: bool = False) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    if db is None:
        raise RuntimeError("Postgres is required for the figure compiler repository.")
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return
        import draft_studio
        draft_studio.ensure_schema()
        with db.cursor(autocommit=True) as cur:
            sql = _MIGRATION.read_text(encoding="utf-8")
            try:
                cur.execute(sql, prepare=False)
            except TypeError:
                cur.execute(sql)
        _SCHEMA_READY = True


class FigureCompilerRepository:
    def _ready(self) -> None:
        ensure_schema()

    @staticmethod
    def _decode(row: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
        if not row:
            return None
        out = dict(row)
        if "payload" in out:
            out["payload"] = _json(out.get("payload"), {})
        return out

    def create_run(self, project_id: int, owner_user_id: int, draft_version_no: int,
                   ruleset: str) -> dict[str, Any]:
        self._ready()
        with db.cursor() as cur:
            cur.execute("UPDATE app_figure_compiler_runs SET active=false,updated_at=now() "
                        "WHERE project_id=%s AND active", (int(project_id),))
            cur.execute(
                "INSERT INTO app_figure_compiler_runs "
                "(project_id,owner_user_id,draft_version_no,ruleset,extractor_version,"
                "renderer_version) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (int(project_id), int(owner_user_id), int(draft_version_no), str(ruleset),
                 figure_compiler.PIR_SCHEMA_VERSION, figure_compiler.RENDERER_VERSION))
            return dict(cur.fetchone())

    def latest_run(self, project_id: int) -> Optional[dict[str, Any]]:
        self._ready()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM app_figure_compiler_runs WHERE project_id=%s AND active "
                        "ORDER BY id DESC LIMIT 1", (int(project_id),))
            row = cur.fetchone()
            return dict(row) if row else None

    def set_stage(self, run_id: int, stage: str) -> dict[str, Any]:
        if stage not in figure_compiler.WORKFLOW_STAGES:
            raise figure_compiler.FigureCompilerError("Unknown compiler workflow stage.")
        self._ready()
        with db.cursor() as cur:
            cur.execute("UPDATE app_figure_compiler_runs SET stage=%s,updated_at=now() "
                        "WHERE id=%s RETURNING *", (stage, int(run_id)))
            row = cur.fetchone()
            if not row:
                raise drafting.DraftingNotFound("Figure compiler run was not found.")
            return dict(row)

    def save_artifact(self, run_id: int, artifact_type: str, payload: Mapping[str, Any],
                      created_by_user_id: int, state: str = "draft",
                      parent_artifact_id: Optional[int] = None) -> dict[str, Any]:
        if state not in {"draft", "approved"}:
            raise figure_compiler.FigureCompilerError("Invalid compiler artifact state.")
        self._ready()
        encoded = json.dumps(dict(payload), sort_keys=True, ensure_ascii=False, default=str)
        with db.cursor() as cur:
            cur.execute("SELECT id FROM app_figure_compiler_runs WHERE id=%s FOR UPDATE",
                        (int(run_id),))
            if not cur.fetchone():
                raise drafting.DraftingNotFound("Figure compiler run was not found.")
            cur.execute("SELECT coalesce(max(version_no),0)+1 AS n "
                        "FROM app_figure_compiler_artifacts WHERE run_id=%s AND artifact_type=%s",
                        (int(run_id), artifact_type))
            version_no = int(cur.fetchone()["n"])
            cur.execute("UPDATE app_figure_compiler_artifacts SET state='superseded' "
                        "WHERE run_id=%s AND artifact_type=%s AND state='draft'",
                        (int(run_id), artifact_type))
            cur.execute(
                "INSERT INTO app_figure_compiler_artifacts "
                "(run_id,artifact_type,version_no,state,payload,content_sha256,"
                "parent_artifact_id,created_by_user_id,approved_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,CASE WHEN %s='approved' THEN now() END) "
                "RETURNING *",
                (int(run_id), artifact_type, version_no, state, encoded,
                 figure_compiler.content_hash(payload), parent_artifact_id,
                 int(created_by_user_id), state))
            return self._decode(cur.fetchone()) or {}

    def latest_artifact(self, run_id: int, artifact_type: str,
                        state: Optional[str] = None) -> Optional[dict[str, Any]]:
        self._ready()
        sql = ("SELECT * FROM app_figure_compiler_artifacts WHERE run_id=%s "
               "AND artifact_type=%s")
        params: list[Any] = [int(run_id), artifact_type]
        if state is not None:
            sql += " AND state=%s"
            params.append(state)
        sql += " ORDER BY version_no DESC LIMIT 1"
        with db.cursor() as cur:
            cur.execute(sql, tuple(params))
            return self._decode(cur.fetchone())

    def artifacts_for_run(self, run_id: int) -> list[dict[str, Any]]:
        self._ready()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM app_figure_compiler_artifacts WHERE run_id=%s "
                        "ORDER BY id", (int(run_id),))
            return [self._decode(row) or {} for row in cur.fetchall()]

    def record_patch(self, run_id: int, package_artifact_id: int, patch: Mapping[str, Any],
                     created_by_user_id: int) -> dict[str, Any]:
        self._ready()
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO app_figure_compiler_patches "
                "(run_id,package_artifact_id,patch,created_by_user_id) "
                "VALUES (%s,%s,%s::jsonb,%s) RETURNING *",
                (int(run_id), int(package_artifact_id),
                 json.dumps(dict(patch), sort_keys=True), int(created_by_user_id)))
            out = dict(cur.fetchone())
            out["patch"] = _json(out.get("patch"), {})
            return out


def _derive_figure_specs(sections: Mapping[str, str],
                         numerals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make an approval-ready plan from disclosed descriptions when the agent supplied none."""
    descriptions = str(sections.get("drawing_descriptions") or "")
    matches = list(re.finditer(
        r"(?ims)\b(FIG(?:URE)?\.?\s*[0-9]+[A-Za-z]?)\b\s*(?:is|shows|illustrates)?\s*"
        r"(.*?)(?=\bFIG(?:URE)?\.?\s*[0-9]+[A-Za-z]?\b|\Z)", descriptions))
    rows = []
    for index, match in enumerate(matches, 1):
        caption = re.sub(r"^[\s,:;-]+|[\s.;]+$", "", match.group(2))[:1000]
        selected = [f"{item['numeral']} {item['part']}" for item in numerals
                    if str(item.get("part") or "").lower() in caption.lower()]
        rows.append({"label": match.group(1), "caption": caption or "disclosed view",
                     "numerals": selected})
    if not rows and numerals:
        rows = [{"label": "FIG. 1", "caption": "overview of the disclosed components",
                 "numerals": [f"{item['numeral']} {item['part']}" for item in numerals]}]
    assigned = {re.match(r"\s*([A-Za-z]?\d{1,4}[A-Za-z]?)", value).group(1).upper()
                for row in rows for value in row["numerals"]
                if re.match(r"\s*([A-Za-z]?\d{1,4}[A-Za-z]?)", value)}
    if rows:
        rows[0]["numerals"].extend(
            f"{item['numeral']} {item['part']}" for item in numerals
            if str(item.get("numeral") or "").upper() not in assigned)
    return rows


class FigureCompilerService:
    def __init__(self, drafting_service: drafting.DraftingService,
                 repository: Optional[FigureCompilerRepository] = None):
        self.drafting_service = drafting_service
        self.repository = repository or FigureCompilerRepository()

    def _project(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        return self.drafting_service.get_project(principal, project_id, include_versions=True)

    def _run(self, project_id: int) -> dict[str, Any]:
        run = self.repository.latest_run(project_id)
        if not run:
            raise drafting.DraftingNotFound("Start the figure compiler for this draft first.")
        return run

    @staticmethod
    def _require_stage(run: Mapping[str, Any], allowed: set[str], action: str) -> None:
        stage = str(run.get("stage") or "")
        if stage in {"APPROVED", "EXPORTED"} and stage not in allowed:
            raise figure_compiler.CompilationBlocked(
                "This compiler run is approved and locked. Start a new run to revise it.")
        if stage not in allowed:
            raise figure_compiler.ApprovalRequired(
                f"{action} is not available while the compiler is at {stage or 'an unknown stage'}.")

    def _version(self, principal: drafting.Principal, project: Mapping[str, Any],
                 version_no: int) -> dict[str, Any]:
        version = next((row for row in project.get("versions") or ()
                        if int(row.get("version_no") or 0) == int(version_no)), None)
        return dict(version) if version else self.drafting_service.get_version(
            principal, int(project["id"]), int(version_no))

    def _state_for_run(self, run: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        empty = {"run": None, "pir": None, "manifest": None, "package": None,
                 "validation": None, "artifacts": []}
        if not run:
            return empty
        artifacts = self.repository.artifacts_for_run(int(run["id"]))
        def latest(kind: str, approved_first: bool = False):
            rows = [row for row in artifacts if row["artifact_type"] == kind]
            if approved_first:
                approved = [row for row in rows if row["state"] == "approved"]
                if approved:
                    return approved[-1]
            current = [row for row in rows if row["state"] != "superseded"]
            return (current or rows)[-1] if rows else None
        pir_row = latest("canonical_model", True) or latest("patent_intermediate_representation")
        manifest_row = latest("figure_manifest", True)
        package_row = latest("compiled_package", True)
        validation_row = latest("validation_report")
        return {
            "run": dict(run),
            "pir": copy.deepcopy((pir_row or {}).get("payload")),
            "manifest": copy.deepcopy((manifest_row or {}).get("payload")),
            "package": copy.deepcopy((package_row or {}).get("payload")),
            "validation": copy.deepcopy((validation_row or {}).get("payload")),
            "artifacts": [{key: row.get(key) for key in
                           ("id", "artifact_type", "version_no", "state", "content_sha256",
                            "created_at", "approved_at", "parent_artifact_id")}
                          for row in artifacts],
        }

    def state(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        self._project(principal, project_id)
        run = self.repository.latest_run(project_id)
        state = self._state_for_run(run)
        package = state.get("package") or {}
        validation = state.get("validation") or {}
        if (run and package and not package.get("approval") and state.get("pir") and
                state.get("manifest") and
                validation.get("validator_version") != figure_compiler.VALIDATOR_VERSION):
            state["validation"] = figure_compiler.validate_package(
                state["pir"], state["manifest"], package, str(run["ruleset"]))
        return state

    def start(self, principal: drafting.Principal, project_id: int, *,
              version_no: Optional[int] = None,
              ruleset: str = "uspto-letter-2026.1") -> dict[str, Any]:
        project = self._project(principal, project_id)
        figure_compiler.load_ruleset(ruleset)
        try:
            selected = int(version_no or project.get("latest_version_no") or 0)
        except (TypeError, ValueError) as exc:
            raise figure_compiler.FigureCompilerError(
                "Draft version must be a positive whole number.") from exc
        if selected <= 0:
            raise drafting.DraftingNotFound("Write a draft version before compiling figures.")
        version = self._version(principal, project, selected)
        sections = _json(version.get("sections"), {})
        numerals = _json(version.get("numerals"), []) or draft_workspace.numerals_from_sections(sections)
        specs = _json(version.get("figure_specs"), []) or _derive_figure_specs(sections, numerals)
        run = self.repository.create_run(project_id, principal.user_id, selected, ruleset)
        snapshot = {"schema_version": "figure-ingest-1", "draft_version_no": selected,
                    "sections": sections, "numerals": numerals, "figure_specs": specs}
        self.repository.save_artifact(run["id"], "ingest_snapshot", snapshot,
                                      principal.user_id)
        pir = figure_compiler.build_pir(sections, numerals, specs)
        self.repository.save_artifact(run["id"], "patent_intermediate_representation", pir,
                                      principal.user_id)
        run = self.repository.set_stage(run["id"], "MODEL_RECONCILED")
        return self._state_for_run(run)

    def approve_model(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        self._project(principal, project_id)
        run = self._run(project_id)
        self._require_stage(run, {"MODEL_RECONCILED"}, "Canonical-model approval")
        pir_row = self.repository.latest_artifact(
            run["id"], "patent_intermediate_representation")
        if not pir_row:
            raise drafting.DraftingNotFound("The canonical model has not been extracted.")
        pir = pir_row["payload"]
        if pir.get("hard_blockers"):
            raise figure_compiler.CompilationBlocked(
                "Resolve the canonical model blockers before approval.")
        approved = figure_compiler.approve_artifact(
            pir, artifact_type="canonical_model", user_id=principal.user_id)
        self.repository.save_artifact(run["id"], "canonical_model", approved,
                                      principal.user_id, state="approved",
                                      parent_artifact_id=pir_row["id"])
        self.repository.set_stage(run["id"], "MODEL_APPROVED")
        snapshot = self.repository.latest_artifact(run["id"], "ingest_snapshot")
        manifest = figure_compiler.plan_manifest(
            approved, (snapshot or {}).get("payload", {}).get("figure_specs") or [])
        self.repository.save_artifact(run["id"], "figure_manifest", manifest,
                                      principal.user_id)
        run = self.repository.set_stage(run["id"], "FIGURES_PLANNED")
        return self._state_for_run(run)

    def resolve_model_conflict(self, principal: drafting.Principal, project_id: int, *,
                               conflict_id: str, choice: str) -> dict[str, Any]:
        self._project(principal, project_id)
        run = self._run(project_id)
        self._require_stage(run, {"MODEL_RECONCILED"}, "Reference reconciliation")
        row = self.repository.latest_artifact(
            run["id"], "patent_intermediate_representation", "draft")
        if not row:
            raise drafting.DraftingNotFound("The editable canonical model was not found.")
        reconciled = figure_compiler.resolve_reference_conflict(
            row["payload"], conflict_id=str(conflict_id), choice=str(choice),
            resolved_by_user_id=principal.user_id)
        self.repository.save_artifact(
            run["id"], "patent_intermediate_representation", reconciled,
            principal.user_id, parent_artifact_id=row["id"])
        run = self.repository.set_stage(run["id"], "MODEL_RECONCILED")
        return self._state_for_run(run)

    def approve_manifest(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        self._project(principal, project_id)
        run = self._run(project_id)
        self._require_stage(run, {"FIGURES_PLANNED"}, "Figure-manifest approval")
        row = self.repository.latest_artifact(run["id"], "figure_manifest")
        if not row:
            raise drafting.DraftingNotFound("Approve the canonical model before the manifest.")
        approved = figure_compiler.approve_artifact(
            row["payload"], artifact_type="figure_manifest", user_id=principal.user_id)
        self.repository.save_artifact(run["id"], "figure_manifest", approved,
                                      principal.user_id, state="approved",
                                      parent_artifact_id=row["id"])
        run = self.repository.set_stage(run["id"], "MANIFEST_APPROVED")
        return self._state_for_run(run)

    def compile(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        self._project(principal, project_id)
        run = self._run(project_id)
        self._require_stage(
            run, {"MANIFEST_APPROVED"}, "Compilation requires an approved figure manifest")
        pir = self.repository.latest_artifact(run["id"], "canonical_model", "approved")
        manifest = self.repository.latest_artifact(run["id"], "figure_manifest", "approved")
        if not pir:
            raise figure_compiler.ApprovalRequired("Approve the canonical model first.")
        if not manifest:
            raise figure_compiler.ApprovalRequired("Approve the figure manifest first.")
        package = figure_compiler.compile_package(
            pir["payload"], manifest["payload"], str(run["ruleset"]))
        self.repository.set_stage(run["id"], "FIGURE_SPECS_COMPILED")
        package_row = self.repository.save_artifact(
            run["id"], "compiled_package", package, principal.user_id)
        self.repository.set_stage(run["id"], "COMPOSED")
        validation = figure_compiler.validate_package(
            pir["payload"], manifest["payload"], package, str(run["ruleset"]))
        self.repository.save_artifact(
            run["id"], "validation_report", validation, principal.user_id,
            parent_artifact_id=package_row["id"])
        run = self.repository.set_stage(
            run["id"], "FINAL_REVIEW" if validation["approved_for_export"] else "VALIDATED")
        return self._state_for_run(run)

    def patch(self, principal: drafting.Principal, project_id: int,
              patch: Mapping[str, Any]) -> dict[str, Any]:
        self._project(principal, project_id)
        run = self._run(project_id)
        self._require_stage(run, {"VALIDATED", "FINAL_REVIEW"}, "Figure editing")
        package_row = self.repository.latest_artifact(run["id"], "compiled_package", "draft")
        pir = self.repository.latest_artifact(run["id"], "canonical_model", "approved")
        manifest = self.repository.latest_artifact(run["id"], "figure_manifest", "approved")
        if not package_row or not pir or not manifest:
            raise drafting.DraftingNotFound("Compile a draft package before editing it.")
        package = figure_compiler.apply_typed_patch(package_row["payload"], patch)
        new_row = self.repository.save_artifact(
            run["id"], "compiled_package", package, principal.user_id,
            parent_artifact_id=package_row["id"])
        self.repository.record_patch(run["id"], new_row["id"], patch, principal.user_id)
        validation = figure_compiler.validate_package(
            pir["payload"], manifest["payload"], package, str(run["ruleset"]))
        self.repository.save_artifact(
            run["id"], "validation_report", validation, principal.user_id,
            parent_artifact_id=new_row["id"])
        run = self.repository.set_stage(
            run["id"], "FINAL_REVIEW" if validation["approved_for_export"] else "VALIDATED")
        return self._state_for_run(run)

    def approve_final(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        self._project(principal, project_id)
        run = self._run(project_id)
        self._require_stage(run, {"FINAL_REVIEW"}, "Final package approval")
        package_row = self.repository.latest_artifact(run["id"], "compiled_package", "draft")
        pir = self.repository.latest_artifact(run["id"], "canonical_model", "approved")
        manifest = self.repository.latest_artifact(run["id"], "figure_manifest", "approved")
        if not package_row or not pir or not manifest:
            raise drafting.DraftingNotFound("Compile and validate a package before final review.")
        # Re-run every deterministic validator against the exact bytes being approved. A report
        # from a previous package version (or a corrupted draft artifact) cannot authorize export.
        validation = figure_compiler.validate_package(
            pir["payload"], manifest["payload"], package_row["payload"], str(run["ruleset"]))
        self.repository.save_artifact(
            run["id"], "validation_report", validation, principal.user_id,
            parent_artifact_id=package_row["id"])
        if not validation.get("approved_for_export"):
            raise figure_compiler.CompilationBlocked(
                "Resolve every hard blocker before final approval.")
        approved = figure_compiler.approve_artifact(
            package_row["payload"], artifact_type="compiled_figure_package",
            user_id=principal.user_id)
        self.repository.save_artifact(
            run["id"], "compiled_package", approved, principal.user_id, state="approved",
            parent_artifact_id=package_row["id"])
        run = self.repository.set_stage(run["id"], "APPROVED")
        return self._state_for_run(run)

    def export(self, principal: drafting.Principal, project_id: int, format_name: str,
               *, sheet: int = 1) -> bytes:
        self._project(principal, project_id)
        run = self._run(project_id)
        self._require_stage(run, {"APPROVED", "EXPORTED"}, "Export")
        package_row = self.repository.latest_artifact(run["id"], "compiled_package", "approved")
        if not package_row:
            raise figure_compiler.ApprovalRequired("Final approval is required before export.")
        package = package_row["payload"]
        if format_name == "pdf":
            output = figure_compiler.render_pdf(package, str(run["ruleset"]))
        elif format_name == "svg":
            sheets = package.get("sheets") or []
            if not 1 <= int(sheet) <= len(sheets):
                raise drafting.DraftingNotFound("Drawing sheet was not found.")
            output = str(sheets[int(sheet) - 1]["svg"]).encode("utf-8")
        else:
            raise figure_compiler.FigureCompilerError("Choose SVG or PDF export.")
        self.repository.set_stage(run["id"], "EXPORTED")
        return output
