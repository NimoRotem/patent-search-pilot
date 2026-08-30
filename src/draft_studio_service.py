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
leased in Postgres anyway - a second process, a second host or a restart mid-turn are all handled
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
import draft_settings
import draft_studio
import draft_terminal
import draft_workspace
import drafting

POLL_SECONDS = max(2.0, float(os.environ.get("DRAFT_TURN_POLL_SECONDS", "5")))
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_DOCUMENTS = 40
MAX_MANUAL_REFERENCES = 60

#  HOW MANY TURNS RUN AT ONCE, AND WHY IT IS NOT ONE.
#  It was one, and one worker draining a queue shared by every project on the host is a queue where
#  a single turn can hold everybody. That is not hypothetical: a drawing repair on one application
#  sat in "drawing and inspecting figures" for three and a half hours while a one-sentence change
#  to another application waited behind it, showing its owner "queued - the drafting agent will
#  pick this up in a moment" the whole time. Postgres remains the authority: claiming is
#  FOR UPDATE SKIP LOCKED and a partial unique index still allows one active turn PER PROJECT, so
#  widening this runs different applications side by side and can never run one of them twice.
#  Five, because three saturated the queue while the box sat at load 0.05 of 8 cores with
#  21 GB free: a drafting turn is almost entirely waiting on a model API, not computing.
#  The ceiling that matters is the provider's rate limit rather than this machine, which is
#  why this stops well short of the hard cap.
DRAFT_TURN_WORKERS = max(1, min(int(os.environ.get("DRAFT_TURN_WORKERS", "5")), 8))
#  A section of an application is long-form prose; this is a ceiling on storage, not on style.
MAX_SECTION_CHARS = 200_000
MAX_AUTOMATIC_FILING_REPAIR_TURNS = max(
    1, min(int(os.environ.get("DRAFT_AUTOMATIC_REPAIR_TURNS", "6")), 6))
_AUTOMATIC_FILING_REPAIR_KEY = re.compile(r"^auto-filing-repair-(\d+)-(\d+)$")
_FILING_GATE_EXHAUSTED = "The automatic filing gate could not clear:"

_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD: threading.Thread | None = None
_THREADS: list[threading.Thread] = []
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
    document itself, so the only thing needed is the text - and the only case that still costs a
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
    """Identify a sheet by its figure number, never by its optional caption."""
    match = re.search(r"\bFIG(?:URE)?S?\.?\s*([0-9]+[A-Za-z]?)\b",
                      str(label or ""), re.IGNORECASE)
    if match:
        return "fig-" + match.group(1).lower()
    return re.sub(r"[^0-9a-z]+", "-", str(label or "").lower()).strip("-")


def _expected_numerals(version: Mapping[str, Any], label: str) -> list[str] | None:
    """The selected figure's labels, joined to the versioned numeral table for prompting."""
    spec = next((item for item in (version or {}).get("figure_specs") or []
                 if _figure_key(item.get("label")) == _figure_key(label)), None)
    if spec is None:
        return None
    table = {str(item.get("numeral") or ""): str(item.get("part") or "")
             for item in (version or {}).get("numerals") or []}
    out = []
    for raw in spec.get("numerals") or []:
        match = re.search(r"\b([A-Za-z]?\d{1,4}[A-Za-z]?)\b", str(raw))
        if not match:
            continue
        numeral = match.group(1)
        fallback_part = re.sub(
            r"^\s*" + re.escape(numeral) + r"\s*(?:=|-)?\s*", "", str(raw)).strip()
        part = table.get(numeral) or fallback_part
        out.append(f"{numeral} = {part}" if part else numeral)
    return out


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
                "Describe the invention in a little more detail - at least a couple of sentences "
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
        #  A NEW AGENT PER DRAFT. The workspace is built, its own CLAUDE.md and private home are
        #  written, a fresh Claude Code session opens in it with no memory of any other project,
        #  and the opening instruction is typed into it. Started in the background because the CLI
        #  takes a few seconds to reach its composer and the person should be looking at their
        #  studio by then, not at a spinner on the intake form.
        threading.Thread(
            target=self._open_first_agent, name=f"draft-open-{project['id']}", daemon=True,
            args=(principal, int(project["id"]), input_kind)).start()
        return project

    def _open_first_agent(self, principal: drafting.Principal, project_id: int,
                          input_kind: str) -> None:
        opening = ("Write the first draft of this application. Everything you need is in input/ "
                   "and prior_art/. Publish it when it is complete."
                   if input_kind == "description" else
                   "Take the draft in input/ and improve it into a filing-quality application. "
                   "Do not discard the user's own text. Publish it when it is complete.")
        try:
            self.send_to_agent(principal, project_id, opening)
        except Exception as exc:                                # noqa: BLE001 - report, never raise
            traceback.print_exc()
            # The project exists and holds everything the user gave us. Losing it because the
            # agent happens to be unconfigured would be the worst possible trade: they would have
            # to re-enter the disclosure and re-upload the art to find out the same thing.
            try:
                self.repository.add_message(
                    project_id, "system",
                    f"The drafting agent could not be started: {str(exc)[:300]} Everything you "
                    "supplied is saved - press Restart above the terminal once that is fixed.")
            except Exception:                                   # noqa: BLE001
                traceback.print_exc()

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
                   kind: str = "revise", idempotency_key: str | None = None,
                   section_key: str = "") -> dict[str, Any]:
        project = self._project(principal, project_id)
        if project.get("status") == "archived":
            raise drafting.DraftingConflict("Restore this project before drafting on it.")
        message = str(message or "").replace("\x00", "").strip()
        if not message:
            raise drafting.DraftingValidationError("Say what you would like changed.")
        if kind not in ("initial", "revise", "question", "qa_fix", "section_edit"):
            kind = "revise"
        section_key = str(section_key or "").strip()
        if kind == "section_edit":
            if section_key not in draft_workspace.SECTION_BY_KEY:
                raise drafting.DraftingValidationError(
                    "That is not a section of this application.")
            if not int(project.get("latest_version_no") or 0):
                raise drafting.DraftingValidationError(
                    "There is no draft yet. Ask for the first draft in the conversation.")
        else:
            section_key = ""
        availability = draft_agent.availability()
        if not availability.get("ok"):
            raise drafting.DraftingConflict(
                f"The drafting agent is not available on this server: {availability['reason']}")
        turn = self.repository.enqueue_turn_safely(
            project_id, principal.user_id, kind=kind, user_message=message,
            project_revision=int(project["revision"]), idempotency_key=idempotency_key,
            section_key=section_key)
        if kind != "initial":
            heading = (draft_workspace.SECTION_BY_KEY[section_key][1]
                       if section_key in draft_workspace.SECTION_BY_KEY else "")
            self.repository.add_message(
                project_id, "user", message,
                payload={"section_key": section_key, "section_heading": heading}
                if heading else None)
        kick()
        return turn

    # -- advanced settings -------------------------------------------------------------------------
    def settings(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        project = self._project(principal, project_id)
        stored = project.get("settings")
        if isinstance(stored, str):
            stored = draft_studio._json(stored, {})
        resolved = dict(stored or {})
        #  The model picker predates this panel and writes its own column, so a project that only
        #  ever used the picker still shows the right model here.
        resolved.setdefault("draft_model", project.get("draft_model") or "")
        return draft_settings.public(resolved)

    def save_settings(self, principal: drafting.Principal, project_id: int,
                      supplied: Mapping[str, Any]) -> dict[str, Any]:
        project = self._project(principal, project_id)
        stored = project.get("settings")
        if isinstance(stored, str):
            stored = draft_studio._json(stored, {})
        try:
            values = draft_settings.clean(supplied, stored)
        except ValueError as exc:
            raise drafting.DraftingValidationError(str(exc)) from exc
        self.repository.save_settings(project_id, values)
        return draft_settings.public(values)

    def set_model(self, principal: drafting.Principal, project_id: int, model: str
                  ) -> dict[str, Any]:
        """Choose which model tier drafts this project, from the next turn onward."""
        self._project(principal, project_id)
        chosen = self.repository.set_draft_model(project_id, model)
        #  Keep the panel and the picker showing the same thing.
        project = self._project(principal, project_id)
        stored = project.get("settings")
        if isinstance(stored, str):
            stored = draft_studio._json(stored, {})
        self.repository.save_settings(
            project_id, {**draft_settings.resolve(stored), "draft_model": chosen})
        return {"draft_model": chosen,
                "label": draft_agent.model_label(chosen) or "the server default"}

    # -- hand editing ----------------------------------------------------------------------------
    def save_section(self, principal: drafting.Principal, project_id: int, *,
                     section_key: str, text: str) -> dict[str, Any]:
        """Store one section exactly as the user typed it.

        The application belongs to the person filing it. The gates that refuse an AGENT's output
        guard against a model inventing something nobody disclosed; they are not a reason to refuse
        a sentence the applicant wrote about their own invention. So this validates only what would
        make the version unfilable whoever wrote it: a citation to a document that is not one of
        this project's sources, and a legal conclusion about patentability. Everything else,
        including a note the user has deliberately left themselves, is saved, and the Review tab is
        what tells them it is still there.
        """
        project = self._project(principal, project_id)
        if project.get("status") == "archived":
            raise drafting.DraftingConflict("Restore this project before editing it.")
        if section_key not in draft_workspace.SECTION_BY_KEY:
            raise drafting.DraftingValidationError("That is not a section of this application.")
        version_no = int(project.get("latest_version_no") or 0)
        if not version_no:
            raise drafting.DraftingValidationError(
                "There is no draft yet. Ask for the first draft in the conversation.")
        if draft_terminal.activity(project_id).get("status") == "busy":
            raise drafting.DraftingConflict(
                "The drafting agent is working on this application, and the version it publishes "
                "would overwrite this. Nothing was saved; your text is still in the box. Press "
                "Stop above the terminal if you want it to stand down.")
        text = str(text or "").replace("\x00", "").strip()
        if len(text) > MAX_SECTION_CHARS:
            raise drafting.DraftingValidationError("That section is too large to store.")
        version = self.drafting_service.repository.get_version(principal, project_id, version_no)
        sections = dict(version.get("sections") or {})
        if str(sections.get(section_key) or "").strip() == text:
            return {"saved": False, "version_no": version_no,
                    "change_note": version.get("change_note") or ""}
        sections[section_key] = str(draft_studio.human_text(text))

        heading = draft_workspace.SECTION_BY_KEY[section_key][1]
        documents = self.repository.documents(project_id)
        allowed = {draft_cite.normalize(key) for key in draft_studio.allowed_reference_keys(
            project.get("references", []), documents)}
        for raw in draft_cite.malformed_citations_in(sections[section_key]):
            raise drafting.DraftingValidationError(
                f"{heading} contains a malformed citation [REF:{raw[:40]}].")
        for citation in draft_cite.citations_in(sections[section_key]):
            canonical = draft_cite.normalize(citation)
            if not canonical or canonical not in allowed:
                raise drafting.DraftingValidationError(
                    f"{heading} cites {canonical or citation}, which is not one of this project's "
                    "sources. Add it under Sources first.")
        for pattern in drafting._LEGAL_CONCLUSION_PATTERNS:
            found = pattern.search(sections[section_key])
            if found:
                raise drafting.DraftingValidationError(
                    f"{heading} states a legal conclusion ({found.group(0)!r}). An application "
                    "describes the invention; it does not conclude on patentability.")

        saved = self.repository.save_manual_version(
            project_id, principal.user_id, sections=sections,
            citations=draft_studio.citations_of(sections), edited_sections=[section_key],
            numerals=version.get("numerals") or [], figures=version.get("figure_specs") or [])
        #  Put the hand edit in the workspace as well. The drafting agent reads draft/ and
        #  publishes what it finds there, so a section edited on the page and not mirrored here is
        #  a section the agent silently reverts the next time it publishes anything at all.
        try:
            name = draft_workspace.SECTION_BY_KEY[section_key][0]
            path = draft_workspace.for_project(int(project_id)) / "draft" / name
            if path.parent.is_dir():
                path.write_text(sections[section_key].rstrip() + "\n", encoding="utf-8")
        except OSError:
            traceback.print_exc()
        return {"saved": True, "version_no": int(saved["version_no"]),
                "continued": bool(saved.get("continued")),
                "change_note": saved.get("change_note") or ""}

    def cancel(self, principal: drafting.Principal, project_id: int, turn_id: int) -> None:
        self._project(principal, project_id)
        self.repository.cancel_turn(project_id, turn_id)

    # -- the drafting agent's terminal ------------------------------------------------------------
    #
    #  Every method here re-checks ownership first, exactly like the rest of the class, because a
    #  drafting terminal is a shell with the draft in it: reading its screen, typing into it and
    #  killing it are all things only this project's owner may do.
    def terminal_state(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        self._project(principal, project_id)
        available = draft_terminal.availability()
        state = draft_terminal.state(project_id)
        return {**state, "available": bool(available.get("ok")),
                "reason": available.get("reason") or "",
                "models": available.get("models") or [],
                "efforts": available.get("efforts") or [],
                "default_model": available.get("default_model") or "",
                "default_effort": available.get("default_effort") or ""}

    def start_terminal(self, principal: drafting.Principal, project_id: int, *,
                       restart: bool = False, fresh: bool = False) -> dict[str, Any]:
        """Start (or restart) this draft's agent, over a workspace rebuilt from the record.

        ``fresh`` also deletes the agent's private home, which is what "a new agent with blank
        memory" means in practice: a new conversation, no transcript of the old one, and nothing
        it learned last week.
        """
        project = self._project(principal, project_id)
        if project.get("status") == "archived":
            raise drafting.DraftingConflict("Restore this project before drafting on it.")
        try:
            built = _runner().build_workspace(project_id)
        except Exception as exc:                                # noqa: BLE001 - report, never 500
            traceback.print_exc()
            raise drafting.DraftingConflict(
                f"The draft workspace could not be built: {str(exc)[:200]}") from exc
        workspace = built["workspace"]
        draft_terminal.sync_figure_images(workspace, project_id, int(project["user_id"]))
        model = draft_terminal.normalize_model(project.get("draft_model"))
        try:
            if fresh:
                return draft_terminal.reset(project_id, workspace, model=model)
            if restart:
                return draft_terminal.restart(project_id, workspace, model=model)
            return draft_terminal.ensure(project_id, workspace, model=model,
                                         effort=draft_terminal.DEFAULT_EFFORT)
        except draft_terminal.TerminalError as exc:
            raise drafting.DraftingConflict(str(exc)) from exc

    def terminal_tail(self, principal: drafting.Principal, project_id: int, *,
                      known_lines: int = 0, last_hash: str = "") -> dict[str, Any]:
        self._project(principal, project_id)
        return draft_terminal.tail(project_id, known_lines=known_lines, last_hash=last_hash)

    def send_to_agent(self, principal: drafting.Principal, project_id: int,
                      message: str) -> dict[str, Any]:
        """Type a message into the drafting agent, starting it first if it is not running."""
        project = self._project(principal, project_id)
        if project.get("status") == "archived":
            raise drafting.DraftingConflict("Restore this project before drafting on it.")
        body = str(message or "").replace("\x00", "").strip()
        if not body:
            raise drafting.DraftingValidationError("Say what you would like changed.")
        if not draft_terminal.exists(project_id):
            self.start_terminal(principal, project_id)
            #  The CLI needs a moment to reach its composer. Typing into the shell that is still
            #  loading it delivers the message to bash, which answers "command not found" and
            #  loses what the person wrote.
            for _ in range(40):
                time.sleep(0.5)
                if draft_terminal.activity(project_id).get("status") in ("idle", "busy") and \
                        "❯" in draft_terminal.capture_recent(project_id, 20):
                    break
        try:
            draft_terminal.send(project_id, body[:draft_studio.MAX_MESSAGE_CHARS])
        except draft_terminal.TerminalError as exc:
            raise drafting.DraftingConflict(str(exc)) from exc
        return {"sent": True}

    def terminal_keys(self, principal: drafting.Principal, project_id: int,
                      keys: Sequence[str]) -> list[str]:
        self._project(principal, project_id)
        try:
            return draft_terminal.send_keys(project_id, keys)
        except draft_terminal.TerminalError as exc:
            raise drafting.DraftingValidationError(str(exc)) from exc

    def interrupt_terminal(self, principal: drafting.Principal, project_id: int) -> bool:
        self._project(principal, project_id)
        return draft_terminal.interrupt(project_id)

    def set_terminal_model(self, principal: drafting.Principal, project_id: int,
                           model: str) -> str:
        self._project(principal, project_id)
        try:
            chosen = draft_terminal.set_model(project_id, model)
        except draft_terminal.TerminalError as exc:
            raise drafting.DraftingConflict(str(exc)) from exc
        #  Remembered on the project so a restarted agent comes back on the model the person
        #  chose, rather than silently dropping to the server default.
        self.repository.set_terminal_model(project_id, chosen)
        return chosen

    def set_terminal_effort(self, principal: drafting.Principal, project_id: int,
                            effort: str) -> str:
        self._project(principal, project_id)
        try:
            return draft_terminal.set_effort(project_id, effort)
        except draft_terminal.TerminalError as exc:
            raise drafting.DraftingConflict(str(exc)) from exc

    def stop_terminal(self, principal: drafting.Principal, project_id: int) -> bool:
        self._project(principal, project_id)
        return draft_terminal.kill(project_id)

    # -- what the agent publishes -----------------------------------------------------------------
    def publish_workspace(self, project_id: int, token: str, *, note: str = "",
                          check: bool = False) -> dict[str, Any]:
        """Store the workspace as a new version, on the agent's own say-so.

        NO principal: this is the agent inside the workspace calling back over loopback, and its
        capability is the per-project token the server wrote into its private home. Ownership is
        not in question - the version is attributed to the project's own user - so what this has
        to get right is the CONTENT: the same validation the automatic turn ran, so a terminal
        agent cannot publish something the queue would have refused.
        """
        loaded = _runner()._load(int(project_id))
        project = loaded["project"]
        workspace = draft_workspace.for_project(int(project_id))
        if not draft_terminal.verify_publish_token(workspace, token):
            raise drafting.DraftingPermissionDenied("That is not this draft's publish token.")
        try:
            snapshot = draft_workspace.snapshot(workspace)
        except drafting.DraftingError as exc:
            return {"ok": False, "error": str(exc), "problems": []}
        allowed = draft_studio.allowed_reference_keys(
            loaded["references"], self.repository.documents(int(project_id)))
        problems = draft_studio.section_problems(snapshot["sections"], allowed)
        if problems:
            return {"ok": False, "checked": bool(check), "problems": problems,
                    "error": "The draft is not publishable yet."}
        if check:
            return {"ok": True, "checked": True, "problems": [],
                    "message": "Every section is present and every citation resolves."}
        version = self.repository.save_manual_version(
            int(project_id), int(project["user_id"]),
            sections=draft_studio.normalize_sections(snapshot["sections"]),
            citations=draft_studio.citations_of(snapshot["sections"]),
            edited_sections=[], numerals=snapshot["numerals"], figures=snapshot["figures"],
            origin="agent", change_note=str(note or "").strip()[:400])
        self.repository.add_message(
            project_id, "agent",
            (str(note or "").strip() or "Published a new version of the draft.")[:2000],
            payload={"version_no": version["version_no"], "source": "terminal"})
        #  The mechanical half of the review, straight away. It costs nothing, it runs the same
        #  way every time, and it means the Review tab is never stale about a version that has
        #  just appeared under it.
        try:
            report = _runner().mechanical_report(
                int(project_id), sections=snapshot["sections"], numerals=snapshot["numerals"],
                figures=snapshot["figures"], allowed=allowed, scope="The drafting agent")
            self.repository.save_qa(int(project_id), turn_id=None,
                                    version_no=int(version["version_no"]), report=report)
        except Exception:                                       # noqa: BLE001 - never fail a publish
            traceback.print_exc()
        return {"ok": True, "version_no": int(version["version_no"]), "problems": []}

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
            f"{canonical} - {record.get('title') or 'untitled'} - added as prior art "
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
        active = next((t for t in turns if t["status"] in ("queued", "running")), None)
        if active and active["status"] == "queued":
            active = dict(active, queue_ahead=self._queue_ahead(active["id"]))
        return {
            "figures": self.figures(project, latest_version),
            "project": project,
            "messages": self.repository.messages(project_id),
            "turns": turns,
            "active_turn": active,
            "qa": qa_reports[0] if qa_reports else None,
            "qa_reports": qa_reports,
            "qa_by_version": qa_by_version,
            "documents": self.repository.documents(project_id),
            "searches": self.repository.searches(project_id),
            "version": latest_version,
            #  The drafting agent IS the terminal now, so what the page needs to know about it is
            #  whether one can run here and whether this draft's own session is up.
            "agent": self.terminal_state(principal, project_id),
        }

    def search_material(self, principal: drafting.Principal, project_id: int) -> dict[str, str]:
        """Build a bounded prior-art query from the current draft without model rewriting."""
        project = self.drafting_service.get_project(principal, project_id, include_versions=True)
        version_no = int(project.get("latest_version_no") or 0)
        version = next((item for item in project.get("versions", [])
                        if int(item.get("version_no") or 0) == version_no), None)
        if not version:
            raise drafting.DraftingValidationError(
                "Wait for the first draft before searching from it.")
        sections = version.get("sections") or {}
        blocks = [str(sections.get("title") or project.get("title") or "").strip(),
                  str(sections.get("summary") or "").strip(),
                  str(sections.get("claims") or "").strip(),
                  str(sections.get("detailed_description") or "").strip()[:9000]]
        query = "\n\n".join(block for block in blocks if block)[:20_000]
        if len(query) < 40:
            raise drafting.DraftingValidationError(
                "The current draft does not contain enough technical detail to search yet.")
        return {"title": (str(sections.get("title") or project.get("title") or
                              "Draft prior-art search")[:240]), "query": query}

    def record_search(self, principal: drafting.Principal, project_id: int, *, slug: str,
                      query: str, status: str) -> dict[str, Any]:
        self._project(principal, project_id)
        row = self.repository.add_search(
            project_id, principal.user_id, slug, query, status=status)
        self.repository.add_message(
            project_id, "system",
            "A prior-art search based on the current draft has started in the background. "
            "It will appear under Sources when results are ready.")
        return row

    def import_search(self, principal: drafting.Principal, project_id: int, slug: str,
                      publication_numbers: Sequence[str]) -> int:
        project = self._project(principal, project_id)
        tracked = self.repository.search(project_id, slug)
        if not tracked:
            raise drafting.DraftingNotFound("That search was not started from this draft.")
        selected = self.drafting_service._selected_report_references(
            principal, slug, project["user_id"], publication_numbers)
        for reference in selected:
            self.repository.add_reference(
                project_id, publication_number=reference["publication_number"],
                title=reference.get("title") or "", source_url=reference.get("source_url"),
                relevance_summary=reference.get("relevance_summary") or "",
                snapshot=reference.get("snapshot") or {}, origin="report",
                report_rank=int(reference.get("report_rank") or 9000))
        self.repository.update_search(
            project_id, slug, status="complete", imported_count=len(selected))
        if selected:
            self.repository.add_message(
                project_id, "system",
                f"{len(selected)} ranked reference(s) from search {slug} were added to the draft. "
                "They are available to the drafting agent from the next message onward.")
        return len(selected)

    def figures(self, project: Mapping[str, Any],
                version: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """The drawings: the SPEC the drafting agent wrote, joined to the sheet the user uploaded.

        Both halves are worth showing on their own. A specification with no sheet is the actionable
        state - it is what the Upload button acts on. A sheet whose specification the agent has
        since removed is the other, and hiding it would be how a drawing quietly stops being part
        of the application without anybody deciding that.

        Nothing here inspects pixels. This product does not draw, so a sheet is what the person
        supplied and the only thing to say about it is that it is present.
        """
        specs = list((version or {}).get("figure_specs") or [])
        try:
            import draft_figures
            uploaded = draft_figures.listing(project["id"], project["user_id"])
        except Exception:                                      # noqa: BLE001 - never break the page
            traceback.print_exc()
            uploaded = []
        by_label = {_figure_key(item.get("figure_label")): item for item in uploaded}
        out = []
        for spec in specs:
            image = by_label.pop(_figure_key(spec.get("label")), None)
            out.append({
                "label": spec.get("label"), "caption": spec.get("caption"),
                "numerals": list(spec.get("numerals") or []),
                "expected_numerals": list(spec.get("numerals") or []),
                "uploaded": bool(image),
                "figure_id": (image or {}).get("id"),
                "active_version": (image or {}).get("active_version"),
                "n_versions": (image or {}).get("n_versions") or 0,
                "versions": [{"version_no": item.get("version_no"),
                              "created_at": str(item.get("created_at") or "")}
                             for item in (image or {}).get("versions") or []]})
        for orphan in by_label.values():
            out.append({
                "label": orphan.get("figure_label"), "caption": orphan.get("caption") or "",
                "numerals": [], "expected_numerals": [],
                "uploaded": True, "figure_id": orphan.get("id"),
                "active_version": orphan.get("active_version"),
                "n_versions": orphan.get("n_versions") or 0,
                "versions": [{"version_no": item.get("version_no"),
                              "created_at": str(item.get("created_at") or "")}
                             for item in orphan.get("versions") or []],
                "orphan": True})
        return out

    def upload_figure(self, principal: drafting.Principal, project_id: int, *, image: bytes,
                      content_type: str, label: str = "", caption: str = "",
                      figure_id: int | None = None) -> dict[str, Any]:
        """Store a finished sheet the user drew themselves.

        The only way a drawing enters this product. Uploading against an existing figure adds a
        version to it rather than a second sheet with the same label, because a redrawn FIG. 3 is
        still FIG. 3 and the History of that sheet is worth keeping.
        """
        import draft_figures
        project = self.drafting_service.get_project(principal, project_id, include_versions=True)
        if not image:
            raise drafting.DraftingValidationError("Choose a drawing file first.")
        try:
            png = draft_figures.normalize_source_image(image, content_type)
        except draft_figures.FigureError as exc:
            raise drafting.DraftingValidationError(str(exc)) from exc

        existing = draft_figures.listing(project_id, project["user_id"])
        version_no = int(project.get("latest_version_no") or 0)
        version = next((item for item in project.get("versions", [])
                        if int(item.get("version_no") or 0) == version_no), {}) or {}
        specs = list(version.get("figure_specs") or [])

        target = None
        if figure_id:
            target = next((item for item in existing
                           if int(item.get("id") or 0) == int(figure_id)), None)
            if not target:
                raise drafting.DraftingNotFound("That drawing is not part of this draft.")
            label = str(target.get("figure_label") or label)
        else:
            label = str(label or "").strip()[:80]
            if label:
                target = next((item for item in existing
                               if _figure_key(item.get("figure_label")) == _figure_key(label)),
                              None)
            else:
                #  No label given: take the first sheet the specification asks for that nobody
                #  has supplied yet, so an upload lands where the draft says it belongs instead
                #  of becoming an orphan the reviewer then complains about.
                supplied = {_figure_key(item.get("figure_label")) for item in existing}
                label = next((str(spec.get("label")) for spec in specs
                              if _figure_key(spec.get("label")) not in supplied), "")
                if not label:
                    number = len(existing) + 1
                    while _figure_key(f"FIG. {number}") in supplied:
                        number += 1
                    label = f"FIG. {number}"
        if target is None and len(existing) >= draft_figures.MAX_FIGURES:
            raise drafting.DraftingValidationError(
                f"A draft can hold at most {draft_figures.MAX_FIGURES} drawings.")

        spec = next((item for item in specs
                     if _figure_key(item.get("label")) == _figure_key(label)), {})
        caption = str(caption or spec.get("caption") or "").strip()[:400]
        if target is None:
            target = draft_figures.create_figure(
                project_id, project["user_id"], label, caption=caption,
                sort_order=len(existing) + 1)
        elif caption:
            draft_figures.update_figure_metadata(
                int(target["id"]), project["user_id"], label, caption=caption,
                sort_order=int(target.get("sort_order") or 0))
        draft_figures.add_version(
            int(target["id"]), prompt="", instruction="Uploaded by the user",
            numerals=list(spec.get("numerals") or []), png=png, mime="image/png",
            source_kind="uploaded")

        #  The agent can open what it has been given. Without this the sheet exists only as rows
        #  in Postgres and the drawing brief it is meant to match is written blind.
        try:
            draft_terminal.sync_figure_images(
                draft_workspace.for_project(int(project_id)), int(project_id),
                int(project["user_id"]))
        except Exception:                                       # noqa: BLE001 - cosmetic only
            traceback.print_exc()
        self.repository.add_message(
            project_id, "system",
            f"A drawing sheet was uploaded for {label}. It is in the workspace as a PNG the "
            "drafting agent can open, and it appears in the Drawings tab.")
        return {"figure_id": int(target["id"]), "label": label, "caption": caption}

    def delete_figure(self, principal: drafting.Principal, project_id: int,
                      figure_id: int) -> None:
        import draft_figures
        project = self._project(principal, project_id)
        figure = draft_figures.get_figure(figure_id, project["user_id"])
        if not figure or int(figure.get("project_id") or 0) != int(project_id):
            raise drafting.DraftingNotFound("That drawing is not part of this draft.")
        if not draft_figures.delete_figure(figure_id, project["user_id"]):
            raise drafting.DraftingNotFound("That drawing was already removed.")
        try:
            draft_terminal.sync_figure_images(
                draft_workspace.for_project(int(project_id)), int(project_id),
                int(project["user_id"]))
        except Exception:                                       # noqa: BLE001 - cosmetic only
            traceback.print_exc()

    def activate_figure_version(self, principal: drafting.Principal, project_id: int,
                                figure_id: int, version_no: int) -> int:
        """Go back to an earlier sheet the user uploaded for this figure."""
        import draft_figures
        project = self._project(principal, project_id)
        figure = draft_figures.get_figure(figure_id, project["user_id"])
        if not figure or int(figure.get("project_id") or 0) != int(project_id):
            raise drafting.DraftingNotFound("That drawing is not part of this draft.")
        if not draft_figures.set_active(figure_id, project["user_id"], int(version_no)):
            raise drafting.DraftingValidationError("That version of the drawing does not exist.")
        try:
            draft_terminal.sync_figure_images(
                draft_workspace.for_project(int(project_id)), int(project_id),
                int(project["user_id"]))
        except Exception:                                       # noqa: BLE001 - cosmetic only
            traceback.print_exc()
        return int(version_no)

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
                      "agent_runs": turn.get("agent_runs") or 0,
                      "spend_usd": float(turn.get("spend_usd") or 0),
                      "model_ms": int(turn.get("model_ms") or 0),
                      "started_at": str(turn.get("started_at") or ""),
                      "tokens_total": sum(int(turn.get(key) or 0) for key in
                                          ("tokens_input", "tokens_output",
                                           "tokens_cache_read", "tokens_cache_write")),
                      "kind": turn.get("kind"), "section_key": turn.get("section_key") or "",
                      "queue_ahead": (self._queue_ahead(turn["id"])
                                      if turn["status"] == "queued" else 0),
                      "version_no": turn.get("version_no")} if turn else None),
            "qa": ({"id": qa["id"], "verdict": qa["verdict"], "counts": qa["counts"],
                    "version_no": qa.get("version_no")} if qa else None),
            "busy": bool(turn and turn["status"] in ("queued", "running")),
            "reviewing": int(project_id) in _REVIEWING,
            #  The drafting agent's own state comes from the terminal, not from the turn queue:
            #  the page needs to know whether it is running so it can show Stop and the pill.
            "agent": draft_terminal.activity(project_id),
        }

    def _queue_ahead(self, turn_id: int) -> int:
        """Never let a counter break the page: an unreadable queue reports as no queue."""
        try:
            return int(self.repository.queue_ahead(turn_id))
        except Exception:                                      # noqa: BLE001
            return 0

    # -- drawings on demand -----------------------------------------------------------------------
    # -- review on demand -------------------------------------------------------------------------
    def rerun_review(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        """Re-review the current version without drafting anything.

        Useful after the user edits a section by hand, and after a citation that was unreachable
        becomes reachable - a report is a point-in-time reading, not a permanent verdict.

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
                     "description alone, and the agent will say so. Add references any time - "
                     "by publication number, by uploading a document, or by running a search - "
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


def process_one(*, stop_event: threading.Event | None = None) -> dict[str, Any] | None:
    """Claim and run one turn. Safe to call from a test or an operator command."""
    if not _RUNNER_FACTORY:
        return None
    runner = _runner()
    if stop_event is not None:
        runner.stop_event = stop_event
    worker_id = f"draft-turn-{os.getpid()}-{threading.get_ident()}"
    claimed = runner.repository.claim_turn(worker_id)
    if not claimed:
        return None
    _stamp(running=True, last_turn_id=claimed["id"], last_result="drafting", last_error=None)
    try:
        outcome = runner.run(claimed)
        _stamp(running=False, last_result="complete", last_error=None)
        return outcome
    except draft_studio.TurnBudgetSpent as exc:
        # Do not retry the same charged turn. The terminal-failure boundary may continue its
        # saved candidate in a fresh bounded turn under the durable repair-chain safety limit.
        return _fail(runner, claimed, str(exc), retryable=False)
    except drafting.DraftingValidationError as exc:
        # The agent produced something we will not store - an empty section, a citation to a
        # document it was not given.  Retryable: the next attempt sees the reason in its request.
        return _fail(runner, claimed, str(exc), retryable=True)
    except drafting.DraftingConflict as exc:
        try:
            runner.restore_figures(int(claimed["id"]))
        except Exception:
            traceback.print_exc()
        _stamp(running=False, last_result="superseded", last_error=str(exc)[:400])
        return None
    except Exception as exc:                                   # noqa: BLE001 - durable boundary
        traceback.print_exc()
        return _fail(runner, claimed, f"{type(exc).__name__}: {str(exc)[:2000]}", retryable=True)


def _fail(runner: draft_studio.TurnRunner, claimed: Mapping[str, Any], error: str, *,
          retryable: bool) -> dict[str, Any] | None:
    error = str(draft_studio.human_text(str(error)))
    preserve_partial_drawings = error.startswith((
        "DrawingBudgetSpent:", "FigureTransientError:")) or "reached its ceiling" in error
    if not preserve_partial_drawings:
        try:
            runner.restore_figures(int(claimed["id"]))
        except Exception:
            traceback.print_exc()
    try:
        result = runner.repository.fail_turn(claimed["id"], claimed["lease_token"], error,
                                             retryable=retryable)
    except drafting.DraftingError:
        _stamp(running=False, last_result="lost", last_error=error[:400])
        return None
    _stamp(running=False, last_result=result.get("status"), last_error=error[:400])
    if result.get("status") == "failed":
        continuation = _continue_terminal_filing_repair(
            runner.repository, claimed, result, error)
        try:
            if continuation == "queued":
                message = (
                    "The candidate did not complete every filing check in that turn. Automatic "
                    "work has continued from the saved candidate in a new turn. No action is "
                    "required.")
            elif continuation == "limit":
                message = (
                    "The candidate still did not pass every filing check after the automatic "
                    "repair safety limit. The candidate and its exact QA findings remain saved, "
                    "and no application version was published.")
            else:
                message = (
                    "The drafting agent could not finish that turn: " + error[:600] +
                    " No application version was published. Try again, or rephrase what you "
                    "asked for.")
            runner.repository.add_message(
                claimed["project_id"], "system", message, turn_id=claimed["id"])
        except Exception:                                      # noqa: BLE001
            pass
    return result


def _continue_terminal_filing_repair(repository: Any, claimed: Mapping[str, Any],
                                     result: Mapping[str, Any], error: str) -> str:
    """Continue a blocked filing gate or a valid candidate interrupted by its provider."""
    filing_gate_stopped = str(error).startswith(_FILING_GATE_EXHAUSTED)
    interrupted_candidate = False
    # A graceful worker restart terminates its drafting or review subprocess with SIGTERM. The
    # shell reports that signal as exit code 143. Treat that like a provider disconnect only when
    # a complete candidate was checkpointed, so a deployment cannot strand filing-ready work.
    # A resumed provider session can disappear while that same graceful stop is propagating. The
    # candidate check below is still mandatory, so this cannot manufacture a continuation from an
    # incomplete drafting response.
    lost_resume_session = "No conversation found with session ID:" in str(error)
    interrupted_run = bool(
        draft_agent._transient_provider_error(error) or
        re.search(r"\bexit code (?:130|143)\b", str(error), re.IGNORECASE) or
        lost_resume_session)
    # The independent reviewer is fail-closed: this exception means no source verdict was
    # accepted, not that the candidate failed source fidelity. Its formatting, timeout, or
    # availability problem can only be repaired by rerunning the mandatory review. No user input
    # can resolve it, so preserve and continue any complete checkpoint automatically.
    source_review_unavailable = str(error).startswith("SourceReviewUnavailable:")
    # FigureTransientError is raised only when a provider or inspection transport failed to
    # return a usable verdict. It is not a visual rejection, and repeating the exact saved
    # candidate is the repair regardless of the provider's particular error wording.
    figure_transient = str(error).startswith("FigureTransientError:")
    # A bounded maintenance caller may stop between sheets. Its exact checkpoint is durable, so
    # continue that candidate without spending a drafting-agent repair on unchanged filing text.
    drawing_budget_spent = str(error).startswith("DrawingBudgetSpent:")
    # The exact charged turn must not retry, but a complete checkpoint may continue in a new
    # bounded turn. The repair-chain sequence prevents unbounded autonomous spend while allowing
    # a difficult application to finish without user intervention.
    turn_budget_spent = "reached its ceiling" in str(error)
    if not filing_gate_stopped and (
            interrupted_run or source_review_unavailable or figure_transient or
            drawing_budget_spent or turn_budget_spent):
        try:
            candidate = repository.retry_candidate(int(result.get("id") or claimed["id"]))
            interrupted_candidate = bool(
                isinstance(candidate, Mapping) and
                isinstance(candidate.get("snapshot"), Mapping) and
                candidate.get("snapshot"))
        except Exception:                                      # noqa: BLE001
            interrupted_candidate = False
    if not filing_gate_stopped and not interrupted_candidate:
        return ""
    current_turn_id = int(result.get("id") or claimed["id"])
    if interrupted_candidate:
        # Provider failures and bounded time or spend slices did not consume a semantic repair
        # attempt. Start a fresh bounded chain from this durable checkpoint so infrastructure
        # timing cannot strand a mechanically repairable application at the filing-repair limit.
        origin_turn_id = current_turn_id
        sequence = 1
    else:
        prior_key = str(result.get("idempotency_key") or claimed.get("idempotency_key") or "")
        matched = _AUTOMATIC_FILING_REPAIR_KEY.fullmatch(prior_key)
        if matched:
            origin_turn_id = int(matched.group(1))
            sequence = int(matched.group(2)) + 1
        else:
            origin_turn_id = current_turn_id
            sequence = 1
    if sequence > MAX_AUTOMATIC_FILING_REPAIR_TURNS:
        return "limit"

    project_id = int(result.get("project_id") or claimed["project_id"])
    user_id = int(result.get("requested_by_user_id") or claimed["requested_by_user_id"])
    revision = int(result.get("project_revision") or claimed["project_revision"])
    try:
        repository.enqueue_turn_safely(
            project_id, user_id,
            kind="gate_resume" if interrupted_candidate else "qa_fix",
            user_message=(
                "Continue automatic filing repair from the saved candidate and its previous QA "
                "report. Resolve every blocker, regenerate any rejected drawing geometry, rerun "
                "all text, source-fidelity, OCR, numeral, leader, and visual checks, and publish "
                "only after every gate passes. This is corrective QA, not new invention "
                "disclosure."),
            project_revision=revision,
            idempotency_key=f"auto-filing-repair-{origin_turn_id}-{sequence}")
        kick()
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        return ""
    return "queued"


def _loop() -> None:
    while not _STOP.is_set():
        try:
            if process_one(stop_event=_STOP) is not None:
                continue
        except Exception as exc:                               # noqa: BLE001 - keep the thread up
            _stamp(running=False, last_result="worker-error", last_error=str(exc)[:400])
            traceback.print_exc()
        _WAKE.wait(POLL_SECONDS)
        _WAKE.clear()


def start_worker() -> threading.Thread:
    """Start the pool, and return its first thread, which is what callers have always waited on."""
    global _THREAD
    with _START_LOCK:
        _THREADS[:] = [thread for thread in _THREADS if thread.is_alive()]
        if _THREAD and _THREAD.is_alive() and len(_THREADS) >= DRAFT_TURN_WORKERS:
            return _THREAD
        _STOP.clear()
        while len(_THREADS) < DRAFT_TURN_WORKERS:
            thread = threading.Thread(
                target=_loop, name=f"draft-turn-worker-{len(_THREADS) + 1}", daemon=True)
            thread.start()
            _THREADS.append(thread)
        _THREAD = _THREADS[0]
        return _THREAD


def stop_worker() -> None:
    _STOP.set()
    _WAKE.set()


def kick() -> None:
    _WAKE.set()


def status() -> dict[str, Any]:
    alive = [thread for thread in _THREADS if thread.is_alive()]
    return {**_STATE, "thread_alive": bool(alive),
            "workers": DRAFT_TURN_WORKERS, "threads_alive": len(alive),
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
    says "drafting…" for ever and the user waits for a process that no longer exists. The lease
    is expired here so the ordinary claim path picks it up on the next poll. The interrupted
    attempt is also returned to the budget because a deployment is not a drafting failure.

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
                    "attempts=greatest(0,attempts-1),"
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
