"""The drafting conversation: persistence, the prompt, and the loop that runs one turn.

PHASE TWO OF THE PRODUCT.  Phase one finds the art.  This writes the application, and it is a
conversation rather than a form: the user says what the invention is (or hands over a draft they
already wrote), the agent drafts, the user reacts, the agent revises.  A separate reviewer runs
after every single iteration whether or not anyone asked for it.

WHAT AN ITERATION IS
    user message  ->  workspace built from Postgres  ->  drafting agent edits the files
                  ->  text validation  ->  checked drawing generation  ->  independent review
                  ->  automatic repair until clean  ->  immutable version and report published

Everything durable lives in Postgres and the workspace is a rebuildable cache, so a restart in the
middle of a fifteen-minute drafting run loses the run, not the project - and the recovery pass
turns an abandoned lease back into a queued turn instead of a project stuck saying "drafting…"
for ever.

INPUT IS OPTIONAL IN BOTH DIRECTIONS.  A project may start from a description or from an existing
draft; it may have a finished prior-art search behind it, a pile of uploaded references, a single
publication number the user typed, or nothing at all.  Prior art makes the draft better and the
prompt says exactly how; its absence is stated plainly in the output rather than hidden.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import draft_agent
import draft_cite
import draft_qa
import draft_workspace
import drafting

try:
    import db
except ModuleNotFoundError:                    # prompt/validation tests need no driver
    db = None                                  # type: ignore[assignment]

MAX_MESSAGE_CHARS = 20_000
MAX_TURNS_LISTED = 200
LEASE_SECONDS = 2400                           # a full drafting turn plus its review
MAX_FINALIZATION_ROUNDS = max(
    2, min(int(os.environ.get("DRAFT_FINALIZATION_ROUNDS", "6")), 6))
_GATE_RESUME_KEY = "_gate_resume"
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_MIGRATION = Path(__file__).resolve().parents[1] / "sql" / "006_draft_agent.sql"


class StudioError(drafting.DraftingError):
    pass


class DrawingInspectionError(StudioError):
    def __init__(self, errors: Sequence[str]):
        self.errors = [str(item)[:2000] for item in errors if str(item).strip()]
        super().__init__(f"{len(self.errors)} drawing sheet(s) did not pass inspection.")


class FilingPreflightError(drafting.DraftingValidationError):
    """A filing gate failure carrying the scope an automatic repair may change."""

    def __init__(self, message: str, *, category: str = "internal_logic"):
        self.category = str(category or "internal_logic")[:40]
        super().__init__(message)


def human_text(value: Any) -> Any:
    """Remove a disallowed punctuation mark from every model-written human-facing string."""
    if isinstance(value, str):
        return re.sub(r"\s*\u2014\s*", " - ", value)
    if isinstance(value, Mapping):
        return {key: human_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [human_text(item) for item in value]
    if isinstance(value, tuple):
        return tuple(human_text(item) for item in value)
    return value


def _gate_resume_report(runs: Sequence[draft_agent.AgentRun],
                        result: Mapping[str, Any]) -> dict[str, Any]:
    """Persist enough agent state to resume deterministic gates after a process restart."""
    final = runs[-1]
    steps = [dict(step) for run in runs for step in run.steps if isinstance(step, Mapping)]
    return {
        "status": "running",
        "verdict": "pending",
        "summary": "The filing candidate is complete. Automatic drawing and review checks "
                   "are continuing.",
        "checks": [],
        "findings": [],
        "counts": {},
        _GATE_RESUME_KEY: human_text({
            "session_id": final.session_id,
            "model": final.model,
            "cost_usd": sum(run.cost_usd for run in runs),
            "duration_ms": sum(run.duration_ms for run in runs),
            "num_turns": sum(run.num_turns for run in runs),
            "steps": steps[-80:],
            "result": dict(result),
        }),
    }


def _gate_resume_run(context: Mapping[str, Any], turn: Mapping[str, Any]
                     ) -> draft_agent.AgentRun | None:
    """Restore an agent result only for the same interrupted leased turn."""
    if int(context.get("resuming_candidate_turn_id") or 0) != int(turn["id"]):
        return None
    prepared = context.get("prepared_qa")
    marker = prepared.get(_GATE_RESUME_KEY) if isinstance(prepared, Mapping) else None
    if not isinstance(marker, Mapping) or not isinstance(marker.get("result"), Mapping):
        return None
    session_id = str(marker.get("session_id") or "")
    if not session_id:
        return None
    steps = [dict(step) for step in (marker.get("steps") or [])
             if isinstance(step, Mapping)]
    return draft_agent.AgentRun(
        ok=True,
        result=human_text(dict(marker["result"])),
        session_id=session_id,
        model=str(marker.get("model") or ""),
        cost_usd=float(marker.get("cost_usd") or 0),
        duration_ms=int(marker.get("duration_ms") or 0),
        num_turns=int(marker.get("num_turns") or 0),
        steps=human_text(steps),
    )


# =============================================================================================
# The drafting prompt
# =============================================================================================
TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["revised", "answered"]},
        "summary": {"type": "string"},
        "reasoning": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "array", "items": {"type": "string"}},
        "prior_art_strategy": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 0},
        "answer": {"type": "string"},
    },
    "required": ["action", "summary", "reasoning", "changes", "prior_art_strategy", "questions",
                 "answer"],
    "additionalProperties": False,
}

DRAFT_SYSTEM = """You are drafting a US utility patent application. You work by editing files in
this workspace; the files ARE the draft.

WHAT YOU ARE AND ARE NOT
You produce complete, internally consistent, filing-ready patent application text and drawing
specifications. You do not give legal advice and you never state or imply that anything is patentable, novel,
non-obvious, valid, infringing, or clear to practise. Write the application; leave the conclusions
to the attorney who signs it.

THE ONE RULE THAT OUTRANKS EVERY OTHER
The inventor's disclosure (input/disclosure.md) and what the user tells you in conversation are
the ONLY authority for what the invention is. Prior art in prior_art/ is context to write AROUND,
never a source to borrow from. Never invent a core structure, relationship, result, measurement,
or experimental fact. When an optional implementation detail is not supplied, write supported
functional language and disclosed alternatives without choosing a made-up value. Omit an
unsupported optional limitation from the claims. If no related application or priority claim was
supplied, write `Not applicable.` in the Cross-Reference section; never delete the section.

FILING-CLEAN OUTPUT IS ABSOLUTE
No placeholder, drafting note, TODO, TBD, blank field, instruction to a draftsperson, question to
the inventor, or request for confirmation may appear anywhere in draft/, draft/numerals.md, or
figures/. Resolve each issue conservatively from the disclosure or omit the unsupported optional
detail. Return `questions` as an empty array. The automatic review will reject the entire turn if
one unfinished marker remains. Use commas, colons, full stops, or ordinary hyphens; never use an
em dash.

DRAWINGS FOLLOW THE INVENTION
Generated drawing pixels are evidence to inspect, never authority for the invention. The inventor
sources govern the patent text, the patent text governs each figure brief, and each brief governs
the rendered sheet. Never add or widen a structure, relationship, embodiment, numeral definition,
or claim to accommodate something an image model drew. Correct or regenerate the drawing instead.

WORKING AROUND THE PRIOR ART
Read prior_art/INDEX.md and then every reference file before you write. For each one, work out
what it actually teaches - not what its title suggests. Then:
  * Draft the independent claims so that each one recites, in its own terms, at least one concrete
    feature or relationship that no reference in prior_art/ discloses, and make that feature do
    real technical work in the invention rather than being an arbitrary addition. Say in your
    reasoning WHICH feature carries the claim clear of WHICH reference.
  * Never write a claim whose recited combination you have just read in one of these documents.
  * Where two references together would suggest the combination, add the feature that neither
    teaches and that neither gives a reason to look for, and explain in the specification the
    technical problem it solves. That explanation, in the specification, is what a later argument
    is built from, so it must be there before it is needed.
  * Describe the invention in a way that does not read onto the art: choose terminology that is
    accurate for your invention and is not the art's own vocabulary for a different thing.

CLAIMS: BROAD BUT DEFENSIBLE
  * Claim 1 is the broadest statement of the invention that the description fully supports and
    that no reference in prior_art/ discloses. Broad means: no limitation in claim 1 that is not
    needed to distinguish the art or to make the invention work. Every unnecessary word in claim 1
    is scope given away for nothing.
  * Defensible means: every limitation is described in the detailed description, in the same
    words; nothing is claimed that the description does not enable a skilled reader to build.
  * Include a graduated ladder of dependent claims - the next-narrowest fallback first, then
    genuinely different embodiments - so that if claim 1 falls there is somewhere to retreat to
    that is still worth having. A dependent claim that adds a trivial or purely aesthetic
    limitation is wasted; each one should add a feature you could argue independently.
  * Write independent claims in more than one statutory class where the disclosure supports it
    (apparatus and method; system and method of manufacture), because they are infringed by
    different parties.
  * Use consistent terminology and antecedent basis. Do not use means-plus-function language
    unless the user asks for it.

CITATIONS
Cite as `[REF:KEY]`, using exactly the keys in prior_art/INDEX.md. Citations belong in the
Background, and in the Detailed Description only to incorporate a document by reference. Never in
the title, summary, claims or abstract. Never cite a key that is not in the index; never
characterise a reference beyond what its file actually says. If the file does not support the
statement you want to make, make a weaker statement or none.

REFERENCE NUMERALS
Keep draft/numerals.md in step with the text at all times: every numeral used anywhere appears
there exactly once against one part name, and every part has one numeral. Introduce a numeral the
first time its part is named ("a suction cup 10"), and use the same words for it every time after.
Figures live in figures/, one file per drawing, listing the numerals that appear on it - a numeral
on a drawing that is not in the table, or a part described as visible in a figure whose file does
not list it, is a defect the review will find. Every application must include at least one figure.
Use a structural view, system diagram, or process flow as appropriate to the disclosed invention.
Normally use two to four figures. Do not list more than eight numerals on one sheet. When more
structure must be shown, add a focused detail or sectional sheet instead of overcrowding one image,
then synchronize the Brief Description of the Drawings and Detailed Description.
Keep each figure brief at or below 2800 characters. Include only disclosure-grounded geometry and
relationships needed to identify the listed parts. Never invent arbitrary exact counts,
proportions, relative heights, corner shapes, line counts, or placement constraints merely to
control the renderer. If a visual constraint is not in the disclosure or specification, omit it.
Figure files are Markdown specifications only. Never create SVG, PNG, or other image files. The
image pipeline generates unlabeled geometry, then adds the listed numerals, FIG. label, callouts,
and leader lines deterministically. Describe the required geometry and relationships, and list
the numerals, but never ask the geometry image to draw text or labels itself. Never address or
mention a draftsperson, drafter, illustrator, reviewer, attorney, or other person in a figure
brief. When a part name is only a semantic identifier, say that it does not appear as drawing
text.

FILES
  input/disclosure.md     the invention (read-only authority)
  input/brief.md          title, applicant, inventors, notes
  input/conversation.md   what has been said so far
  input/request.md        what the user is asking for THIS turn
  input/materials/        anything else the user uploaded
  prior_art/              the references, with INDEX.md listing the citation keys
  draft/01-title.md … draft/09-abstract.md    the application, body text only, no heading lines
  draft/numerals.md       the reference-numeral table
  figures/                one file per drawing
  review/previous-qa.md   what the reviewer found last time - fix it
  tools/patent_lookup.py  `python3 tools/patent_lookup.py US-9108319-B2` reads a publication out
                          of the local corpus of millions of patents. Use it when you need what a
                          reference ACTUALLY says, or to check a publication before citing it.

HOW TO WORK
Read before you write: the request, the conversation, the disclosure, the current draft, the
review. Then edit only what the request and the review require. A request to narrow one claim is
not licence to reword the background - an unnecessary rewrite destroys the user's own edits and
makes the change log useless.

FINISH by returning the structured answer. `reasoning` is read by the user and is the record of
why this draft is the shape it is: give the actual decisions - which feature you put in claim 1
and what it clears, what you deliberately left out of the independent claim and why, where the
description had to be widened to support a claim. Not a restatement of what you did."""

FIRST_TURN_PROMPT = """Write the complete filing-ready draft of this US patent application.

Read, in this order: input/request.md, input/brief.md, input/disclosure.md, every file in
prior_art/ (start with INDEX.md), and anything in input/materials/.

%(seeded)s

Then write every section in draft/, build draft/numerals.md, and write one file per drawing in
figures/ for the figures this invention needs. Specify the view, the visible structures and
relationships, and the exact numerals that appear. The image pipeline will draw these files
automatically, so make every instruction final and self-contained.

Return the structured answer with `action` set to "revised"."""

REVISE_PROMPT = """Continue drafting this application.

The user's request for this turn is in input/request.md. The reviewer's report on the current
draft is in review/previous-qa.md.

Do what the user asked. Then fix anything in the review that is genuinely wrong - and if you
think a review finding is mistaken, leave the draft alone and say why in your summary rather than
changing good text to silence a check.

Change only what needs changing. If the request is a question rather than a change, answer it in
`answer`, set `action` to "answered", and leave the files alone.

Return the structured answer."""

FINALIZE_PROMPT = """The current draft did not pass the automatic filing gate.

Read review/previous-qa.md, then fix every listed mechanical check and every independently
verified finding. Do not argue with a finding in the structured response. If wording triggered a
false positive, make the draft unambiguous enough that the check passes without changing the
invention. Keep the reference-numeral table, drawing descriptions, detailed description, claims,
and every file in figures/ synchronized.

Generated pixels are never authority for the invention. The authority order is the inventor's
disclosure and conversation, then the patent text, then the figure briefs, then the rendered
sheets. Never change the claims, description, numeral table, or disclosed embodiments merely to
excuse geometry, an object, or a leader endpoint that the image model drew incorrectly. Never add
an implementation detail or embodiment because it appeared in a generated image. For a drawing
finding, preserve the authoritative invention, strengthen the figure brief, and regenerate the
sheet from the authoritative text until the pixels conform. A stubborn rendering artifact remains
a drawing defect; it does not become part of the invention.

For a figure-plan coverage failure, never delete a disclosed part, numeral definition, or
supporting specification text. Redistribute labels among focused sheets, or add a focused sheet
when necessary, and synchronize the drawing descriptions.

If a figure brief is over-specified, shorten it by removing invented rendering constraints while
preserving the authoritative text, numeral table, disclosed geometry, and required relationships.

Leave no note, placeholder, question, or instruction for a person. Return the structured answer
with `action` set to "revised" and `questions` as an empty array."""

QUESTION_PROMPT = """The user has asked a question about this draft rather than asking for a
change. Their question is in input/request.md.

Read whatever you need in order to answer it accurately - the draft, the prior art, the
disclosure. Answer in `answer`, set `action` to "answered", and do not edit any file.

If answering honestly requires a change to the draft, say so in your answer and ask them to
confirm; do not make the change unasked."""


def build_prompt(kind: str, *, seeded: bool = False) -> str:
    if kind == "initial":
        return FIRST_TURN_PROMPT % {"seeded": (
            "The draft/ files have been pre-filled by splitting the user's own document on its "
            "headings. That split was mechanical and may be wrong: check that every part of the "
            "source document landed somewhere sensible, move anything that did not, and improve "
            "the result. Do not discard the user's text and start again."
            if seeded else
            "The draft/ files are empty. Write them.")}
    if kind == "question":
        return QUESTION_PROMPT
    return REVISE_PROMPT


def filing_blockers(report: Mapping[str, Any]) -> list[str]:
    """Reasons a workspace cannot be published as a filing-ready version."""
    blockers: list[str] = []
    if str(report.get("status") or "") != "complete":
        blockers.append("The independent review did not complete.")
    for check in report.get("checks") or ():
        if str(check.get("status") or "") != "pass":
            blockers.append(f"Mechanical check did not pass: {check.get('name') or 'unnamed'}")
    for finding in report.get("findings") or ():
        blockers.append(f"Independent review finding: {finding.get('title') or 'unnamed'}")
    return list(dict.fromkeys(blockers))


_DRAWING_INSPECTION_CHECK = "Every drawing sheet passes geometry, leader, and OCR inspection"
_FIGURE_PLAN_CHECKS = frozenset({
    "Each drawing numeral appears once",
    "Drawing sheets are not overcrowded",
    "Numerals on the drawings are defined",
    "Every drawing numeral appears in the specification",
    "Every specification numeral appears in a drawing",
    "Application includes a drawing plan",
    "Drawing briefs are concise and renderable",
    "Figure-sheet numbering is unique and contiguous",
    "Every figure used is described",
    "Every drawing sheet is described",
    "Each described figure has a drawing sheet",
})


def _report_item_category(item: Mapping[str, Any]) -> str:
    category = str(item.get("category") or "")
    if category:
        return category
    name = str(item.get("name") or "")
    if name == _DRAWING_INSPECTION_CHECK or name in _FIGURE_PLAN_CHECKS:
        return "figures_and_numerals"
    return ""


def restore_text_after_drawing_only_review(workspace: Path, snapshot: Mapping[str, Any],
                                           report: Mapping[str, Any]) -> bool:
    """Keep image-model artifacts from changing filing sources during an automatic repair.

    A drawing-only review may edit figure briefs, but it has no authority to change the filing
    text, redefine a numeral, add or remove a sheet, or move a numeral between sheets. Restore
    those sources after the agent returns while retaining corrected geometry instructions inside
    each existing figure brief. Mixed reviews are not locked because a source-fidelity,
    claim-support, or other text finding may legitimately require a broader repair.
    """
    checks = [item for item in (report.get("checks") or [])
              if str(item.get("status") or "") != "pass"]
    findings = list(report.get("findings") or [])
    if not checks and not findings:
        return False
    if any(str(item.get("name") or "") != _DRAWING_INSPECTION_CHECK for item in checks):
        return False
    if any(str(item.get("category") or "") != "figures_and_numerals" for item in findings):
        return False
    baseline_snapshot = candidate_snapshot_for_repair(snapshot)
    if baseline_snapshot is None:
        return False
    sections = baseline_snapshot["sections"]
    numerals = baseline_snapshot["numerals"]
    baseline_figures = human_text(
        [dict(item) for item in baseline_snapshot["figures"]])
    current_figures = human_text(draft_workspace.read_figures(workspace))
    used_current: set[int] = set()
    locked_figures = []
    for index, baseline in enumerate(baseline_figures):
        baseline_number = draft_qa.figure_number(baseline.get("label"))
        match = next(((item_index, item)
                      for item_index, item in enumerate(current_figures)
                      if item_index not in used_current and baseline_number and
                      draft_qa.figure_number(item.get("label")) == baseline_number), None)
        if match is None and index < len(current_figures) and index not in used_current:
            candidate = current_figures[index]
            if not baseline_number and str(candidate.get("label") or "").strip().lower() == \
                    str(baseline.get("label") or "").strip().lower():
                match = (index, candidate)
        current = match[1] if match else None
        if match:
            used_current.add(match[0])
        locked_figures.append({
            "label": str(baseline.get("label") or f"FIG. {index + 1}"),
            "caption": str((current or baseline).get("caption") or ""),
            "numerals": list(baseline.get("numerals") or []),
        })
    comparable_current = [{
        "label": str(item.get("label") or ""),
        "caption": str(item.get("caption") or ""),
        "numerals": list(item.get("numerals") or []),
    } for item in current_figures]
    sections_changed = draft_workspace.read_sections(workspace) != sections
    numerals_changed = draft_workspace.read_numerals(workspace) != numerals
    figures_changed = comparable_current != locked_figures
    if not (sections_changed or numerals_changed or figures_changed):
        return False
    if sections_changed:
        draft_workspace.write_sections(workspace, sections)
    if numerals_changed:
        draft_workspace.write_numerals(workspace, numerals)
    if figures_changed:
        draft_workspace.write_figures(workspace, locked_figures)
    return True


def restore_sources_after_figure_plan_review(workspace: Path, snapshot: Mapping[str, Any],
                                             report: Mapping[str, Any]) -> bool:
    """Keep figure-plan repairs from erasing the invention they must illustrate.

    A plan repair may redistribute numerals among sheets, add focused sheets, rewrite geometry
    briefs, and synchronize the Brief Description of the Drawings. The prior checked candidate
    remains authoritative for every other filing section and for the complete numeral table.
    Mixed reports are deliberately not locked because a non-figure finding may require a genuine
    source-text repair.
    """
    checks = [item for item in (report.get("checks") or [])
              if str(item.get("status") or "") != "pass"]
    findings = list(report.get("findings") or [])
    blockers = [*checks, *findings]
    if not blockers:
        return False
    if any(_report_item_category(item) != "figures_and_numerals" for item in blockers):
        return False

    baseline_snapshot = candidate_snapshot_for_repair(snapshot)
    if baseline_snapshot is None:
        return False
    baseline_sections = baseline_snapshot["sections"]
    current_sections = human_text(draft_workspace.read_sections(workspace))
    locked_sections = dict(baseline_sections)
    locked_sections["drawing_descriptions"] = str(
        current_sections.get("drawing_descriptions") or
        baseline_sections.get("drawing_descriptions") or "")
    baseline_numerals = baseline_snapshot["numerals"]

    sections_changed = current_sections != locked_sections
    numerals_changed = draft_workspace.read_numerals(workspace) != baseline_numerals
    if sections_changed:
        draft_workspace.write_sections(workspace, locked_sections)
    if numerals_changed:
        draft_workspace.write_numerals(workspace, baseline_numerals)
    return sections_changed or numerals_changed


def figures_for_qa(project_id: int, user_id: int,
                   figure_specs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Replace requested numerals with vision-detected pixels for every drawn sheet."""
    try:
        import draft_figures
        drawn = draft_figures.listing(project_id, user_id)
    except Exception:
        # Never substitute the intended labels for pixels that could not be loaded. The review may
        # continue, but it must carry a blocking, visible inspection result rather than a false pass.
        return [{**dict(item), "numerals": [], "drawn": False,
                 "numeral_audit": {"inspected": False,
                                     "error": "The drawing store could not be read."},
                 "leader_audit": {"inspected": False,
                                    "errors": ["The drawing store could not be read."]}}
                for item in figure_specs]

    def key(value):
        return draft_figures.figure_key(value)

    by_label = {key(item.get("figure_label")): item for item in drawn}
    out = []
    for spec in figure_specs:
        item = dict(spec)
        image = by_label.pop(key(spec.get("label")), None)
        item["drawn"] = False
        # A figure specification is not a drawing. Until an active image exists it contributes no
        # visible numerals to the bidirectional QA check.
        item["numerals"] = []
        if image:
            active = next((version for version in image.get("versions") or []
                           if int(version.get("version_no") or 0) ==
                           int(image.get("active_version") or 0)), None) or {}
            audit = active.get("numeral_audit") or {}
            semantic = active.get("semantic_audit") or {}
            leaders = active.get("leader_audit") or {}
            item["drawn"] = bool(active)
            if audit.get("inspected"):
                item["numerals"] = list(active.get("detected_numerals") or [])
            item["numeral_audit"] = dict(audit)
            item["semantic_audit"] = dict(semantic)
            item["leader_audit"] = dict(leaders)
        out.append(item)
    # Stored sheets whose figure specification disappeared are still real pixels. Include them so
    # an unexpected numeral or obsolete drawing cannot vanish from QA merely because the text side
    # was edited first.
    for image in by_label.values():
        active = next((version for version in image.get("versions") or []
                       if int(version.get("version_no") or 0) ==
                       int(image.get("active_version") or 0)), None) or {}
        audit = active.get("numeral_audit") or {}
        semantic = active.get("semantic_audit") or {}
        leaders = active.get("leader_audit") or {}
        out.append({"label": image.get("figure_label"), "caption": image.get("caption") or "",
                    "numerals": (list(active.get("detected_numerals") or [])
                                 if audit.get("inspected") else []),
                    "drawn": bool(active), "orphan": True, "numeral_audit": dict(audit)})
        out[-1]["semantic_audit"] = dict(semantic)
        out[-1]["leader_audit"] = dict(leaders)
    return out


# =============================================================================================
# Validation
# =============================================================================================
def validate_sections(sections: Mapping[str, str],
                      allowed_references: Sequence[str] = ()) -> dict[str, str]:
    """Check an agent-written draft hard enough to store it, and no harder.

    Deliberately looser than the one-shot generator's validator in ``drafting``: this draft is
    reviewed straight afterwards and shown to the user for another turn, so a citation in the
    Detailed Description (which is where a document is incorporated by reference) is correct
    rather than a reason to throw away twenty minutes of work.  What is still refused is what
    cannot be repaired by another turn: an empty section, a fabricated citation key, and any
    legal conclusion.
    """
    out: dict[str, str] = {}
    missing: list[str] = []
    total = 0
    for key, _name, heading in draft_workspace.SECTION_FILES:
        value = str(human_text(sections.get(key) or "")).replace("\x00", "").strip()
        if not value:
            missing.append(heading)
        out[key] = value
        total += len(value)
    if missing:
        raise drafting.DraftingValidationError(
            "The draft is missing " + ", ".join(missing) + ".")
    if total > drafting.MAX_GENERATED_CHARS:
        raise drafting.DraftingValidationError("The draft is too large to store safely.")

    allowed = {draft_cite.normalize(a) for a in allowed_references if draft_cite.normalize(a)}
    for key, _name, heading in draft_workspace.SECTION_FILES:
        for raw in draft_cite.malformed_citations_in(out[key]):
            raise drafting.DraftingValidationError(
                f"{heading} contains a malformed citation [REF:{raw[:40]}].")
        for citation in draft_cite.citations_in(out[key]):
            canonical = draft_cite.normalize(citation)
            if not canonical:
                raise drafting.DraftingValidationError(
                    f"{heading} cites an unusable publication number [REF:{citation[:40]}].")
            if canonical not in allowed:
                raise drafting.DraftingValidationError(
                    f"{heading} cites {canonical}, which is not among this project's sources.")

    joined = "\n".join(out.values())
    placeholders = draft_qa.find_placeholders(out)
    if placeholders:
        raise drafting.DraftingValidationError(
            "The draft contains an unresolved placeholder: " + placeholders[0] + ".")
    for pattern in drafting._LEGAL_CONCLUSION_PATTERNS:
        found = pattern.search(joined)
        if found:
            raise drafting.DraftingValidationError(
                f"The draft states a legal conclusion ({found.group(0)!r}). An application "
                "describes the invention; it does not conclude on patentability or infringement.")
    return out


def validate_snapshot(snapshot: Mapping[str, Any],
                      allowed_references: Sequence[str] = ()) -> dict[str, Any]:
    """Validate every agent-owned filing artifact before any image call or version save."""
    sections = validate_sections(snapshot.get("sections") or {}, allowed_references)
    numerals = [human_text(dict(item)) for item in (snapshot.get("numerals") or ())]
    figures = [human_text(dict(item)) for item in (snapshot.get("figures") or ())]
    markers = []
    markers.extend(draft_qa.placeholders_in_text(
        "Reference numeral table", json.dumps(numerals, ensure_ascii=False)))
    markers.extend(draft_qa.placeholders_in_text(
        "Drawing specifications", json.dumps(figures, ensure_ascii=False)))
    if markers:
        raise FilingPreflightError(
            "The filing artifacts contain an unresolved placeholder: " + markers[0] + ".",
            category="figures_and_numerals")
    for figure in figures:
        values = {draft_qa._drawing_numeral(value)
                  for value in (figure.get("numerals") or [])}
        values.discard("")
        if len(values) > draft_qa.MAX_NUMERALS_PER_SHEET:
            label = str(figure.get("label") or "Drawing")[:80]
            raise FilingPreflightError(
                f"{label} lists {len(values)} numerals, which is more than "
                f"{draft_qa.MAX_NUMERALS_PER_SHEET} numerals on one sheet. Split it into focused "
                "views and synchronize the drawing descriptions before generating images.",
                category="figures_and_numerals")
    mechanical = draft_qa.run_checks(
        sections=sections, numerals=numerals, figures=figures,
        allowed_references=allowed_references, allow_remote=False)
    failures = [item for item in mechanical if str(item.get("status") or "") == "fail"]
    if failures:
        details = []
        for item in failures[:8]:
            evidence = list(item.get("items") or [])
            details.append(
                f"{item.get('name') or 'Unnamed check'}: " +
                str(evidence[0] if evidence else item.get("detail") or "failed")[:300])
        category = ("figures_and_numerals"
                    if all(str(item.get("name") or "") in _FIGURE_PLAN_CHECKS
                           for item in failures)
                    else "internal_logic")
        raise FilingPreflightError(
            "The candidate failed the mechanical filing preflight. " + "; ".join(details),
            category=category)
    return {"sections": sections, "numerals": numerals, "figures": figures}


def candidate_snapshot_for_repair(snapshot: Any) -> dict[str, Any] | None:
    """Recover a once-validated candidate after a newer preflight rule blocks publication."""
    if not isinstance(snapshot, Mapping):
        return None
    raw_sections = snapshot.get("sections")
    raw_numerals = snapshot.get("numerals")
    raw_figures = snapshot.get("figures")
    if (not isinstance(raw_sections, Mapping) or
            not isinstance(raw_numerals, Sequence) or isinstance(raw_numerals, (str, bytes)) or
            not isinstance(raw_figures, Sequence) or isinstance(raw_figures, (str, bytes))):
        return None
    sections = {}
    for key, _name, _heading in draft_workspace.SECTION_FILES:
        value = raw_sections.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        sections[key] = human_text(value.replace("\x00", "").strip())
    if not all(isinstance(item, Mapping) for item in raw_numerals):
        return None
    if not all(isinstance(item, Mapping) for item in raw_figures):
        return None
    return {
        "sections": sections,
        "numerals": human_text([dict(item) for item in raw_numerals]),
        "figures": human_text([dict(item) for item in raw_figures]),
    }


def candidate_preflight_report(report: Mapping[str, Any] | None,
                               error: Exception) -> dict[str, Any]:
    """Add the current gate failure to an older candidate's repair instructions."""
    out = human_text(dict(report or {}))
    out.pop(_GATE_RESUME_KEY, None)
    name = "Saved candidate passes the current filing preflight"
    superseded = {name, "Drafting run completed"}
    checks = [dict(item) for item in (out.get("checks") or [])
              if isinstance(item, Mapping) and
              str(item.get("name") or "") not in superseded]
    detail = str(error)[:1200]
    checks.append({
        "name": name, "status": "fail", "severity": "error", "detail": detail,
        "items": [detail[:600]],
        "category": str(getattr(error, "category", "internal_logic"))[:40],
    })
    findings = [dict(item) for item in (out.get("findings") or [])
                if isinstance(item, Mapping)]
    out.update({
        "status": "failed", "verdict": "fail",
        "summary": "The saved candidate needs an automatic update for the current filing gates.",
        "checks": checks, "findings": findings,
        "counts": draft_qa.counts_for(checks, findings), "last_error": detail,
    })
    return out


def citations_of(sections: Mapping[str, str]) -> list[str]:
    out: list[str] = []
    for key, _name, _heading in draft_workspace.SECTION_FILES:
        for citation in draft_cite.citations_in(str(sections.get(key) or "")):
            canonical = draft_cite.normalize(citation) or citation
            if canonical not in out:
                out.append(canonical)
    return out


def project_title_from(version_no: int, sections: Mapping[str, str]) -> str:
    """The title the project should take from a newly saved version, or '' to leave it alone.

    The FIRST version names the project. Until one exists the title is a placeholder - usually the
    opening line of whatever the user pasted - so the drafts list and the page header show a
    sentence where the invention's title belongs. Only the first version does this, so a
    deliberate rename later is never undone.
    """
    if int(version_no) != 1:
        return ""
    title = str(sections.get("title") or "").strip()
    return title.splitlines()[0].strip()[:240] if title else ""


def render_markdown(sections: Mapping[str, str]) -> str:
    blocks = []
    for index, (key, _name, heading) in enumerate(draft_workspace.SECTION_FILES):
        prefix = "#" if index == 0 else "##"
        blocks.append(f"{prefix} {heading}\n\n{str(sections.get(key) or '').strip()}")
    return "\n\n".join(blocks).strip() + "\n"


def allowed_reference_keys(references: Sequence[Mapping[str, Any]],
                           documents: Sequence[Mapping[str, Any]]) -> list[str]:
    allowed = [str(row.get("publication_number") or "") for row in references
               if row.get("publication_number")]
    allowed += [str(row.get("publication_number")) for row in documents
                if row.get("kind") == "prior_art" and row.get("publication_number")]
    allowed += [f"UPLOAD-{index:02d}" for index in range(1, 1 + sum(
        1 for row in documents
        if row.get("kind") == "prior_art" and not row.get("publication_number")))]
    return allowed


# =============================================================================================
# Persistence
# =============================================================================================
def ensure_schema(force: bool = False) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return
        drafting.ensure_schema()
        sql = _MIGRATION.read_text(encoding="utf-8")
        with db.cursor(autocommit=True) as cur:
            try:
                cur.execute(sql, prepare=False)
            except TypeError:
                cur.execute(sql)
        _SCHEMA_READY = True


def reset_schema_cache_for_tests() -> None:
    global _SCHEMA_READY
    _SCHEMA_READY = False


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value if value is not None else fallback


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class StudioRepository:
    """Postgres boundary for the conversation. Ownership is enforced by the drafting repository."""

    def __init__(self, cursor_factory: Callable[..., Any] | None = None, *, migrate: bool = True):
        self._cursor_factory = cursor_factory
        self._migrate = migrate

    def _ready(self) -> None:
        if self._migrate:
            ensure_schema()

    def _cursor(self, **kwargs: Any):
        if self._cursor_factory:
            return self._cursor_factory(**kwargs)
        if db is None:
            raise RuntimeError("The Postgres driver is required for drafting persistence.")
        return db.cursor(**kwargs)

    # -- conversation ------------------------------------------------------------------------
    def add_message(self, project_id: int, role: str, body: str, *, turn_id: int | None = None,
                    payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._ready()
        if role not in ("user", "agent", "qa", "system"):
            raise drafting.DraftingValidationError("Unknown message role.")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO app_draft_messages (project_id,turn_id,role,body,payload) "
                "VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING *",
                (int(project_id), turn_id, role, str(body or "")[:MAX_MESSAGE_CHARS],
                 _dumps(dict(payload or {}))))
            row = dict(cur.fetchone())
            row["payload"] = _json(row.get("payload"), {})
            return row

    def message_cursor(self, project_id: int) -> dict[str, int]:
        """The cheap read behind the three-second poll: how many, and the newest id."""
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT count(*)::int AS n, coalesce(max(id),0) AS last "
                        "FROM app_draft_messages WHERE project_id=%s", (int(project_id),))
            row = cur.fetchone() or {}
            return {"count": int(row.get("n") or 0), "last_id": int(row.get("last") or 0)}

    def messages(self, project_id: int, *, limit: int = 400) -> list[dict[str, Any]]:
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT * FROM app_draft_messages WHERE project_id=%s "
                        "ORDER BY id LIMIT %s", (int(project_id), int(limit)))
            out = []
            for row in cur.fetchall():
                item = dict(row)
                item["payload"] = _json(item.get("payload"), {})
                out.append(item)
            return out

    # -- documents ---------------------------------------------------------------------------
    def add_document(self, project_id: int, user_id: int, *, kind: str, filename: str,
                     body: str, title: str = "", note: str = "", content_type: str = "",
                     publication_number: str | None = None) -> dict[str, Any]:
        self._ready()
        if kind not in ("prior_art", "material", "source_draft"):
            raise drafting.DraftingValidationError("Unknown document kind.")
        body = str(body or "").replace("\x00", "").strip()
        if not body:
            raise drafting.DraftingValidationError(
                f"No readable text could be extracted from {filename!r}.")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO app_draft_documents "
                "(project_id,uploaded_by_user_id,kind,filename,content_type,publication_number,"
                "title,note,body,char_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (int(project_id), int(user_id), kind, str(filename)[:300], str(content_type)[:120],
                 (draft_cite.normalize(publication_number) or None) if publication_number else None,
                 str(title)[:400], str(note)[:2000], body[:draft_workspace.MAX_DOCUMENT_CHARS],
                 len(body)))
            return dict(cur.fetchone())

    def documents(self, project_id: int) -> list[dict[str, Any]]:
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT * FROM app_draft_documents WHERE project_id=%s ORDER BY id",
                        (int(project_id),))
            return [dict(row) for row in cur.fetchall()]

    def delete_document(self, project_id: int, document_id: int) -> None:
        self._ready()
        with self._cursor() as cur:
            cur.execute("DELETE FROM app_draft_documents WHERE project_id=%s AND id=%s",
                        (int(project_id), int(document_id)))

    # -- searches launched without leaving the studio ----------------------------------------
    def add_search(self, project_id: int, user_id: int, slug: str, query: str,
                   status: str = "running") -> dict[str, Any]:
        self._ready()
        if status not in ("running", "complete", "error"):
            status = "running"
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO app_draft_searches "
                "(project_id,requested_by_user_id,slug,query,status,completed_at) "
                "VALUES (%s,%s,%s,%s,%s,CASE WHEN %s='complete' THEN now() ELSE NULL END) "
                "ON CONFLICT (project_id,slug) DO UPDATE SET status=EXCLUDED.status,"
                "query=EXCLUDED.query,completed_at=EXCLUDED.completed_at RETURNING *",
                (int(project_id), int(user_id), str(slug)[:64], str(query)[:60_000], status, status))
            return dict(cur.fetchone())

    def searches(self, project_id: int, limit: int = 20) -> list[dict[str, Any]]:
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT * FROM app_draft_searches WHERE project_id=%s "
                        "ORDER BY id DESC LIMIT %s", (int(project_id), int(limit)))
            return [dict(row) for row in cur.fetchall()]

    def search(self, project_id: int, slug: str) -> dict[str, Any] | None:
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT * FROM app_draft_searches WHERE project_id=%s AND slug=%s",
                        (int(project_id), str(slug)))
            row = cur.fetchone()
        return dict(row) if row else None

    def update_search(self, project_id: int, slug: str, *, status: str,
                      imported_count: int | None = None) -> None:
        self._ready()
        if status not in ("running", "complete", "error"):
            raise drafting.DraftingValidationError("Unknown search status.")
        with self._cursor() as cur:
            cur.execute(
                "UPDATE app_draft_searches SET status=%s,"
                "completed_at=CASE WHEN %s='complete' THEN coalesce(completed_at,now()) "
                "ELSE completed_at END, imported_count=coalesce(%s,imported_count) "
                "WHERE project_id=%s AND slug=%s",
                (status, status, imported_count, int(project_id), str(slug)))

    # -- references (prior art added outside a search report) ---------------------------------
    def add_reference(self, project_id: int, *, publication_number: str, title: str,
                      source_url: str | None, relevance_summary: str,
                      snapshot: Mapping[str, Any], origin: str = "manual",
                      report_rank: int = 9000) -> None:
        self._ready()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO app_drafting_references "
                "(project_id,publication_number,report_rank,title,source_url,relevance_summary,"
                "snapshot,origin) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                "ON CONFLICT (project_id,publication_number) DO UPDATE SET "
                "title=EXCLUDED.title,source_url=EXCLUDED.source_url,snapshot=EXCLUDED.snapshot",
                (int(project_id), publication_number, int(report_rank), str(title)[:1000],
                 source_url, str(relevance_summary)[:8000], _dumps(dict(snapshot)), origin))

    def remove_reference(self, project_id: int, publication_number: str) -> None:
        self._ready()
        with self._cursor() as cur:
            cur.execute("DELETE FROM app_drafting_references WHERE project_id=%s "
                        "AND publication_number=%s", (int(project_id), publication_number))

    # -- turns --------------------------------------------------------------------------------
    def enqueue_turn(self, project_id: int, user_id: int, *, kind: str, user_message: str,
                     project_revision: int, idempotency_key: str | None = None) -> dict[str, Any]:
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT id,status FROM app_draft_turns WHERE project_id=%s "
                        "AND status IN ('queued','running') LIMIT 1", (int(project_id),))
            if cur.fetchone():
                raise drafting.DraftingConflict(
                    "The drafting agent is still working on the previous message.")
            if idempotency_key:
                cur.execute("SELECT * FROM app_draft_turns WHERE project_id=%s "
                            "AND idempotency_key=%s", (int(project_id), idempotency_key))
                prior = cur.fetchone()
                if prior:
                    return self._turn(dict(prior))
            cur.execute("SELECT coalesce(max(turn_no),0)+1 AS n FROM app_draft_turns "
                        "WHERE project_id=%s", (int(project_id),))
            turn_no = int(cur.fetchone()["n"])
            cur.execute(
                "INSERT INTO app_draft_turns (project_id,turn_no,requested_by_user_id,"
                "project_revision,kind,user_message,idempotency_key,stage) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'queued') RETURNING *",
                (int(project_id), turn_no, int(user_id), int(project_revision), kind,
                 str(user_message or "")[:MAX_MESSAGE_CHARS], idempotency_key))
            turn = self._turn(dict(cur.fetchone()))
            cur.execute("UPDATE app_drafting_projects SET status='queued',updated_at=now() "
                        "WHERE id=%s", (int(project_id),))
            return turn

    # A second tab, or a double click on Send, races the check above; the partial unique index
    # is the real guard and its violation means the same thing the check does.
    def enqueue_turn_safely(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self.enqueue_turn(*args, **kwargs)
        except drafting.DraftingError:
            raise
        except Exception as exc:                                   # noqa: BLE001
            if "app_draft_turns_one_active_uq" in str(exc):
                raise drafting.DraftingConflict(
                    "The drafting agent is still working on the previous message.") from exc
            raise

    def turns(self, project_id: int, *, limit: int = MAX_TURNS_LISTED) -> list[dict[str, Any]]:
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT * FROM app_draft_turns WHERE project_id=%s "
                        "ORDER BY turn_no DESC LIMIT %s", (int(project_id), int(limit)))
            return [self._turn(dict(row)) for row in cur.fetchall()]

    def save_retry_candidate(self, turn_id: int, lease_token: str, *,
                             snapshot: Mapping[str, Any], report: Mapping[str, Any]) -> None:
        """Durably checkpoint a valid but blocked candidate for the next leased attempt."""
        self._ready()
        with self._cursor() as cur:
            self._verify(cur, turn_id, lease_token)
            cur.execute(
                "INSERT INTO app_draft_turn_candidates (turn_id,snapshot,qa_report) "
                "VALUES (%s,%s::jsonb,%s::jsonb) ON CONFLICT (turn_id) DO UPDATE SET "
                "snapshot=EXCLUDED.snapshot,qa_report=EXCLUDED.qa_report,updated_at=now()",
                (int(turn_id), _dumps(dict(snapshot)), _dumps(dict(report))))

    def retry_candidate(self, turn_id: int) -> dict[str, Any] | None:
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT snapshot,qa_report FROM app_draft_turn_candidates WHERE turn_id=%s",
                        (int(turn_id),))
            row = cur.fetchone()
        if not row:
            return None
        return {"turn_id": int(turn_id),
                "snapshot": _json(row.get("snapshot"), {}),
                "qa_report": _json(row.get("qa_report"), {})}

    def latest_retry_candidate(self, project_id: int, *, before_turn_id: int
                               ) -> dict[str, Any] | None:
        """Return the newest unpublished candidate from an earlier turn in this project."""
        self._ready()
        with self._cursor() as cur:
            cur.execute(
                "SELECT c.turn_id,c.snapshot,c.qa_report "
                "FROM app_draft_turn_candidates c JOIN app_draft_turns t ON t.id=c.turn_id "
                "WHERE t.project_id=%s AND c.turn_id<%s "
                "ORDER BY t.turn_no DESC,c.updated_at DESC LIMIT 1",
                (int(project_id), int(before_turn_id)))
            row = cur.fetchone()
        if not row:
            return None
        return {"turn_id": int(row["turn_id"]),
                "snapshot": _json(row.get("snapshot"), {}),
                "qa_report": _json(row.get("qa_report"), {})}

    def discard_retry_candidate(self, turn_id: int) -> None:
        self._ready()
        with self._cursor() as cur:
            cur.execute("DELETE FROM app_draft_turn_candidates WHERE turn_id=%s", (int(turn_id),))

    def latest_turn(self, project_id: int) -> dict[str, Any] | None:
        rows = self.turns(project_id, limit=1)
        return rows[0] if rows else None

    def cancel_turn(self, project_id: int, turn_id: int) -> None:
        self._ready()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE app_draft_turns SET status='cancelled',stage='cancelled',"
                "completed_at=now(),updated_at=now(),lease_token_hash=NULL,lease_expires_at=NULL,"
                "last_error='Cancelled by the user' WHERE project_id=%s AND id=%s "
                "AND status IN ('queued','running')", (int(project_id), int(turn_id)))
            cur.execute("UPDATE app_drafting_projects SET status=CASE WHEN latest_version_no>0 "
                        "THEN 'ready' ELSE 'active' END,updated_at=now() WHERE id=%s",
                        (int(project_id),))
            cur.execute("DELETE FROM app_draft_turn_candidates WHERE turn_id=%s", (int(turn_id),))

    @staticmethod
    def _turn(row: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in ("reasoning", "changes", "questions"):
            out[key] = _json(out.get(key), [])
        out.pop("lease_token_hash", None)
        out["cost_usd"] = float(out.get("cost_usd") or 0)
        return out

    # -- worker boundary ----------------------------------------------------------------------
    def claim_turn(self, worker_id: str, *, lease_seconds: int = LEASE_SECONDS
                   ) -> dict[str, Any] | None:
        """Take one queued turn, or one whose worker died, with an unguessable lease."""
        self._ready()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE app_draft_turns t SET status='cancelled',stage='cancelled',"
                "completed_at=now(),updated_at=now(),last_error='The project was archived' "
                "FROM app_drafting_projects p WHERE p.id=t.project_id "
                "AND t.status IN ('queued','running') AND p.status='archived'")
            cur.execute(
                "WITH spent AS ("
                " UPDATE app_draft_turns SET status='failed',stage='failed',completed_at=now(),"
                " updated_at=now(),lease_token_hash=NULL,lease_expires_at=NULL,"
                " last_error=coalesce(last_error,'The drafting agent could not finish this turn.')"
                " WHERE status IN ('queued','running') AND attempts>=max_attempts"
                " AND (status='queued' OR lease_expires_at IS NULL OR lease_expires_at<=now())"
                " RETURNING project_id"
                ") UPDATE app_drafting_projects p SET status=CASE WHEN latest_version_no>0"
                " THEN 'ready' ELSE 'active' END,updated_at=now()"
                " WHERE p.id IN (SELECT project_id FROM spent)")
            cur.execute(
                "SELECT t.* FROM app_draft_turns t "
                "JOIN app_drafting_projects p ON p.id=t.project_id "
                "WHERE t.attempts<t.max_attempts AND p.status<>'archived' "
                "AND ((t.status='queued' AND t.next_attempt_at<=now()) "
                "  OR (t.status='running' AND t.lease_expires_at<=now())) "
                "ORDER BY t.next_attempt_at,t.id FOR UPDATE OF t,p SKIP LOCKED LIMIT 1")
            row = cur.fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(36)
            cur.execute(
                "UPDATE app_draft_turns SET status='running',stage='preparing',"
                "attempts=attempts+1,claimed_by=%s,lease_token_hash=%s,"
                "lease_expires_at=now()+(%s * interval '1 second'),"
                "started_at=coalesce(started_at,now()),last_error=NULL,updated_at=now() "
                "WHERE id=%s RETURNING *",
                (str(worker_id)[:180], hashlib.sha256(token.encode()).hexdigest(),
                 int(lease_seconds), row["id"]))
            claimed = self._turn(dict(cur.fetchone()))
            claimed["lease_token"] = token
            cur.execute("UPDATE app_drafting_projects SET status='generating',updated_at=now() "
                        "WHERE id=%s", (row["project_id"],))
            cur.execute("SELECT * FROM app_drafting_projects WHERE id=%s", (row["project_id"],))
            claimed["project"] = dict(cur.fetchone())
            return claimed

    def _verify(self, cur: Any, turn_id: int, lease_token: str) -> dict[str, Any]:
        cur.execute("SELECT * FROM app_draft_turns WHERE id=%s FOR UPDATE", (int(turn_id),))
        row = cur.fetchone()
        if not row:
            raise drafting.DraftingNotFound("Drafting turn was not found.")
        expected = str(row.get("lease_token_hash") or "")
        supplied = hashlib.sha256(str(lease_token or "").encode()).hexdigest()
        if row.get("status") != "running" or not expected or not hmac.compare_digest(
                supplied, expected):
            raise drafting.DraftingConflict("This worker no longer owns the drafting turn.")
        return dict(row)

    def heartbeat(self, turn_id: int, lease_token: str, *, stage: str = "",
                  lease_seconds: int = LEASE_SECONDS) -> None:
        self._ready()
        with self._cursor() as cur:
            self._verify(cur, turn_id, lease_token)
            if stage:
                cur.execute("UPDATE app_draft_turns SET stage=%s,"
                            "lease_expires_at=now()+(%s * interval '1 second'),updated_at=now() "
                            "WHERE id=%s", (str(stage)[:60], int(lease_seconds), int(turn_id)))
            else:
                cur.execute("UPDATE app_draft_turns SET lease_expires_at=now()+"
                            "(%s * interval '1 second'),updated_at=now() WHERE id=%s",
                            (int(lease_seconds), int(turn_id)))

    def save_version(self, turn_id: int, lease_token: str, *, sections: Mapping[str, str],
                     citations: Sequence[str], change_note: str, model_name: str,
                     numerals: Sequence[Mapping[str, Any]] = (),
                     figures: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """Publish an immutable version produced by this turn."""
        self._ready()
        with self._cursor() as cur:
            turn = self._verify(cur, turn_id, lease_token)
            cur.execute("SELECT * FROM app_drafting_projects WHERE id=%s FOR UPDATE",
                        (turn["project_id"],))
            project = dict(cur.fetchone())
            version_no = int(project["latest_version_no"]) + 1
            cur.execute(
                "INSERT INTO app_draft_versions (project_id,version_no,base_version_no,"
                "project_revision,sections,markdown,citations,model_name,created_by_user_id,"
                "turn_id,change_note,numerals,figure_specs) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s::jsonb) "
                "RETURNING *",
                (project["id"], version_no,
                 int(project["latest_version_no"]) or None, project["revision"],
                 _dumps(dict(sections)), render_markdown(sections), _dumps(list(citations)),
                 str(model_name)[:180], turn["requested_by_user_id"], turn["id"],
                 str(change_note or "")[:4000], _dumps([dict(n) for n in numerals]),
                 _dumps([dict(f) for f in figures])))
            version = dict(cur.fetchone())
            version["sections"] = _json(version.get("sections"), {})
            version["citations"] = _json(version.get("citations"), [])
            version["numerals"] = _json(version.get("numerals"), [])
            version["figure_specs"] = _json(version.get("figure_specs"), [])
            adopted = project_title_from(version_no, sections)
            if adopted:
                cur.execute("UPDATE app_drafting_projects SET latest_version_no=%s,title=%s,"
                            "updated_at=now() WHERE id=%s", (version_no, adopted, project["id"]))
            else:
                cur.execute("UPDATE app_drafting_projects SET latest_version_no=%s,"
                            "updated_at=now() WHERE id=%s", (version_no, project["id"]))
            cur.execute("UPDATE app_draft_turns SET version_no=%s,updated_at=now() WHERE id=%s",
                        (version_no, turn["id"]))
            # The text version and acceptance of its checked drawing set are one transaction.
            # A worker crash can therefore never publish one while rolling the other back.
            cur.execute("UPDATE app_draft_figure_turn_checkpoints SET accepted_at=now() "
                        "WHERE turn_id=%s", (turn["id"],))
            return version

    def complete_turn(self, turn_id: int, lease_token: str, *, result: Mapping[str, Any],
                      session_id: str, cost_usd: float, duration_ms: int, model_name: str,
                      transcript_path: str = "", discard_candidates: bool = True
                      ) -> dict[str, Any]:
        self._ready()
        with self._cursor() as cur:
            turn = self._verify(cur, turn_id, lease_token)
            cur.execute(
                "UPDATE app_draft_turns SET status='complete',stage='complete',"
                "completed_at=now(),updated_at=now(),lease_token_hash=NULL,lease_expires_at=NULL,"
                "agent_session_id=%s,summary=%s,reasoning=%s::jsonb,changes=%s::jsonb,"
                "questions=%s::jsonb,prior_art_strategy=%s,answer=%s,cost_usd=%s,duration_ms=%s,"
                "model_name=%s,transcript_path=%s WHERE id=%s RETURNING *",
                (str(session_id)[:80], str(result.get("summary") or "")[:8000],
                 _dumps(draft_agent.strings(result.get("reasoning"))),
                 _dumps(draft_agent.strings(result.get("changes"))),
                 _dumps(draft_agent.strings(result.get("questions"), limit=12)),
                 str(result.get("prior_art_strategy") or "")[:8000],
                 str(result.get("answer") or "")[:MAX_MESSAGE_CHARS],
                 round(float(cost_usd or 0), 4), int(duration_ms or 0), str(model_name)[:180],
                 str(transcript_path)[:500], turn["id"]))
            out = self._turn(dict(cur.fetchone()))
            if discard_candidates:
                cur.execute(
                    "DELETE FROM app_draft_turn_candidates c USING app_draft_turns t "
                    "WHERE c.turn_id=t.id AND t.project_id=%s", (int(turn["project_id"]),))
            cur.execute(
                "UPDATE app_drafting_projects SET status='ready',agent_session_id=%s,"
                "agent_turn_no=%s,updated_at=now() WHERE id=%s",
                (str(session_id)[:80], turn["turn_no"], turn["project_id"]))
            return out

    def fail_turn(self, turn_id: int, lease_token: str, error: str, *,
                  retryable: bool = True) -> dict[str, Any]:
        self._ready()
        with self._cursor() as cur:
            turn = self._verify(cur, turn_id, lease_token)
            will_retry = retryable and int(turn["attempts"]) < int(turn["max_attempts"])
            if will_retry:
                delay = min(300, 20 * (2 ** max(0, int(turn["attempts"]) - 1)))
                cur.execute(
                    "UPDATE app_draft_turns SET status='queued',stage='waiting to retry',"
                    "next_attempt_at=now()+%s,lease_token_hash=NULL,lease_expires_at=NULL,"
                    "last_error=%s,updated_at=now() WHERE id=%s RETURNING *",
                    (timedelta(seconds=delay), str(error)[:4000], turn["id"]))
            else:
                cur.execute(
                    "UPDATE app_draft_turns SET status='failed',stage='failed',"
                    "completed_at=now(),lease_token_hash=NULL,lease_expires_at=NULL,"
                    "last_error=%s,updated_at=now() WHERE id=%s RETURNING *",
                    (str(error)[:4000], turn["id"]))
            out = self._turn(dict(cur.fetchone()))
            cur.execute(
                "UPDATE app_drafting_projects SET status=CASE WHEN %s THEN 'queued' "
                "WHEN latest_version_no>0 THEN 'ready' ELSE 'active' END,updated_at=now() "
                "WHERE id=%s", (will_retry, turn["project_id"]))
            return out

    # -- QA -------------------------------------------------------------------------------------
    def save_qa(self, project_id: int, *, turn_id: int | None, version_no: int | None,
                report: Mapping[str, Any]) -> dict[str, Any]:
        self._ready()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO app_draft_qa_reports (project_id,turn_id,version_no,status,verdict,"
                "summary,checks,findings,counts,cost_usd,duration_ms,model_name,last_error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s) RETURNING *",
                (int(project_id), turn_id, version_no,
                 str(report.get("status") or "complete"), str(report.get("verdict") or "unknown"),
                 str(report.get("summary") or "")[:8000], _dumps(report.get("checks") or []),
                 _dumps(report.get("findings") or []), _dumps(report.get("counts") or {}),
                 round(float(report.get("cost_usd") or 0), 4),
                 int(report.get("duration_ms") or 0), str(report.get("model_name") or "")[:180],
                 str(report.get("last_error") or "")[:4000]))
            return self._qa(dict(cur.fetchone()))

    def qa_reports(self, project_id: int, *, limit: int = 60) -> list[dict[str, Any]]:
        self._ready()
        with self._cursor() as cur:
            cur.execute("SELECT * FROM app_draft_qa_reports WHERE project_id=%s "
                        "ORDER BY id DESC LIMIT %s", (int(project_id), int(limit)))
            return [self._qa(dict(row)) for row in cur.fetchall()]

    def latest_qa(self, project_id: int) -> dict[str, Any] | None:
        rows = self.qa_reports(project_id, limit=1)
        return rows[0] if rows else None

    @staticmethod
    def _qa(row: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out["checks"] = _json(out.get("checks"), [])
        out["findings"] = _json(out.get("findings"), [])
        out["counts"] = _json(out.get("counts"), {})
        out["cost_usd"] = float(out.get("cost_usd") or 0)
        return out


# =============================================================================================
# Running one turn
# =============================================================================================
class TurnRunner:
    """Execute one turn and publish only after every text, drawing, and review gate passes."""

    def __init__(self, repository: StudioRepository, drafting_repository:
                 drafting.DraftingRepository, *, agent=draft_agent, qa=draft_qa,
                 workspace=draft_workspace):
        self.repository = repository
        self.drafting = drafting_repository
        self.agent = agent
        self.qa = qa
        self.workspace = workspace

    # -- inputs ---------------------------------------------------------------------------------
    def _load(self, project_id: int) -> dict[str, Any]:
        with self.drafting._cursor() as cur:
            cur.execute("SELECT * FROM app_drafting_projects WHERE id=%s", (int(project_id),))
            project = dict(cur.fetchone())
            cur.execute("SELECT * FROM app_drafting_references WHERE project_id=%s "
                        "ORDER BY report_rank,publication_number", (int(project_id),))
            references = []
            for row in cur.fetchall():
                item = dict(row)
                item["snapshot"] = _json(item.get("snapshot"), {})
                references.append(item)
            cur.execute("SELECT sections,numerals,figure_specs FROM app_draft_versions "
                        "WHERE project_id=%s ORDER BY version_no DESC LIMIT 1", (int(project_id),))
            row = cur.fetchone()
            sections = _json(row["sections"], {}) if row else None
            numerals = _json(row["numerals"], []) if row else []
            figures = _json(row["figure_specs"], []) if row else []
        return {"project": project, "references": references, "sections": sections,
                "numerals": numerals, "figures": figures}

    def prepare(self, turn: Mapping[str, Any]) -> dict[str, Any]:
        project_id = int(turn["project_id"])
        loaded = self._load(project_id)
        project = loaded["project"]
        documents = self.repository.documents(project_id)
        history = [m for m in self.repository.messages(project_id, limit=60)
                   if m["role"] in ("user", "agent")][-24:]
        latest_qa = self.repository.latest_qa(project_id)
        sections = loaded["sections"]
        seeded = False
        retry_snapshot = None

        candidate = None
        if int(turn.get("attempts") or 0) > 1:
            candidate = self.repository.retry_candidate(int(turn["id"]))
        if not candidate:
            candidate = self.repository.latest_retry_candidate(
                project_id, before_turn_id=int(turn["id"]))
        if candidate:
            try:
                retry_snapshot = validate_snapshot(
                    candidate.get("snapshot") or {},
                    allowed_reference_keys(loaded["references"], documents))
            except drafting.DraftingError as exc:
                retry_snapshot = candidate_snapshot_for_repair(
                    candidate.get("snapshot") or {})
                if retry_snapshot is None:
                    self.repository.discard_retry_candidate(
                        int(candidate.get("turn_id") or turn["id"]))
                else:
                    sections = retry_snapshot["sections"]
                    latest_qa = candidate_preflight_report(
                        candidate.get("qa_report") or {}, exc)
            else:
                sections = retry_snapshot["sections"]
                latest_qa = candidate.get("qa_report") or latest_qa

        if sections is None and retry_snapshot is None:
            # First turn.  If the user brought a draft, pre-split it so the agent improves a
            # document rather than facing nine empty files and rewriting from the summary.
            source = next((d for d in documents if d["kind"] == "source_draft"), None)
            raw = (source or {}).get("body") or (
                project.get("disclosure_text") if project.get("input_kind") == "existing_draft"
                else "")
            if raw:
                seeded_sections = self.workspace.seed_sections_from_document(raw)
                if seeded_sections:
                    sections = seeded_sections
                    seeded = True

        numerals = (retry_snapshot["numerals"] if retry_snapshot else
                    self.workspace.numerals_from_sections(sections) if seeded else
                    loaded["numerals"])
        figures = retry_snapshot["figures"] if retry_snapshot else loaded["figures"]
        workspace = self.workspace.build(
            project=project, references=loaded["references"], documents=documents,
            sections=sections, numerals=numerals, figures=figures,
            conversation=history, request=turn.get("user_message") or "",
            qa_report=latest_qa)
        return {"workspace": workspace, "project": project, "references": loaded["references"],
                "documents": documents, "seeded": seeded, "had_version": loaded["sections"] is not None,
                "resuming_candidate": retry_snapshot is not None,
                "resuming_candidate_turn_id": (int(candidate.get("turn_id") or turn["id"])
                                                 if retry_snapshot is not None and candidate
                                                 else None),
                "prepared_snapshot": {"sections": sections or {}, "numerals": numerals,
                                      "figures": figures},
                "prepared_qa": latest_qa or {},
                "previous_sections": loaded["sections"] or {}}

    # -- the turn --------------------------------------------------------------------------------
    def _run_agent(self, *, turn_id: int, lease: str, workspace: Path, prompt: str,
                   session_id: str, resume: bool, transcript: Path,
                   stage: str) -> draft_agent.AgentRun:
        self.repository.heartbeat(turn_id, lease, stage=stage)
        beat = _Heartbeat(self.repository, turn_id, lease, stage)
        beat.start()
        try:
            run = self.agent.run(
                workspace=workspace, prompt=prompt, system_prompt=DRAFT_SYSTEM,
                schema=TURN_SCHEMA, session_id=session_id, resume=resume,
                model=self.agent.DRAFT_MODEL, timeout=self.agent.DRAFT_TIMEOUT,
                transcript=transcript, cancel=beat.cancelled)
        finally:
            beat.stop()
        if not run.ok:
            raise (drafting.DraftingConflict("Stopped at your request.") if run.cancelled
                   else StudioError(run.error or "The drafting agent did not finish."))
        return run

    def _checkpoint_interrupted_agent(self, *, turn_id: int, lease: str, workspace: Path,
                                      allowed: Sequence[str], error: Exception) -> None:
        """Keep structurally valid edits when an agent stops before its structured answer."""
        try:
            snapshot = validate_snapshot(self.workspace.snapshot(workspace), allowed)
        except Exception:
            return
        detail = str(error)[:1200]
        check = {
            "name": "Drafting run completed",
            "status": "fail",
            "severity": "error",
            "detail": detail,
            "items": ["Continue from this saved candidate and finish every remaining section."],
        }
        report = {
            "status": "failed",
            "verdict": "fail",
            "summary": "The drafting run stopped after saving valid edits. Continue from this "
                       "candidate instead of rebuilding the published version.",
            "checks": [check],
            "findings": [],
            "counts": draft_qa.counts_for([check], []),
            "cost_usd": 0.0,
            "duration_ms": 0,
            "model_name": "",
            "last_error": detail,
        }
        self.repository.save_retry_candidate(
            turn_id, lease, snapshot=snapshot, report=report)
        try:
            self.workspace._write_review(workspace, report)
        except Exception:
            pass

    def _ensure_figures(self, *, turn_id: int, lease: str, project_id: int, user_id: int,
                        sections: Mapping[str, str], numerals: Sequence[Mapping[str, str]],
                        figures: Sequence[Mapping[str, Any]], disclosure: str,
                        workspace: Path) -> dict[str, Any]:
        import draft_figures
        draft_figures.checkpoint_project_figures(turn_id, project_id, user_id)

        def check_cancel() -> None:
            self.repository.heartbeat(
                turn_id, lease, stage="drawing and inspecting figures")

        result = draft_figures.ensure_project_figures(
            project_id, user_id, sections=sections, disclosure=disclosure,
            numeral_table=numerals, figure_specs=figures, check_cancel=check_cancel)
        result["review_images"] = draft_figures.materialize_review_images(
            project_id, user_id, workspace)
        return result

    @staticmethod
    def restore_figures(turn_id: int) -> bool:
        import draft_figures
        return draft_figures.restore_project_figure_checkpoint(turn_id)

    def run(self, turn: Mapping[str, Any]) -> dict[str, Any]:
        turn_id, lease = int(turn["id"]), turn["lease_token"]
        project_id = int(turn["project_id"])
        context = self.prepare(turn)
        workspace: Path = context["workspace"]
        project = context["project"]
        allowed = allowed_reference_keys(context["references"], context["documents"])

        kind = str(turn.get("kind") or "revise")
        first = not context["had_version"] and not context.get("resuming_candidate")
        prompt_kind = "initial" if first else ("revise" if kind == "initial" else kind)
        prompt = build_prompt(prompt_kind, seeded=context["seeded"])
        transcript = workspace / ".agent" / f"turn-{turn['turn_no']:04d}.jsonl"

        #  Whether to RESUME and which prompt to send are separate decisions, and conflating them
        #  is an outage: `--session-id` on an id that already exists is an error, so a first turn
        #  that answered a question without producing a version would make the next turn pass an
        #  existing id as if it were new. Continue the thread whenever there is one.
        prior_session = str(project.get("agent_session_id") or "")
        run = _gate_resume_run(context, turn)
        if run is None:
            try:
                run = self._run_agent(
                    turn_id=turn_id, lease=lease, workspace=workspace, prompt=prompt,
                    session_id=prior_session or self.agent.new_session_id(),
                    resume=bool(prior_session), transcript=transcript, stage="drafting")
            except StudioError as exc:
                self._checkpoint_interrupted_agent(
                    turn_id=turn_id, lease=lease, workspace=workspace, allowed=allowed, error=exc)
                raise
        else:
            self.repository.heartbeat(
                turn_id, lease, stage="resuming automatic filing checks")
        runs = [run]
        result = human_text(dict(run.result))
        action = str(result.get("action") or "revised")
        if context.get("resuming_candidate"):
            source_lock = restore_text_after_drawing_only_review(
                workspace, context.get("prepared_snapshot") or {},
                context.get("prepared_qa") or {})
            if not source_lock:
                source_lock = restore_sources_after_figure_plan_review(
                    workspace, context.get("prepared_snapshot") or {},
                    context.get("prepared_qa") or {})
            if source_lock:
                changes = list(result.get("changes") or [])
                changes.append(
                    "Preserved the checked filing sources while applying the requested "
                    "drawing repair.")
                result["changes"] = changes

        #  Only an explicit question on an existing application may complete without a filing
        #  candidate. An initial or revision turn that answers instead of drafting is fed back as
        #  a gate failure and automatically resumed.
        candidate_required = bool(first or kind != "question")
        if action == "answered" and not candidate_required:
            self.repository.add_message(
                project_id, "agent", str(result.get("answer") or "")[:MAX_MESSAGE_CHARS],
                turn_id=turn_id, payload={"action": action, "summary": result.get("summary"),
                                          "version_no": None, "cost_usd": run.cost_usd,
                                          "steps": run.steps[-80:]})
            completed = self.repository.complete_turn(
                turn_id, lease, result=result, session_id=run.session_id,
                cost_usd=run.cost_usd, duration_ms=run.duration_ms, model_name=run.model,
                transcript_path=str(transcript), discard_candidates=False)
            return {"turn": completed, "version": None}
        answered_without_candidate = action == "answered"

        report: dict[str, Any] | None = None
        snapshot: dict[str, Any] = {}
        sections: dict[str, str] = {}
        for review_index in range(MAX_FINALIZATION_ROUNDS):
            if review_index:
                prior_snapshot, prior_report = snapshot, report or {}
                try:
                    repair = self._run_agent(
                        turn_id=turn_id, lease=lease, workspace=workspace,
                        prompt=FINALIZE_PROMPT, session_id=runs[-1].session_id,
                        resume=True, transcript=transcript, stage="repairing the draft")
                except StudioError as exc:
                    self._checkpoint_interrupted_agent(
                        turn_id=turn_id, lease=lease, workspace=workspace,
                        allowed=allowed, error=exc)
                    raise
                runs.append(repair)
                result = human_text(dict(repair.result))
                action = "revised"
                source_lock = restore_text_after_drawing_only_review(
                    workspace, prior_snapshot, prior_report)
                if not source_lock:
                    source_lock = restore_sources_after_figure_plan_review(
                        workspace, prior_snapshot, prior_report)
                if source_lock:
                    changes = list(result.get("changes") or [])
                    changes.append(
                        "Preserved the checked filing sources while applying the requested "
                        "drawing repair.")
                    result["changes"] = changes

            self.repository.heartbeat(turn_id, lease, stage="checking the draft")
            if answered_without_candidate and review_index == 0:
                check = {"name": "Complete filing candidate", "status": "fail",
                         "severity": "error",
                         "detail": "The drafting turn answered without producing an application.",
                         "items": ["Produce the complete filing candidate without asking a question."]}
                report = {
                    "status": "failed", "verdict": "fail",
                    "summary": "The agent did not produce the required filing candidate.",
                    "checks": [check], "findings": [],
                    "counts": draft_qa.counts_for([check], []), "cost_usd": 0.0,
                    "duration_ms": 0, "model_name": "", "last_error": check["detail"],
                }
            else:
                try:
                    raw_snapshot = self.workspace.snapshot(workspace)
                    # A new deterministic rule may reject a structurally complete candidate.
                    # Retain that full candidate before validation so the following repair round
                    # has an authoritative source-lock baseline. Assigning only after validation
                    # leaves ``snapshot`` empty and lets a figure-only repair erase every filing
                    # section when the lock restores the empty value.
                    repair_snapshot = candidate_snapshot_for_repair(raw_snapshot)
                    if repair_snapshot is not None:
                        snapshot = repair_snapshot
                    snapshot = validate_snapshot(raw_snapshot, allowed)
                    sections = snapshot["sections"]
                    self.repository.save_retry_candidate(
                        turn_id, lease, snapshot=snapshot,
                        report=_gate_resume_report(runs, result))
                    self.repository.heartbeat(turn_id, lease, stage="drawing and inspecting figures")
                    generated = self._ensure_figures(
                        turn_id=turn_id, lease=lease, project_id=project_id,
                        user_id=int(project["user_id"]), sections=sections,
                        numerals=snapshot["numerals"], figures=snapshot["figures"],
                        disclosure=str(project.get("disclosure_text") or ""), workspace=workspace)
                    if not generated.get("ok"):
                        failures = [str(item) for item in generated.get("errors") or ()]
                        raise DrawingInspectionError(failures or [
                            "One or more sheets did not pass geometry, leader, and OCR inspection."])
                    self.repository.heartbeat(turn_id, lease, stage="independent review")
                    report = self.evaluate(
                        project_id, version_no=int(project.get("latest_version_no") or 0) + 1,
                        workspace=workspace, allowed=allowed, sections=sections,
                        numerals=snapshot["numerals"], figures=snapshot["figures"],
                        review_index=review_index)
                except DrawingInspectionError as exc:
                    check = {
                        "name": "Every drawing sheet passes geometry, leader, and OCR inspection",
                        "status": "fail", "severity": "error",
                        "category": "figures_and_numerals",
                        "detail": (f"{len(exc.errors)} sheet(s) failed. Each failure is listed "
                                   "below so the next repair can address the full set."),
                        "items": exc.errors,
                    }
                    report = {
                        "status": "failed", "verdict": "fail",
                        "summary": f"{len(exc.errors)} drawing sheet(s) require automatic repair.",
                        "checks": [check], "findings": [],
                        "counts": draft_qa.counts_for([check], []), "cost_usd": 0.0,
                        "duration_ms": 0, "model_name": "", "last_error": str(exc),
                    }
                except drafting.DraftingConflict:
                    raise
                except Exception as exc:                         # a failed gate becomes repair input
                    traceback.print_exc()
                    check = {"name": "Automatic filing candidate checks",
                             "status": "fail", "severity": "error",
                             "category": str(getattr(exc, "category", "internal_logic"))[:40],
                             "detail": str(exc)[:1200], "items": [str(exc)[:600]]}
                    report = {
                        "status": "failed", "verdict": "fail",
                        "summary": "The automatic filing gate did not pass: " + str(exc)[:1000],
                        "checks": [check], "findings": [],
                        "counts": draft_qa.counts_for([check], []), "cost_usd": 0.0,
                        "duration_ms": 0, "model_name": "", "last_error": str(exc)[:1200],
                    }

            blockers = filing_blockers(report)
            if not blockers:
                break
            if snapshot.get("sections"):
                self.repository.save_retry_candidate(
                    turn_id, lease, snapshot=snapshot, report=report)
            self.workspace._write_review(workspace, report)
            if review_index + 1 >= MAX_FINALIZATION_ROUNDS:
                raise drafting.DraftingValidationError(
                    "The automatic filing gate could not clear: " + "; ".join(blockers[:8]))

        version = None
        final_run = runs[-1]
        if sections != context["previous_sections"]:
            version = self.repository.save_version(
                turn_id, lease, sections=sections, citations=citations_of(sections),
                change_note=str(result.get("summary") or "")[:4000],
                model_name=final_run.model, numerals=snapshot["numerals"],
                figures=snapshot["figures"])
        else:
            # The accepted turn may have repaired only the pixels for an unchanged specification.
            import draft_figures
            draft_figures.commit_project_figure_checkpoint(turn_id)
        version_no = int((version or {}).get("version_no") or
                         project.get("latest_version_no") or 0) or None

        total_cost = sum(item.cost_usd for item in runs)
        total_duration = sum(item.duration_ms for item in runs)
        steps = human_text([step for item in runs for step in item.steps][-80:])
        self.repository.add_message(
            project_id, "agent",
            str(result.get("answer") or result.get("summary") or "")[:MAX_MESSAGE_CHARS],
            turn_id=turn_id,
            payload={"action": action, "summary": result.get("summary"),
                     "reasoning": self.agent.strings(result.get("reasoning")),
                     "changes": self.agent.strings(result.get("changes")),
                     "questions": [], "prior_art_strategy": result.get("prior_art_strategy"),
                     "version_no": version_no, "cost_usd": total_cost, "steps": steps})
        self._publish_review(
            project_id, turn_id=turn_id, version_no=version_no,
            workspace=workspace, report=report or {})

        completed = self.repository.complete_turn(
            turn_id, lease, result=result, session_id=final_run.session_id, cost_usd=total_cost,
            duration_ms=total_duration, model_name=final_run.model,
            transcript_path=str(transcript))
        try:
            import draft_figures
            draft_figures.discard_project_figure_checkpoint(turn_id)
        except Exception:
            traceback.print_exc()
        return {"turn": completed, "version": version}

    # -- review -----------------------------------------------------------------------------------
    def evaluate(self, project_id: int, *, version_no: int | None, workspace: Path,
                 allowed: Sequence[str], sections: Mapping[str, str],
                 numerals: Sequence[Mapping[str, str]], figures: Sequence[Mapping[str, Any]],
                 review_index: int = 0) -> dict[str, Any]:
        """Evaluate a workspace without publishing either the version or the report."""
        started = time.time()
        qa_figures = list(figures)
        try:
            loaded = self._load(project_id)
            qa_figures = figures_for_qa(
                project_id, int(loaded["project"]["user_id"]), figures)
        except Exception:
            pass
        try:
            checks = self.qa.run_checks(sections=sections, numerals=numerals, figures=qa_figures,
                                        allowed_references=allowed)
        except Exception as exc:                                # noqa: BLE001
            traceback.print_exc()
            checks = [{"name": "Mechanical checks", "status": "fail", "severity": "warn",
                       "detail": f"The checks could not run ({type(exc).__name__}).", "items": []}]
        transcript = workspace / ".agent" / (
            f"review-{version_no or 0:04d}-{int(review_index) + 1:02d}.jsonl")
        outcome = self.qa.review(workspace, checks=checks, transcript=transcript)
        findings = outcome.get("findings") or []
        verdict = self.qa.verdict_for(checks, findings)
        report = {
            "status": "complete" if outcome.get("ok") else "failed",
            "verdict": verdict,
            "summary": (outcome.get("summary") or "").strip() or
                       self.qa.summarize(checks, findings, verdict),
            "checks": checks, "findings": findings,
            "counts": self.qa.counts_for(checks, findings),
            "cost_usd": outcome.get("cost_usd") or 0.0,
            "duration_ms": int((time.time() - started) * 1000),
            "model_name": outcome.get("model") or "",
            "last_error": outcome.get("error") or "",
        }
        return human_text(report)

    def _publish_review(self, project_id: int, *, turn_id: int | None,
                        version_no: int | None, workspace: Path,
                        report: Mapping[str, Any]) -> dict[str, Any]:
        saved = self.repository.save_qa(
            project_id, turn_id=turn_id, version_no=version_no, report=report)
        self.repository.add_message(
            project_id, "qa", str(report.get("summary") or ""), turn_id=turn_id,
            payload={"verdict": report.get("verdict"), "counts": report.get("counts") or {},
                     "qa_id": saved.get("id"), "version_no": version_no,
                     "failed": draft_qa.failed_check_names(report.get("checks") or [])})
        try:
            self.workspace._write_review(workspace, saved)
        except Exception:                                       # noqa: BLE001 - cosmetic only
            pass
        return saved

    def review(self, project_id: int, *, turn_id: int | None, version_no: int | None,
               workspace: Path, allowed: Sequence[str], sections: Mapping[str, str],
               numerals: Sequence[Mapping[str, str]],
               figures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Run and persist an explicit review requested outside the drafting loop."""
        report = self.evaluate(
            project_id, version_no=version_no, workspace=workspace, allowed=allowed,
            sections=sections, numerals=numerals, figures=figures)
        return self._publish_review(
            project_id, turn_id=turn_id, version_no=version_no,
            workspace=workspace, report=report)


class _Heartbeat:
    """Renew the lease while a run that can take fifteen minutes is in flight.

    It is also how Stop reaches the model. The lease is the authority: when the user cancels, the
    turn row stops being `running` and the next renewal is refused - which is exactly the moment
    to kill the subprocess. Without this the button would mark the turn cancelled while the agent
    kept working, kept holding a core, and kept spending.
    """

    def __init__(self, repository: StudioRepository, turn_id: int, lease: str, stage: str):
        self._repository, self._turn_id, self._lease, self._stage = repository, turn_id, lease, stage
        self._stop = threading.Event()
        self.cancelled = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"draft-turn-{self._turn_id}",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(20):
            try:
                self._repository.heartbeat(self._turn_id, self._lease, stage=self._stage)
            except drafting.DraftingError:
                self.cancelled.set()                    # cancelled or taken over, on purpose
                return
            except Exception:                           # noqa: BLE001 - a DB blip is not fatal
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
