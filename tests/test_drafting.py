"""Hermetic contracts for account-owned, citation-aware drafting."""
import re
from contextlib import contextmanager
from pathlib import Path

import pytest

import drafting

DISCLOSURE = (
    "A handheld vacuum lifting tool has a battery-powered pump, a detachable base plate, "
    "and an RFID reader in the handle. Each base plate stores an identifier that selects a "
    "corresponding safe-load display profile when the plate is mechanically attached."
)
USER = drafting.Principal(41)
ADMIN = drafting.Principal(7, is_admin=True)


def report_cards():
    return [
        {
            "pub": "US-20240123456-A1", "rank": 2, "title": "Interchangeable lifting plate",
            "abstract": "A lifting head accepts interchangeable contact plates.",
            "why_relevant": "It describes a detachable plate but not RFID configuration.",
            "evidence": [{"text": "The contact plate is releasably coupled to a lifting head."}],
            "url": "https://patents.google.com/patent/US20240123456A1/en",
        },
        {
            "pub": "US-11223344-B2", "rank": 1, "title": "RFID tool accessory",
            "abstract": "A reader identifies an accessory and changes a tool setting.",
            "rationale": "It grounds accessory identification and automatic settings.",
            "claims": [{"text": "a controller selecting an operating setting from an identifier"}],
        },
    ]


def generated_sections(citation="US-11223344-B2"):
    return {
        "title": "Configurable Vacuum Lifting Tool",
        "cross_reference": "Not applicable.",
        "field": "The disclosure relates to portable vacuum lifting tools.",
        "background": f"Some tools identify interchangeable accessories [REF:{citation}].",
        "summary": "A handle reads an identifier associated with an attached base plate.",
        "drawing_descriptions": "FIG. 1 is a side elevation of the lifting tool.",
        "detailed_description": "FIG. 1 shows the disclosed handle and battery-powered pump.",
        "claims": "1. A lifting apparatus comprising a handle, a pump, and an RFID reader.",
        "abstract": "A vacuum lifting tool identifies a detachable base plate and selects a profile.",
    }


def test_prompt_is_deterministic_ranked_and_covers_every_required_section():
    project = {"title": "RFID plate lifter", "disclosure_text": DISCLOSURE,
               "inventor_notes": "The plate is mechanically keyed."}
    refs = [drafting.normalize_report_reference(card, i + 1)
            for i, card in enumerate(report_cards())]
    first = drafting.assemble_us_application_prompt(project, refs, "Prefer apparatus claims.")
    second = drafting.assemble_us_application_prompt(dict(reversed(list(project.items()))),
                                                      list(reversed(refs)),
                                                      "Prefer apparatus claims.")

    assert first == second
    assert len(first.sha256) == 64
    assert first.allowed_references == ("US-11223344-B2", "US-20240123456-A1")
    assert first.user_prompt.index("US-11223344-B2") < first.user_prompt.index("US-20240123456-A1")
    for key, heading in drafting.SECTION_ORDER:
        assert key in first.user_prompt
        assert heading in first.user_prompt


def test_prompt_contains_citation_and_non_invention_guardrails():
    project = {"title": "RFID plate lifter", "disclosure_text": DISCLOSURE,
               "inventor_notes": ""}
    reference = drafting.normalize_report_reference(report_cards()[0])
    bundle = drafting.assemble_us_application_prompt(project, [reference])

    guardrails = re.sub(r"\s+", " ", bundle.system_prompt.lower())
    assert "only authority for what the invention includes" in guardrails
    assert "never use them to fill a disclosure gap" in guardrails
    assert "no placeholder" in guardrails
    assert "[drafting note:" not in guardrails
    assert "patentability, novelty, non-obviousness" in guardrails
    assert "citation tokens belong only in background" in guardrails
    assert "every affirmative limitation in a claim" in guardrails


def test_report_reference_snapshot_is_bounded_and_rejects_unsafe_url():
    card = report_cards()[0] | {
        "url": "javascript:alert(1)",
        "claims": [{"text": "x" * (drafting.MAX_REFERENCE_CONTEXT_CHARS + 500)}],
    }
    reference = drafting.normalize_report_reference(card)
    assert reference["source_url"] is None
    assert len(reference["snapshot"]["prompt_context"]) <= drafting.MAX_REFERENCE_CONTEXT_CHARS
    assert set(reference["snapshot"]) == {
        "publication_number", "report_rank", "title", "source_url",
        "relevance_summary", "prompt_context",
    }


def test_generated_sections_validate_selected_citations_and_render_in_order():
    sections = drafting.normalize_generated_sections(
        generated_sections(), ["US-11223344-B2", "US-20240123456-A1"])
    assert drafting.extract_citations(sections) == ["US-11223344-B2"]
    markdown = drafting.render_application_markdown(sections)
    headings = ["# Title"] + [f"## {heading}" for _, heading in drafting.SECTION_ORDER[1:]]
    offsets = [markdown.index(heading) for heading in headings]
    assert offsets == sorted(offsets)
    assert markdown.endswith("\n")


@pytest.mark.parametrize("mutation,message", [
    ({"background": "A cited system [REF:US-99999999-B2]."}, "unselected reference"),
    ({"claims": "1. A tool according to [REF:US-11223344-B2]."}, "only in Background"),
    ({"background": "No citations are used here."}, "must ground"),
    ({"summary": "The disclosed system is patentable."}, "legal conclusion"),
    ({"cross_reference": "[DRAFTING NOTE: confirm priority.]"}, "unfinished placeholder"),
    ({"summary": "Part names are for the draftsperson only."}, "unfinished placeholder"),
    ({"abstract": ""}, "missing the abstract"),
])
def test_generated_sections_reject_ungrounded_or_legal_output(mutation, message):
    payload = generated_sections()
    payload.update(mutation)
    with pytest.raises(drafting.DraftingValidationError, match=message):
        drafting.normalize_generated_sections(payload, ["US-11223344-B2"])


class FakeRepository:
    def __init__(self):
        self.project = {
            "id": 9, "user_id": USER.user_id, "search_slug": "adhoc-secure",
            "title": "RFID plate lifter", "disclosure_text": DISCLOSURE,
            "inventor_notes": "", "revision": 3, "status": "active",
        }
        self.references = []
        self.replace_call = None
        self.create_atomic_call = None
        self.enqueue_call = None
        self.jobs = {
            18: {"id": 18, "project_id": 9, "status": "failed", "max_attempts": 3,
                 "request_instructions": "Use apparatus claims."}
        }

    def get_project(self, principal, project_id):
        assert project_id == self.project["id"]
        assert principal.user_id in {USER.user_id, ADMIN.user_id}
        return dict(self.project)

    def list_references(self, principal, project_id):
        self.get_project(principal, project_id)
        return [dict(reference) for reference in self.references]

    def replace_references(self, principal, project_id, references, *, expected_revision=None):
        self.get_project(principal, project_id)
        self.references = [dict(reference) for reference in references]
        self.replace_call = (principal, project_id, self.references, expected_revision)
        return self.project | {"revision": 4, "reference_count": len(references)}

    def create_project_with_references(self, principal, **kwargs):
        self.create_atomic_call = (principal, kwargs)
        return self.project | {"reference_count": len(kwargs["references"])}

    def enqueue_job(self, principal, project_id, bundle, **kwargs):
        self.enqueue_call = (principal, project_id, bundle, kwargs)
        return {
            "id": 22, "project_id": project_id, "status": "queued",
            "system_prompt": bundle.system_prompt, "user_prompt": bundle.user_prompt,
            "lease_token_hash": "must-not-leak", "allowed_references": bundle.allowed_references,
        }

    def get_job(self, principal, job_id):
        return dict(self.jobs[job_id])


def test_service_selects_only_from_trusted_ranked_report_and_queues_safe_payload():
    repository = FakeRepository()
    loader_calls = []

    def loader(principal, slug, owner_user_id):
        loader_calls.append((principal, slug, owner_user_id))
        return {"cards": report_cards()}

    service = drafting.DraftingService(repository, loader)
    selected = service.select_references(
        USER, 9, ["US-11223344-B2", "US-20240123456-A1"], expected_revision=3)
    assert selected["reference_count"] == 2
    assert loader_calls == [(USER, "adhoc-secure", USER.user_id)]
    assert repository.references[0]["publication_number"] == "US-11223344-B2"

    job = service.queue_generation(USER, 9, instructions="Use apparatus claims.",
                                   idempotency_key="draft-click-1")
    assert job["status"] == "queued"
    assert "system_prompt" not in job and "user_prompt" not in job
    assert "lease_token_hash" not in job
    _, _, bundle, options = repository.enqueue_call
    assert bundle.allowed_references == ("US-11223344-B2", "US-20240123456-A1")
    assert options["idempotency_key"] == "draft-click-1"


def test_service_rejects_reference_not_in_authoritative_report():
    service = drafting.DraftingService(FakeRepository(),
                                       lambda principal, slug, owner: report_cards())
    with pytest.raises(drafting.DraftingPermissionDenied, match="not in"):
        service.select_references(USER, 9, ["US-00000001-A1"])


def test_service_validates_sources_before_atomic_project_creation():
    repository = FakeRepository()
    service = drafting.DraftingService(repository,
                                       lambda principal, slug, owner: report_cards())
    project = service.create_project_with_references(
        USER, search_slug="adhoc-secure", title="RFID lifter",
        disclosure_text=DISCLOSURE, publication_numbers=["US-11223344-B2"])
    assert project["reference_count"] == 1
    _principal, call = repository.create_atomic_call
    assert call["references"][0]["publication_number"] == "US-11223344-B2"


def test_service_retry_uses_current_inputs_and_links_prior_job():
    repository = FakeRepository()
    repository.references = [drafting.normalize_report_reference(report_cards()[0])]
    service = drafting.DraftingService(repository,
                                       lambda principal, slug, owner: report_cards())
    retried = service.retry_generation(USER, 18, idempotency_key="retry-18")
    assert retried["status"] == "queued"
    _, _, _, options = repository.enqueue_call
    assert options["retry_of_job_id"] == 18
    assert options["instructions"] == "Use apparatus claims."


class ScriptedCursor:
    def __init__(self, project):
        self.project = project
        self.current = None
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if sql.startswith("SELECT * FROM app_drafting_projects"):
            self.current = dict(self.project) if self.project else None
        elif "AS reference_count" in sql:
            self.current = {"reference_count": 0}
        else:  # pragma: no cover - guards accidental repository query expansion
            raise AssertionError(sql)

    def fetchone(self):
        return self.current


def repository_with_project(project):
    cursor = ScriptedCursor(project)

    @contextmanager
    def factory(**kwargs):
        yield cursor

    return drafting.DraftingRepository(factory, migrate=False), cursor


def test_repository_enforces_owner_admin_and_inactive_boundaries():
    project = {"id": 9, "user_id": 41, "status": "active", "latest_version_no": 0}
    repository, _ = repository_with_project(project)
    assert repository.get_project(USER, 9)["id"] == 9
    assert repository.get_project(ADMIN, 9)["id"] == 9

    with pytest.raises(drafting.DraftingNotFound):
        repository.get_project(drafting.Principal(99), 9)
    with pytest.raises(drafting.DraftingPermissionDenied):
        repository.get_project(drafting.Principal(41, is_active=False), 9)


def test_worker_lease_uses_digest_and_constant_time_comparison_contract():
    token = "opaque-worker-capability"
    row = {"status": "running",
           "lease_token_hash": drafting.hashlib.sha256(token.encode()).hexdigest()}
    drafting.DraftingRepository._verify_lease(row, token)
    with pytest.raises(drafting.DraftingConflict, match="no longer owns"):
        drafting.DraftingRepository._verify_lease(row, "wrong-token")


def test_sql_migration_has_ownership_versions_queue_and_stale_job_controls():
    migration = (Path(__file__).parents[1] / "sql" / "004_drafting.sql").read_text()
    assert "app_drafting_projects" in migration
    assert "user_id bigint NOT NULL REFERENCES app_users" in migration
    assert "app_drafting_references" in migration
    assert "app_draft_versions" in migration and "UNIQUE (project_id, version_no)" in migration
    assert "app_drafting_jobs_one_active_uq" in migration
    assert "project_revision" in migration
    assert "lease_token_hash" in migration and "lease_expires_at" in migration
    assert "superseded" in migration and "retry_of_job_id" in migration
    assert "notification_status" in migration
    source = (Path(__file__).parents[1] / "src" / "drafting.py").read_text()
    assert "status IN ('queued','running') AND attempts>=max_attempts" in source
