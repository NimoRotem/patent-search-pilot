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
import draft_qa
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

TERMINAL_NOT_FOR_THIS_ACCOUNT = (
    "A drafting agent is not enabled for this account. Everything else in the studio works: the "
    "draft, the review, the sources, the drawings and hand editing.")

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
        if self._may_use_terminal(principal):
            threading.Thread(
                target=self._open_first_agent, name=f"draft-open-{project['id']}", daemon=True,
                args=(principal, int(project["id"]), input_kind)).start()
        else:
            self.repository.add_message(
                project["id"], "system",
                TERMINAL_NOT_FOR_THIS_ACCOUNT + " Everything you supplied is saved.")
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
    #  Every method here re-checks ownership first, exactly like the rest of the class. Ownership
    #  is not the whole of it, though: see _may_use_terminal.
    def _may_use_terminal(self, principal: drafting.Principal) -> bool:
        """Whether this account may have a drafting agent at all.

        A DRAFTING TERMINAL IS A REAL SHELL. The agent runs interactively with permissions
        bypassed, as the operator's own unix user, on the machine that serves this site: it can
        read the application's .env, the box's credentials and every other tenant's files. That is
        the right capability for the person who owns the box, and it is the same thing their own
        dashboard gives them.

        It is the wrong capability for a stranger, and registration on this site is OPEN - anyone
        can sign up. So the terminal is admin-only unless an account is named in
        DRAFT_TERMINAL_USERS (comma-separated ids or emails). Everything else in the studio - the
        draft, review, sources, history, filing, uploads, hand editing - is unaffected.

        The real fix, when this needs to be open to customers, is a separate unix user per agent
        with no read access to the app: a permission ALLOW-LIST cannot do it, because a blanket
        Bash deny removes the tool the publish contract needs and a deny LIST is a blacklist
        somebody walks around with /bin/cat.
        """
        if getattr(principal, "is_admin", False):
            return True
        named = {item.strip().lower()
                 for item in os.environ.get("DRAFT_TERMINAL_USERS", "").split(",")
                 if item.strip()}
        if not named:
            return False
        if str(principal.user_id) in named:
            return True
        try:
            import accounts
            email = str((accounts.get_user(principal.user_id) or {}).get("email") or "")
        except Exception:                                       # noqa: BLE001 - deny, never crash
            return False
        return bool(email) and email.lower() in named

    def _require_terminal(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        project = self._project(principal, project_id)
        if not self._may_use_terminal(principal):
            raise drafting.DraftingPermissionDenied(TERMINAL_NOT_FOR_THIS_ACCOUNT)
        return project

    def terminal_state(self, principal: drafting.Principal, project_id: int) -> dict[str, Any]:
        self._project(principal, project_id)
        if not self._may_use_terminal(principal):
            return {"available": False, "reason": TERMINAL_NOT_FOR_THIS_ACCOUNT,
                    "status": "stopped", "detail": "", "exists": False, "running": False,
                    "models": [], "efforts": [], "default_model": "", "default_effort": "",
                    "model": "", "effort": "", "pane_width": 0, "pane_total": 0,
                    "session": ""}
        available = draft_terminal.availability()
        state = draft_terminal.state(project_id)
        return {**state, "available": bool(available.get("ok")),
                "reason": available.get("reason") or "",
                "models": available.get("models") or [],
                "efforts": available.get("efforts") or [],
                "default_model": available.get("default_model") or "",
                "default_effort": available.get("default_effort") or "",
                #  What this draft was actually switched to, so a reloaded page does not go back
                #  to claiming the server default over an agent running on something else.
                "model": draft_terminal.normalize_model(
                    self._project(principal, project_id).get("draft_model")),
                "effort": draft_terminal.normalize_effort(
                    self.repository.terminal_effort(project_id))}

    def start_terminal(self, principal: drafting.Principal, project_id: int, *,
                       restart: bool = False, fresh: bool = False) -> dict[str, Any]:
        """Start (or restart) this draft's agent, over a workspace rebuilt from the record.

        ``fresh`` also deletes the agent's private home, which is what "a new agent with blank
        memory" means in practice: a new conversation, no transcript of the old one, and nothing
        it learned last week.
        """
        project = self._require_terminal(principal, project_id)
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
        self._require_terminal(principal, project_id)
        return draft_terminal.tail(project_id, known_lines=known_lines, last_hash=last_hash)

    def send_to_agent(self, principal: drafting.Principal, project_id: int,
                      message: str, *, section_key: str = "") -> dict[str, Any]:
        """Type a message into the drafting agent, starting it first if it is not running."""
        project = self._require_terminal(principal, project_id)
        if project.get("status") == "archived":
            raise drafting.DraftingConflict("Restore this project before drafting on it.")
        body = str(message or "").replace("\x00", "").strip()
        if not body:
            raise drafting.DraftingValidationError("Say what you would like changed.")
        body = frame_section_request(body, section_key)
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

    def send_review_to_agent(self, principal: drafting.Principal,
                             project_id: int) -> dict[str, Any]:
        """Hand the latest review to the drafting agent as one instruction."""
        self._require_terminal(principal, project_id)
        qa = self.repository.latest_qa(project_id)
        if not qa:
            raise drafting.DraftingValidationError(
                "There is no review to send yet. Run one first.")
        message, items = review_fix_message(qa)
        if not items:
            raise drafting.DraftingValidationError(
                "The review found nothing to fix.")
        self.send_to_agent(principal, project_id, message)
        return {"sent": True, "items": items, "version_no": qa.get("version_no")}

    def terminal_keys(self, principal: drafting.Principal, project_id: int,
                      keys: Sequence[str]) -> list[str]:
        self._require_terminal(principal, project_id)
        try:
            return draft_terminal.send_keys(project_id, keys)
        except draft_terminal.TerminalError as exc:
            raise drafting.DraftingValidationError(str(exc)) from exc

    def interrupt_terminal(self, principal: drafting.Principal, project_id: int) -> bool:
        self._require_terminal(principal, project_id)
        return draft_terminal.interrupt(project_id)

    def set_terminal_model(self, principal: drafting.Principal, project_id: int,
                           model: str) -> str:
        self._require_terminal(principal, project_id)
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
        self._require_terminal(principal, project_id)
        try:
            chosen = draft_terminal.set_effort(project_id, effort)
        except draft_terminal.TerminalError as exc:
            raise drafting.DraftingConflict(str(exc)) from exc
        self.repository.set_terminal_effort(project_id, chosen)
        return chosen

    def stop_terminal(self, principal: drafting.Principal, project_id: int) -> bool:
        self._require_terminal(principal, project_id)
        return draft_terminal.kill(project_id)

    # -- what the agent publishes -----------------------------------------------------------------
    def agent_figure_report(self, project_id: int, token: str) -> dict[str, Any]:
        """What is on the uploaded sheets, and where the workspace text disagrees with them.

        NO principal, for the same reason ``publish_workspace`` has none: the caller is the agent
        inside the workspace, over loopback, holding this project's own token. It reads the
        sections from the WORKSPACE rather than from the last published version, because the
        agent runs this while it is editing and an answer about a version it has moved past would
        send it to fix something it has already fixed.
        """
        import draft_figures
        import filing_service
        loaded = _runner()._load(int(project_id))
        project = loaded["project"]
        workspace = draft_workspace.for_project(int(project_id))
        if not draft_terminal.verify_publish_token(workspace, token):
            raise drafting.DraftingPermissionDenied("That is not this draft's agent token.")
        snapshot = draft_workspace.snapshot(workspace)
        figures = []
        for figure in draft_figures.listing(int(project_id), int(project["user_id"])):
            _mime, png = draft_figures.png_bytes(
                int(figure["id"]), int(project["user_id"]),
                int(figure.get("active_version") or 0))
            if png:
                figures.append({"label": draft_figures.canonical_figure_label(
                    figure.get("figure_label")), "png": png})
        return filing_service.agent_report(
            sections=snapshot["sections"], numerals=snapshot["numerals"], figures=figures,
            project_id=int(project_id))

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
            #  Which claims stand alone, decided once on the server. The page marks them, the
            #  review counts them and the fee worksheet bills them off the same reading.
            "claims": draft_qa.claim_map(
                str((latest_version or {}).get("sections", {}).get("claims") or ""),
                target_independent=draft_workspace.independent_claim_target(project)),
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
                      query: str, status: str, level: str = "find",
                      query_note: str = "") -> dict[str, Any]:
        self._project(principal, project_id)
        import research_levels
        row = self.repository.add_search(
            project_id, principal.user_id, slug, query, status=status, level=level,
            query_note=query_note)
        item = research_levels.BY_ID.get(level) or {}
        self.repository.add_message(
            project_id, "system",
            f"Research started on the current draft at the {item.get('label', level)} level "
            f"({item.get('eta', 'no estimate')}). The results appear in the Research panel under "
            "the draft, and they are saved to your search history as search "
            f"{slug}.")
        return row

    def redraft_from_search(self, principal: drafting.Principal, project_id: int, slug: str, *,
                            level: str = "find", top: int = 8) -> dict[str, Any]:
        """Hand a finished research run to the drafting agent and ask it to draft around it.

        A search that finishes and sits on the page changes nothing, and an application filed
        against art the search already found is the expensive failure this button exists to stop.
        So it does the two things a person would otherwise do by hand and forget half of:
        ATTACHES the references, so the agent can read the documents rather than a summary, and
        then raises ONE turn that says what the search established and, crucially, what it did
        not.
        """
        import draft_novelty
        import research_levels
        project = self._project(principal, project_id)
        tracked = self.repository.search(project_id, slug)
        if not tracked:
            raise drafting.DraftingNotFound("That search was not started from this draft.")
        #  ONE LOAD. `report_loader` is the same trusted path every other reference selection
        #  goes through, and it hands back the whole view: the cards for what to attach, and the
        #  claim chart for the measurement below. Loading it twice would be two chances for the
        #  page and the message to disagree about what the search found.
        view = self.drafting_service.report_loader(
            principal, str(slug), int(project["user_id"])) or {}
        cards = [card for card in (view.get("cards") or []) if isinstance(card, Mapping)]
        publications = [str(card.get("pub") or "") for card in cards if card.get("pub")][:top]
        if not publications:
            raise drafting.DraftingValidationError(
                "That search has no ranked references yet, so there is nothing to draft around.")
        imported = self.import_search(principal, project_id, slug, publications)

        #  ONLY THE TIER THAT CHARTS GETS A MEASUREMENT. `read_view` reads the report's own claim
        #  chart; on a tier that built none it returns nothing, and passing that through as a zero
        #  would tell the agent the art reached none of the claims when the truth is that nobody
        #  looked.
        reading = None
        if (research_levels.BY_ID.get(level) or {}).get("charts"):
            try:
                reading = draft_novelty.read_view(view)
            except Exception:                                  # noqa: BLE001 - never lose the turn
                traceback.print_exc()
                reading = None

        references = [{"publication_number": str(card.get("pub") or ""),
                       "title": card.get("title") or "",
                       "publication_date": card.get("publication_date") or "",
                       "relevance_summary": card.get("why") or card.get("relevance") or ""}
                      for card in cards[:top]]
        item = research_levels.BY_ID.get(level) or research_levels.BY_ID[research_levels.DEFAULT]
        message = research_levels.redraft_request(
            label=item["label"], level_id=item["id"], slug=slug,
            note=str(tracked.get("query_note") or "searched on this draft"),
            references=references, reading=reading)
        turn = self.repository.enqueue_turn_safely(
            project_id, principal.user_id, kind="revise", user_message=message,
            project_revision=int(project.get("revision") or 1),
            idempotency_key=f"redraft-{slug}")
        self.repository.update_search(project_id, slug, status="complete",
                                      redrafted_turn_id=int(turn["id"]))
        kick()
        return {"turn_id": int(turn["id"]), "imported": int(imported),
                "references": len(references)}

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


REVIEW_FIX_MAX_CHARS = 12_000


def review_fix_message(qa: Mapping[str, Any]) -> tuple[str, int]:
    """The whole review, written out for the agent to work through, and how many items it holds.

    Built here rather than in the browser. The page truncates an item list to keep a check
    readable and collapses everything that passed, so a message assembled from what happens to be
    on screen is a message missing whatever the reader had not opened. This reads the stored
    report.
    """
    checks = [item for item in (qa.get("checks") or []) if item.get("status") != "pass"]
    findings = list(qa.get("findings") or [])
    if not checks and not findings:
        return "", 0
    lines = [
        f"The review of version {qa.get('version_no') or 'this draft'} did not pass. Everything "
        "it raised is below. Work through all of it, then publish.",
        "",
    ]
    if checks:
        lines.append("MECHANICAL CHECKS THAT DID NOT PASS")
        for check in checks:
            lines.append(f"- [{check.get('status')}] {str(check.get('name') or '')}: "
                         f"{str(check.get('detail') or '')}")
            for item in list(check.get("items") or [])[:40]:
                lines.append(f"    - {str(item)[:400]}")
        lines.append("")
    if findings:
        lines.append("REVIEWER FINDINGS")
        for finding in findings:
            lines.append(f"- [{finding.get('severity') or 'minor'}] "
                         f"{str(finding.get('title') or '')} "
                         f"({str(finding.get('where') or '')}): "
                         f"{str(finding.get('detail') or '')}")
            if finding.get("evidence"):
                lines.append(f"    text: {str(finding['evidence'])[:400]}")
            if finding.get("fix"):
                lines.append(f"    suggested fix: {str(finding['fix'])[:400]}")
        lines.append("")
    lines += [
        "Fix every one of them. Where a check is a false positive, change the wording or the "
        "figure specification until it passes rather than leaving it and explaining why: the "
        "check runs again on the next version and a draft nobody can get clean is a draft nobody "
        "can file. Do not ask me which ones to do.",
    ]
    body = "\n".join(lines)
    if len(body) > REVIEW_FIX_MAX_CHARS:
        #  Truncated at a line boundary and SAID so, rather than cut mid-item and read as the
        #  whole list.
        body = body[:REVIEW_FIX_MAX_CHARS].rsplit("\n", 1)[0]
        body += ("\n\n(That list was cut to fit. Run the review again after this round for the "
                 "rest.)")
    return body, len(checks) + len(findings)


def frame_section_request(message: str, section_key: str) -> str:
    """Say which section a request is about, when the person asked from inside one.

    THE BUG THIS FIXES. The Draft tab lets you open one section and ask for a change to it. That
    request went to the agent as bare text, with nothing saying where it came from: somebody
    opened Field of the Disclosure, asked for it to be longer and more detailed, and the agent -
    which had last been talking about the title - lengthened the title. It was not wrong to; it
    was never told. The page knew, and threw the knowledge away between the click and the send.

    Written as a sentence rather than a tag, because it is typed into a conversation and the
    agent reads it as one. The file name is included because that is what the agent edits.
    """
    body = str(message or "").strip()
    entry = draft_workspace.SECTION_BY_KEY.get(str(section_key or ""))
    if not entry:
        return body
    filename, heading = entry
    return (f"This is about one section of the application: {heading}, which is "
            f"draft/{filename}. Change that section. Leave the other sections alone unless a "
            f"change there is needed to stay consistent with this one, and say so if you make "
            f"one.\n\n{body}")


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
    """Never. There is nothing left for an autonomous repair chain to repair.

    This used to queue itself another `gate_resume` turn whenever one failed - up to six - so a
    difficult application could finish its drawing and filing gates without anybody watching. Two
    things ended that. The drawing gate is gone with the image generation it drove, and the
    drafting agent is now a person's interactive session rather than a queue: there is somebody at
    the terminal, and the Review tab tells them what is still wrong.

    Left as a chain of one-line facts rather than deleted, because the shape of what it did is the
    reason it must not run: it was measured on 2026-08-30 re-queuing FIVE drawing turns across five
    projects, minutes after the last five were cancelled, each one spending model time redrawing
    sheets for a feature the product no longer has. An autonomous loop that nobody is watching and
    that spends money is exactly the thing to switch off deliberately, once, rather than to leave
    running because its trigger looks unreachable.
    """
    return ""


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
    if "pytest" not in sys.modules and not app.config.get("TESTING"):
        #  Started whatever DRAFT_TURN_WORKER says, and deliberately: that flag is about the turn
        #  QUEUE, and the drafting agents are not on it. A copy of this app with the worker off
        #  still opens terminals and still has to close the ones nobody came back to.
        draft_terminal.start_reaper()
        #  And the auto-push, for the same reason: the agent is told never to ask, and when one
        #  asks anyway the page it asked may already be closed. A question nobody answers costs
        #  the whole turn.
        draft_terminal.start_auto_answer()
        if os.environ.get("DRAFT_TURN_WORKER", "1").lower() not in ("0", "false", "no"):
            start_worker()
    return app
