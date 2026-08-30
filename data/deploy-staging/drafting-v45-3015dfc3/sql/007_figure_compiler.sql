-- Versioned, approval-gated patent figure compiler artifacts.
--
-- Approved rows are immutable at the database boundary. A repair creates the next artifact
-- version and records a typed patch; it never rewrites the sheet a person approved.

CREATE TABLE IF NOT EXISTS app_figure_compiler_runs (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES app_drafting_projects(id) ON DELETE CASCADE,
  owner_user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  draft_version_no integer NOT NULL CHECK (draft_version_no > 0),
  stage text NOT NULL DEFAULT 'INGESTED' CHECK (stage IN (
    'INGESTED','PARSED','DISCLOSURE_EXTRACTED','MODEL_RECONCILED','MODEL_APPROVED',
    'FIGURES_PLANNED','MANIFEST_APPROVED','FIGURE_SPECS_COMPILED','RENDERED','ANNOTATED',
    'COMPOSED','VALIDATED','FINAL_REVIEW','APPROVED','EXPORTED'
  )),
  ruleset text NOT NULL,
  extractor_version text NOT NULL DEFAULT 'pir-1',
  renderer_version text NOT NULL DEFAULT 'semantic-svg-1.0.0',
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS app_figure_compiler_one_active_uq
  ON app_figure_compiler_runs(project_id) WHERE active;
CREATE INDEX IF NOT EXISTS app_figure_compiler_runs_project_idx
  ON app_figure_compiler_runs(project_id, id DESC);

CREATE TABLE IF NOT EXISTS app_figure_compiler_artifacts (
  id bigserial PRIMARY KEY,
  run_id bigint NOT NULL REFERENCES app_figure_compiler_runs(id) ON DELETE CASCADE,
  artifact_type text NOT NULL CHECK (artifact_type IN (
    'ingest_snapshot','patent_intermediate_representation','canonical_model',
    'figure_manifest','compiled_package','validation_report'
  )),
  version_no integer NOT NULL CHECK (version_no > 0),
  state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','approved','superseded')),
  payload jsonb NOT NULL,
  content_sha256 char(64) NOT NULL,
  parent_artifact_id bigint REFERENCES app_figure_compiler_artifacts(id) ON DELETE RESTRICT,
  created_by_user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  approved_at timestamptz,
  UNIQUE(run_id, artifact_type, version_no)
);
CREATE INDEX IF NOT EXISTS app_figure_compiler_artifacts_run_idx
  ON app_figure_compiler_artifacts(run_id, artifact_type, version_no DESC);

CREATE TABLE IF NOT EXISTS app_figure_compiler_patches (
  id bigserial PRIMARY KEY,
  run_id bigint NOT NULL REFERENCES app_figure_compiler_runs(id) ON DELETE CASCADE,
  package_artifact_id bigint NOT NULL REFERENCES app_figure_compiler_artifacts(id) ON DELETE RESTRICT,
  patch jsonb NOT NULL,
  created_by_user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_figure_compiler_patches_run_idx
  ON app_figure_compiler_patches(run_id, id DESC);

CREATE OR REPLACE FUNCTION protect_approved_figure_compiler_artifact()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state = 'approved' THEN
    RAISE EXCEPTION 'approved figure compiler artifacts are immutable';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;

DROP TRIGGER IF EXISTS app_figure_compiler_artifacts_immutable ON app_figure_compiler_artifacts;
CREATE TRIGGER app_figure_compiler_artifacts_immutable
  BEFORE UPDATE OR DELETE ON app_figure_compiler_artifacts
  FOR EACH ROW EXECUTE FUNCTION protect_approved_figure_compiler_artifact();
