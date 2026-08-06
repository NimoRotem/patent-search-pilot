"""What the web routes call, and the worker that drains the drafting queue.

Two boundaries live here and they are deliberately different shapes:

  ``StudioService``  every method takes a ``Principal`` and re-checks ownership through the
                     drafting repository even when the route already did.  Nothing here trusts a
                     request body for anything but content.

  the worker         takes no principal at all.  Possession of a short-lived, single-turn lease
                     token IS its capability, and no account-facing method ever returns one.

The worker runs as a thread inside the web process.  That is right for THIS deployment and worth
saying why: the app runs one gunicorn process with sixteen threads (a second process would double
the reranker's memory on a four-core box), so there is exactly one worker, and the turn queue is
leased in Postgres anyway — a second process, a second host or a restart mid-turn are all handled
by the lease rather than by there happening to be one of us.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from typing import Any, Callable, Mapping, Sequence

import draft_agent
import draft_cite
import draft_studio
import draft_workspace
import drafting

POLL_SECONDS = max(2.0, float(os.environ.get("DRAFT_TURN_POLL_SECONDS", "5")))
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_DOCUMENTS = 40
MAX_MANUAL_REFERENCES = 60

_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD: threading.Thread | None = None
_START_LOCK = threading.Lock()
_RUNNER_FACTORY: Callable[[], draft_studio.TurnRunner] | None = None
_STATE: dict[str, Any] = {"running": False, "last_turn_id": None, "last_result": None,
                          "last_error": None, "updated_at": None}
#  Projects with a review running outside the turn queue. One per project: a second concurrent
#  review would spend twice and produce two reports of the same version that disagree.
_REVIEWING: set[int] = set()


# =============================================================================================
# Document intake
# =============================================================================================
def extract_text(data: bytes, filename: str) -> dict[str, Any]:
    """Pull readable text out of an upload, cheaply where possible.

    Deliberately lighter than the search front door's ``/extract``: that path spends a model pass
    building a search brief because the query depends on it.  Here the agent will read the whole
    document itself, so the only thing needed is the text — and the only case that still costs a
    model call is a scanned facsimile with no text layer, where there is no alternative.
    """
    import ingest_input
    if not data:
        return {"ok": False, "error": "The file is empty."}
    if len(data) > MAX_UPLOAD_BYTES:
        return {"ok": False,
                "error": f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."}
    kind = ingest_input.sniff_kind(data, filename)
    label = ingest_input.safe_label(filename)
    if kind == "bad_pdf":
        return {"ok": False, "error": f"{label} claims to be a PDF but does not start with %PDF-."}
    if kind == "bad_docx":
        return {"ok": False, "error": f"{label} is not a valid .docx file."}
    if kind == "unknown":
        return {"ok": False, "error": f"{label} is not a PDF, .docx or plain text."}

    notes: list[str] = []
    if kind == "pdf":
        read = ingest_input._pdf_read(data)          # column reconstruction; see patent_pdf.py
        text = str(read.get("text") or "")
        notes.extend(read.get("notes") or [])
        if not read.get("text_layer"):
            # A scan.  There is nothing to read without transcribing it, and handing the agent an
            # empty document while calling it prior art would be worse than the model call.
            import patent_doc
            structure = patent_doc.analyze(text="", pdf_bytes=data) or {}
            parts = [structure.get("title") or "", structure.get("abstract") or ""]
            parts += [c.get("text", "") for c in (structure.get("claims") or [])]
            parts += [p if isinstance(p, str) else str(p.get("text", ""))
                      for p in (structure.get("paragraphs") or [])]
            text = "\n\n".join(p for p in parts if p).strip()
            notes.append("No text layer: this document was transcribed from the page images, so "
                         "check it before relying on the wording.")
    elif kind == "docx":
        text = ingest_input._docx_text(data)
    else:
        text = data.decode("utf-8", "ignore")

    text = (text or "").replace("\x00", "").strip()
    if not text:
        return {"ok": False, "error": f"No readable text could be extracted from {label}."}
    return {"ok": True, "text": text, "label": label, "kind": kind, "notes": notes,
            "publication_number": _publication_in(text[:4000]), "title": _title_of(text)}


def _publication_in(head: str) -> str:
    """A publication number printed on the front page of the document, if there is one."""
    for candidate in draft_cite.bare_publication_numbers(head):
        return candidate
    return ""


def _figure_key(label: Any) -> str:
    """"FIG. 1", "Fig 1" and "FIGURE 1" are the same drawing."""
    return re.sub(r"[^0-9a-z]", "", str(label or "").lower()).replace("figure", "fig")


def _title_of(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if 8 <= len(line) <= 200 and not line.lower().startswith(("page ", "united states")):
            return line
    return ""


# =============================================================================================
# Service
# =============================================================================================
class StudioService:
    def __init__(self, drafting_service: drafting.DraftingService,
                 repository: draft_studio.StudioRepository | None = None):
        self.drafting_service = drafting_service
        self.repository = repository or draft_studio.StudioRepository()

    # -- helpers ------------------------------------------------------------------------------
    def _project(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        return self.drafting_service.repository.get_project(principal, project_id)

    # -- creation -----------------------------------------------------------------------------
    def create(self, principal: drafting.Principal, *, title: str, disclosure_text: str,
               input_kind: str = "description", search_slug: str = "",
               publication_numbers: Sequence[str] = (), inventor_notes: str = "",
               applicant: str = "", inventors: str = "",
               uploads: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """Open a drafting project. Prior art is optional in every form it can take."""
        principal.require_active()
        if input_kind not in ("description", "existing_draft"):
            raise drafting.DraftingValidationError("Unknown starting point for the draft.")
        title = str(title or "").strip()[:240]
        disclosure_text = str(disclosure_text or "").replace("\x00", "").strip()
        if len(disclosure_text) < 40:
            raise drafting.DraftingValidationError(
                "Describe the invention in a little more detail — at least a couple of sentences "
                "about what it is and what it does.")
        if not title:
            title = _title_of(disclosure_text)[:240] or "Untitled invention"

        references: list[dict[str, Any]] = []
        if search_slug:
            # The report is the authority for which publications exist and how they were ranked;
            # a publication the user did not get from their own report is not selectable here.
            references = self.drafting_service._selected_report_references(
                principal, search_slug, principal.user_id, list(publication_numbers))

        draft_studio.ensure_schema()
        project = self.drafting_service.repository.create_project_with_references(
            principal, search_slug=search_slug or "", title=title,
            disclosure_text=disclosure_text, inventor_notes=inventor_notes,
            references=references) if references else \
            self.drafting_service.repository.create_project(
                principal, search_slug=search_slug or "", title=title,
                disclosure_text=disclosure_text, inventor_notes=inventor_notes)

        with self.drafting_service.repository._cursor() as cur:
            cur.execute("UPDATE app_drafting_projects SET input_kind=%s,applicant=%s,inventors=%s "
                        "WHERE id=%s", (input_kind, str(applicant or "")[:300],
                                        str(inventors or "")[:2000], project["id"]))
            if references:
                cur.execute("UPDATE app_drafting_references SET origin='report' WHERE project_id=%s",
                            (project["id"],))
        project["input_kind"] = input_kind

        opening = ("I have an existing draft to work from." if input_kind == "existing_draft"
                   else "Here is my invention.")
        self.repository.add_message(project["id"], "user", f"{opening}\n\n{disclosure_text}"[:
                                    draft_studio.MAX_MESSAGE_CHARS])
        for upload in uploads:
            try:
                self._store_upload(project["id"], principal.user_id, upload)
            except drafting.DraftingError:
                continue
        if input_kind == "existing_draft":
            self.repository.add_document(
                project["id"], principal.user_id, kind="source_draft",
                filename="disclosure.txt", body=disclosure_text,
                title=title, note="The draft the user brought.")
        self.repository.add_message(
            project["id"], "system",
            _opening_note(bool(references), search_slug, len(list(uploads))))
        try:
            self.start_turn(principal, project["id"], message=(
                "Write the first draft." if input_kind == "description"
                else "Take my existing draft and improve it into a filing-quality application."),
                kind="initial")
        except drafting.DraftingError as exc:
            # The project exists and holds everything the user gave us. Losing it because the
            # agent happens to be unconfigured would be the worst possible trade: they would have
            # to re-enter the disclosure and re-upload the art to find out the same thing.
            self.repository.add_message(
                project["id"], "system",
                f"The first draft could not be started: {exc} Everything you supplied is saved — "
                "send a message here to try again once that is fixed.")
        return project

    def _store_upload(self, project_id: int, user_id: int,
                      upload: Mapping[str, Any]) -> dict[str, Any]:
        extracted = extract_text(upload.get("data") or b"", str(upload.get("filename") or ""))
        if not extracted.get("ok"):
            raise drafting.DraftingValidationError(extracted.get("error") or "Unreadable upload.")
        kind = str(upload.get("kind") or "prior_art")
        note = str(upload.get("note") or "")
        if extracted.get("notes"):
            note = (note + " " + " ".join(extracted["notes"])).strip()
        return self.repository.add_document(
            project_id, user_id, kind=kind, filename=extracted["label"],
            content_type=str(upload.get("content_type") or ""), body=extracted["text"],
            title=str(upload.get("title") or extracted.get("title") or "")[:400],
            note=note[:2000],
            publication_number=str(upload.get("publication_number") or
                                   extracted.get("publication_number") or "") or None)

    # -- conversation --------------------------------------------------------------------------
    def start_turn(self, principal: drafting.Principal, project_id: int, *, message: str,
                   kind: str = "revise", idempotency_key: str | None = None) -> dict[str, Any]:
        project = self._project(principal, project_id)
        if project.get("status") == "archived":
            raise drafting.DraftingConflict("Restore this project before drafting on it.")
        message = str(message or "").replace("\x00", "").strip()
        if not message:
            raise drafting.DraftingValidationError("Say what you would like changed.")
        if kind not in ("initial", "revise", "question", "qa_fix"):
            kind = "revise"
        availability = draft_agent.availability()
        if not availability.get("ok"):
            raise drafting.DraftingConflict(
                f"The drafting agent is not available on this server: {availability['reason']}")
        if kind != "initial":
            self.repository.add_message(project_id, "user", message)
        turn = self.repository.enqueue_turn_safely(
            project_id, principal.user_id, kind=kind, user_message=message,
            project_revision=int(project["revision"]), idempotency_key=idempotency_key)
        kick()
        return turn

    def cancel(self, principal: drafting.Principal, project_id: int, turn_id: int) -> None:
        self._project(principal, project_id)
        self.repository.cancel_turn(project_id, turn_id)

    # -- sources ---------------------------------------------------------------------------------
    def add_uploads(self, principal: drafting.Principal, project_id: int,
                    uploads: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        self._project(principal, project_id)
        existing = self.repository.documents(project_id)
        if len(existing) + len(uploads) > MAX_DOCUMENTS:
            raise drafting.DraftingValidationError(
                f"A project can hold at most {MAX_DOCUMENTS} uploaded documents.")
        stored = []
        for upload in uploads:
            stored.append(self._store_upload(project_id, principal.user_id, upload))
        if stored:
            names = ", ".join(d["filename"] for d in stored)
            self.repository.add_message(
                project_id, "system",
                f"{len(stored)} document(s) added to this project's sources: {names}. They are "
                "available to the drafting agent from the next message onward.")
        return stored

    def add_reference(self, principal: drafting.Principal, project_id: int,
                      publication: str) -> dict[str, Any]:
        """Add prior art by publication number, resolved against the corpus before it is stored."""
        self._project(principal, project_id)
        canonical = draft_cite.normalize(publication)
        if not canonical:
            raise drafting.DraftingValidationError(
                f"{publication!r} is not a publication number we can read. Try a form like "
                "US-9108319-B2 or EP 3 707 092 B1.")
        record = draft_cite.resolve(canonical, with_text=True)
        if not record.get("found"):
            raise drafting.DraftingValidationError(
                f"{canonical} could not be found in the corpus or any reachable source "
                f"({record.get('reason')}). Upload the document instead and it will be used "
                "exactly as supplied.")
        with self.drafting_service.repository._cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM app_drafting_references WHERE project_id=%s",
                        (int(project_id),))
            if int(cur.fetchone()["n"]) >= MAX_MANUAL_REFERENCES:
                raise drafting.DraftingValidationError(
                    f"A project can hold at most {MAX_MANUAL_REFERENCES} references.")
        self.repository.add_reference(
            project_id, publication_number=canonical, title=record.get("title") or "",
            source_url=record.get("url") or None,
            relevance_summary="Added by the user; not ranked by a prior-art search.",
            snapshot={"publication_number": canonical, "title": record.get("title") or "",
                      "abstract": record.get("abstract") or "",
                      "claims": record.get("claims") or "",
                      "description": (record.get("description") or "")[:60_000],
                      "publication_date": record.get("publication_date") or "",
                      "filing_date": record.get("filing_date") or "",
                      "priority_date": record.get("priority_date") or "",
                      "assignee": record.get("assignee") or "",
                      "source_url": record.get("url") or ""},
            origin="manual")
        self.repository.add_message(
            project_id, "system",
            f"{canonical} — {record.get('title') or 'untitled'} — added as prior art "
            f"(found in {record.get('source')}). It reaches the drafting agent with your next "
            "message; a turn already running was given the sources it started with.")
        return record

    def remove_reference(self, principal: drafting.Principal, project_id: int,
                         publication: str) -> None:
        self._project(principal, project_id)
        self.repository.remove_reference(project_id, draft_cite.normalize(publication) or publication)

    def remove_document(self, principal: drafting.Principal, project_id: int,
                        document_id: int) -> None:
        self._project(principal, project_id)
        self.repository.delete_document(project_id, document_id)

    # -- reading -----------------------------------------------------------------------------------
    def state(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        """Everything the studio page renders, in one read."""
        project = self.drafting_service.get_project(principal, project_id, include_versions=True)
        turns = self.repository.turns(project_id)
        qa_reports = self.repository.qa_reports(project_id)
        latest_version = next((v for v in project.get("versions", [])
                               if int(v["version_no"]) == int(project["latest_version_no"] or 0)),
                              None)
        qa_by_version = {}
        for report in qa_reports:
            if report.get("version_no") and report["version_no"] not in qa_by_version:
                qa_by_version[report["version_no"]] = report
        return {
            "figures": self.figures(project, latest_version),
            "project": project,
            "messages": self.repository.messages(project_id),
            "turns": turns,
            "active_turn": next((t for t in turns if t["status"] in ("queued", "running")), None),
            "qa": qa_reports[0] if qa_reports else None,
            "qa_reports": qa_reports,
            "qa_by_version": qa_by_version,
            "documents": self.repository.documents(project_id),
            "version": latest_version,
            "agent": draft_agent.availability(),
        }

    def figures(self, project: Mapping[str, Any],
                version: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """The drawings, as the SPEC the agent wrote joined to the image (if one was drawn yet).

        Two halves of the same thing kept in one list on purpose: a figure specification with no
        drawing is the actionable state — it is what the Draw button acts on — and a drawing whose
        specification the agent has since removed is the other, which is worth seeing rather than
        silently hiding.
        """
        specs = list((version or {}).get("figure_specs") or [])
        try:
            import draft_figures
            drawn = draft_figures.listing(project["id"], project["user_id"])
        except Exception:                                      # noqa: BLE001 - never break the page
            traceback.print_exc()
            drawn = []
        by_label = {_figure_key(d.get("figure_label")): d for d in drawn}
        out = []
        for spec in specs:
            image = by_label.pop(_figure_key(spec.get("label")), None)
            out.append({"label": spec.get("label"), "caption": spec.get("caption"),
                        "numerals": spec.get("numerals") or [], "drawn": bool(image),
                        "figure_id": (image or {}).get("id"),
                        "active_version": (image or {}).get("active_version"),
                        "n_versions": (image or {}).get("n_versions") or 0,
                        "versions": (image or {}).get("versions") or []})
        for orphan in by_label.values():
            out.append({"label": orphan.get("figure_label"), "caption": orphan.get("caption"),
                        "numerals": [], "drawn": True, "figure_id": orphan.get("id"),
                        "active_version": orphan.get("active_version"),
                        "n_versions": orphan.get("n_versions") or 0,
                        "versions": orphan.get("versions") or [], "orphan": True})
        return out

    def draw_figure(self, principal: drafting.Principal, project_id: int, *, label: str,
                    caption: str, instruction: str = "",
                    figure_id: int | None = None) -> dict[str, Any]:
        """Draw (or redraw) one figure from the draft's own description and numerals."""
        import draft_figures
        project = self.drafting_service.get_project(principal, project_id, include_versions=True)
        version_no = int(project.get("latest_version_no") or 0)
        sections = next((v.get("sections") for v in project.get("versions", [])
                         if int(v.get("version_no") or 0) == version_no), {}) or {}
        try:
            return draft_figures.render_figure(
                project_id, project["user_id"], label=str(label or "")[:80],
                caption=str(caption or "")[:400], sections=sections,
                instruction=str(instruction or "")[:1000], figure_id=figure_id,
                disclosure=str(project.get("disclosure_text") or "")[:4000])
        except draft_figures.FigureError as exc:
            raise drafting.DraftingValidationError(str(exc)) from exc

    def poll(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        """The small, cheap read the page polls while a turn is in flight.

        Called every three seconds per open tab, so it counts messages rather than reading them:
        the full state is fetched only once something has actually changed.
        """
        project = self.drafting_service.repository.get_project(principal, project_id)
        turn = self.repository.latest_turn(project_id)
        qa = self.repository.latest_qa(project_id)
        cursor = self.repository.message_cursor(project_id)
        return {
            "status": project["status"],
            "latest_version_no": project["latest_version_no"],
            "message_count": cursor["count"],
            "last_message_id": cursor["last_id"],
            "turn": ({"id": turn["id"], "turn_no": turn["turn_no"], "status": turn["status"],
                      "stage": turn["stage"], "last_error": turn.get("last_error"),
                      "version_no": turn.get("version_no")} if turn else None),
            "qa": ({"id": qa["id"], "verdict": qa["verdict"], "counts": qa["counts"],
                    "version_no": qa.get("version_no")} if qa else None),
            "busy": bool(turn and turn["status"] in ("queued", "running")),
            "reviewing": int(project_id) in _REVIEWING,
        }

    # -- review on demand -------------------------------------------------------------------------
    def rerun_review(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        """Re-review the current version without drafting anything.

        Useful after the user edits a section by hand, and after a citation that was unreachable
        becomes reachable — a report is a point-in-time reading, not a permanent verdict.

        Runs in the BACKGROUND. A review is minutes of model time; holding a request thread open
        for it would be a request the browser or a proxy eventually gives up on, and the page
        already knows how to notice a new report arriving.
        """
        project = self._project(principal, project_id)
        version_no = int(project.get("latest_version_no") or 0)
        if not version_no:
            raise drafting.DraftingValidationError("There is no draft to review yet.")
        if int(project_id) in _REVIEWING:
            raise drafting.DraftingConflict("A review of this draft is already running.")
        version = self.drafting_service.repository.get_version(principal, project_id, version_no)
        _REVIEWING.add(int(project_id))
        threading.Thread(target=self._review_now, args=(int(project_id), version),
                         name=f"draft-review-{project_id}", daemon=True).start()
        return {"queued": True, "version_no": version_no}

    def _review_now(self, project_id: int, version: Mapping[str, Any]) -> None:
        try:
            runner = _runner()
            context = runner.prepare({"project_id": project_id, "user_message": "",
                                      "turn_no": 0, "kind": "revise"})
            workspace = context["workspace"]
            draft_workspace.write_sections(workspace, version["sections"])
            snapshot = draft_workspace.snapshot(workspace)
            allowed = [r["publication_number"] for r in context["references"]]
            runner.review(project_id, turn_id=None, version_no=int(version["version_no"]),
                          workspace=workspace, allowed=allowed, sections=snapshot["sections"],
                          numerals=snapshot["numerals"], figures=snapshot["figures"])
        except Exception:                                       # noqa: BLE001 - never kill the thread
            traceback.print_exc()
        finally:
            _REVIEWING.discard(int(project_id))


def _opening_note(has_report_art: bool, slug: str, uploads: int) -> str:
    parts = []
    if has_report_art:
        parts.append(f"Prior art selected from search {slug} is attached to this project.")
    if uploads:
        parts.append(f"{uploads} uploaded document(s) are attached.")
    if not parts:
        parts.append("No prior art is attached yet. The draft will be written from your "
                     "description alone, and the agent will say so. Add references any time — "
                     "by publication number, by uploading a document, or by running a search — "
                     "and ask for a revision.")
    parts.append("Drafting now. This takes a few minutes; you can leave the page.")
    return " ".join(parts)


# =============================================================================================
# Worker
# =============================================================================================
def configure(factory: Callable[[], draft_studio.TurnRunner]) -> None:
    global _RUNNER_FACTORY
    _RUNNER_FACTORY = factory


def _runner() -> draft_studio.TurnRunner:
    if not _RUNNER_FACTORY:
        raise drafting.DraftingConflict("The drafting worker is not configured on this server.")
    return _RUNNER_FACTORY()


def _stamp(**values: Any) -> None:
    _STATE.update(values, updated_at=time.time())


def process_one() -> dict[str, Any] | None:
    """Claim and run one turn. Safe to call from a test or an operator command."""
    if not _RUNNER_FACTORY:
        return None
    runner = _runner()
    worker_id = f"draft-turn-{os.getpid()}-{threading.get_ident()}"
    claimed = runner.repository.claim_turn(worker_id)
    if not claimed:
        return None
    _stamp(running=True, last_turn_id=claimed["id"], last_result="drafting", last_error=None)
    try:
        outcome = runner.run(claimed)
        _stamp(running=False, last_result="complete", last_error=None)
        return outcome
    except drafting.DraftingValidationError as exc:
        # The agent produced something we will not store — an empty section, a citation to a
        # document it was not given.  Retryable: the next attempt sees the reason in its request.
        return _fail(runner, claimed, str(exc), retryable=True)
    except drafting.DraftingConflict as exc:
        _stamp(running=False, last_result="superseded", last_error=str(exc)[:400])
        return None
    except Exception as exc:                                   # noqa: BLE001 - durable boundary
        traceback.print_exc()
        return _fail(runner, claimed, f"{type(exc).__name__}: {str(exc)[:2000]}", retryable=True)


def _fail(runner: draft_studio.TurnRunner, claimed: Mapping[str, Any], error: str, *,
          retryable: bool) -> dict[str, Any] | None:
    try:
        result = runner.repository.fail_turn(claimed["id"], claimed["lease_token"], error,
                                             retryable=retryable)
    except drafting.DraftingError:
        _stamp(running=False, last_result="lost", last_error=error[:400])
        return None
    _stamp(running=False, last_result=result.get("status"), last_error=error[:400])
    if result.get("status") == "failed":
        try:
            runner.repository.add_message(
                claimed["project_id"], "system",
                "The drafting agent could not finish that turn: " + error[:600] +
                " Nothing in your draft was changed. Try again, or rephrase what you asked for.",
                turn_id=claimed["id"])
        except Exception:                                      # noqa: BLE001
            pass
    return result


def _loop() -> None:
    while not _STOP.is_set():
        try:
            if process_one() is not None:
                continue
        except Exception as exc:                               # noqa: BLE001 - keep the thread up
            _stamp(running=False, last_result="worker-error", last_error=str(exc)[:400])
            traceback.print_exc()
        _WAKE.wait(POLL_SECONDS)
        _WAKE.clear()


def start_worker() -> threading.Thread:
    global _THREAD
    with _START_LOCK:
        if _THREAD and _THREAD.is_alive():
            return _THREAD
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="draft-turn-worker", daemon=True)
        _THREAD.start()
        return _THREAD


def stop_worker() -> None:
    _STOP.set()
    _WAKE.set()


def kick() -> None:
    _WAKE.set()


def status() -> dict[str, Any]:
    return {**_STATE, "thread_alive": bool(_THREAD and _THREAD.is_alive()),
            "configured": _RUNNER_FACTORY is not None, "agent": draft_agent.availability()}


def _worker_is_alive(claimed_by: Any) -> bool:
    """Is the process that claimed this turn still running on THIS host?

    ``claimed_by`` is ``draft-turn-<pid>-<thread>``.  A live pid means the run is still in flight
    and its lease must be left alone.
    """
    match = re.match(r"^draft-turn-(\d+)-", str(claimed_by or ""))
    if not match:
        return False
    try:
        os.kill(int(match.group(1)), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True                      # exists, owned by someone else: assume alive


def recover_interrupted_turns() -> int:
    """At startup, hand back turns whose worker died mid-run.

    A turn's completion is recorded by the process that ran it.  A deploy or an OOM halfway
    through leaves the row saying `running` with a lease nobody holds; without this the project
    says "drafting…" for ever and the user waits for a process that no longer exists.  The lease
    is simply expired here so the ordinary claim path picks it up on the next poll — which also
    means a turn that has already exhausted its attempts fails honestly instead of looping.

    A turn whose claiming PROCESS IS STILL ALIVE is left alone.  Expiring its lease would let a
    second worker claim a turn that is genuinely still running, and the cost of that is not a
    duplicate row: it is a second fifteen-minute model run against the same workspace, with two
    agents editing the same files.
    """
    released = 0
    try:
        import db
        with db.cursor() as cur:
            cur.execute("SELECT id,claimed_by FROM app_draft_turns WHERE status='running' "
                        "AND (lease_expires_at IS NULL OR lease_expires_at>now()) FOR UPDATE")
            stale = [row["id"] for row in cur.fetchall()
                     if not _worker_is_alive(row.get("claimed_by"))]
            if stale:
                cur.execute(
                    "UPDATE app_draft_turns SET lease_expires_at=now()-interval '1 second',"
                    "stage='resuming after a restart',updated_at=now() WHERE id = ANY(%s)",
                    (stale,))
                released = len(stale)
    except Exception:                                          # noqa: BLE001 - never block boot
        traceback.print_exc()
        return 0
    if released:
        print(f"[recovery] {released} drafting turn(s) released for another attempt", flush=True)
        kick()
    return released


def init_app(app, runner_factory: Callable[[], draft_studio.TurnRunner]):
    configure(runner_factory)
    if ("pytest" not in sys.modules and not app.config.get("TESTING")
            and os.environ.get("DRAFT_TURN_WORKER", "1").lower() not in ("0", "false", "no")):
        start_worker()
    return app
