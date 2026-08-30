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
import draft_settings
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
#  How long the explicit drawing pass may run before it reports what it did not reach. Unbounded,
#  it re-inspects every sheet whenever an audit constant moves, which measured 53 minutes.
DEFAULT_DRAWING_BUDGET_SECONDS = 3600
DRAWING_BUDGET_SECONDS = max(
    60, int(os.environ.get("DRAFT_DRAWING_SECONDS", str(DEFAULT_DRAWING_BUDGET_SECONDS))))
_GATE_RESUME_KEY = "_gate_resume"
_AUTOMATIC_GATE_RESUME_TURN_KEY = re.compile(r"^auto-filing-repair-\d+-\d+$")
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_MIGRATION = _SQL_DIR / "006_draft_agent.sql"
#  018 adds the chosen model, the turn's section scope, and where a version came from. It is
#  applied here as well as through migrate.py because every statement in it is replayable and the
#  worker must never start against a database that is missing a column it writes on its first turn.
_MIGRATIONS = (_MIGRATION, _SQL_DIR / "018_draft_studio_editing.sql",
               _SQL_DIR / "019_draft_turn_kinds.sql",
               _SQL_DIR / "020_draft_research_rounds.sql",
               _SQL_DIR / "021_draft_project_settings.sql",
               _SQL_DIR / "022_draft_turn_spend.sql",
               _SQL_DIR / "023_draft_source_review_cache.sql")


class StudioError(drafting.DraftingError):
    pass


class SourceFidelityInspectionError(StudioError):
    """A completed pre-render review that found source or text blockers."""

    def __init__(self, report: Mapping[str, Any]):
        self.report = human_text(dict(report))
        findings = self.report.get("findings") or []
        detail = str(self.report.get("last_error") or self.report.get("summary") or "")
        if findings:
            detail = f"{len(findings)} source-fidelity finding(s) must be repaired."
        super().__init__(detail or "The source-fidelity preflight did not pass.")


class SourceReviewUnavailable(StudioError):
    """The independent reviewer failed, so retry the saved candidate unchanged."""

    retry_without_repair = True


class TurnBudgetSpent(StudioError):
    """One turn reached the ceiling on what it may spend, and stopped.

    Not retryable. A turn that has already spent its budget will spend it again on the next
    attempt, so retrying is the one response guaranteed to make it worse.
    """

    retry_without_repair = False


class DrawingBudgetSpent(StudioError):
    """A bounded caller stopped drawing work, so retry the same saved candidate unchanged."""

    retry_without_repair = True


def _drawing_issue_count(count: int) -> str:
    return f"{count} drawing {'issue' if count == 1 else 'issues'}"


class DrawingInspectionError(StudioError):
    def __init__(self, errors: Sequence[str]):
        self.errors = [str(item)[:2000] for item in errors if str(item).strip()]
        super().__init__(
            f"{_drawing_issue_count(len(self.errors))} did not pass inspection.")


class FigurePlanInspectionError(DrawingInspectionError):
    """A drawing-source defect that may change sheet membership and numeral distribution."""


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
    """Restore an agent result for its interrupted turn or automatic continuation."""
    candidate_turn_id = int(context.get("resuming_candidate_turn_id") or 0)
    current_turn_id = int(turn["id"])
    same_turn = candidate_turn_id == current_turn_id
    automatic_continuation = bool(
        0 < candidate_turn_id < current_turn_id and
        str(turn.get("kind") or "") == "gate_resume")
    if not same_turn and not automatic_continuation:
        return None
    prepared = context.get("prepared_qa")
    marker = prepared.get(_GATE_RESUME_KEY) if isinstance(prepared, Mapping) else None
    if not isinstance(marker, Mapping) or not isinstance(marker.get("result"), Mapping):
        if str(turn.get("kind") or "") != "gate_resume":
            return None
        project = context.get("project")
        session_id = (str(project.get("agent_session_id") or "")
                      if isinstance(project, Mapping) else "")
        return draft_agent.AgentRun(
            ok=True,
            result={
                "action": "revised",
                "summary": "Restored the saved filing candidate for automatic review.",
                "reasoning": [],
                "changes": [],
                "questions": [],
                "prior_art_strategy": "",
                "answer": "",
            },
            session_id=session_id,
            model="saved-candidate",
            cost_usd=0.0,
            duration_ms=0,
            num_turns=0,
            steps=[],
        )
    session_id = str(marker.get("session_id") or "")
    # A gate continuation may itself have restored a legacy checkpoint without a provider
    # session. Its complete candidate remains the authority on the next bounded continuation;
    # sending it through a new drafting run only spends budget and risks unrelated rewrites.
    if not session_id and str(turn.get("kind") or "") != "gate_resume":
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


def _candidate_differs_from_published(context: Mapping[str, Any],
                                      snapshot: Mapping[str, Any]) -> bool:
    """Compare a resumed candidate with the published version, not its restored copy."""
    published = context.get("published_snapshot")
    if isinstance(published, Mapping):
        return any(
            snapshot.get(key) != published.get(key)
            for key in ("sections", "numerals", "figures")
        )
    prepared = context.get("prepared_snapshot")
    prepared = prepared if isinstance(prepared, Mapping) else {}
    return bool(
        snapshot.get("sections") != context.get("previous_sections", {}) or
        snapshot.get("numerals") != prepared.get("numerals") or
        snapshot.get("figures") != prepared.get("figures"))


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
If no government support was supplied, write `Not applicable.` in the government-support section;
never delete the section. Operational requests to resume, preserve, repair, inspect, or audit an
existing candidate do not disclose or affirm its technical content. Nor do statements that a
candidate is source-faithful, its numeral or figure counts, its labels, or the filing gates it
should pass. A corrective message
that names a detail only to reject, remove, narrow, question, or audit it is not affirmative source
support. Require an independent USER passage that affirmatively describes the technical detail.

SOURCE COMPLETENESS IS BIDIRECTIONAL
Never silently drop affirmative technical matter from the inventor sources. The Detailed
Description must preserve every disclosed technical structure, relationship, operation,
safety or recovery behavior, installation or calibration procedure, data-recording behavior,
and alternative embodiment unless a later affirmative USER passage withdraws or replaces it.
Use supported dependent claims to cover commercially distinct embodiments and safety or recovery
modes where claim form can capture them cleanly. Do not force every optional feature into an
independent claim, and do not copy filing instructions, motivations, rejected details, or redundant
wording as though they were technical embodiments. Description-only preservation is not claim
coverage. When the claim set remains below 20 total claims, include a source-supported dependent
claim for each distinct technical safeguard against misconfiguration or failure and each distinct
commercial technical capability that is not already necessarily recited. Installation or
calibration controls, tamper-evident technical records, recovery or fallback behavior, and
serviceable technical modules are examples when the source gives them technical substance.
Treat each conditional, temporal, negative, exception, threshold, actor, and verification
relationship as an indivisible source constraint. Preserve qualifiers such as only, until, unless,
after, before, remains, corresponding, independent, and expired in substance. Never replace
sensor-confirmed agreement with human confirmation, a named sensed channel with a generic
response, or an unexpired-token condition with generic authorization.
No automatic fix may leave more than 20 total claims or more than three independent claims.
When additional source-supported coverage is needed at either limit, consolidate redundant
coverage or amend an existing claim instead of adding a claim that exceeds the limit.

FILING-CLEAN OUTPUT IS ABSOLUTE
No placeholder, drafting note, TODO, TBD, blank field, instruction to a draftsperson, question to
the inventor, or request for confirmation may appear anywhere in draft/, draft/numerals.md, or
figures/. Resolve each issue conservatively from the disclosure or omit the unsupported optional
detail. Return `questions` as an empty array. The automatic review will reject the entire turn if
one unfinished marker remains. Use commas, colons, full stops, or ordinary hyphens; never use an
em dash.

THE DRAFT IS THE ONLY DRAFT
Every file in draft/ reads as the single, finished application that is about to be filed. It is
never a revision OF something. Never write a version or draft number, a date of revision, a change
log, a note about what you altered or why, a comparison with an earlier wording, an editorial
aside, a reviewer's initials, or any sentence addressed to the reader of this conversation rather
than to the examiner. No "version 2", "revised", "as amended", "previously", "now corrected",
"see note", no bracketed commentary and no trailing summary paragraph. What you changed and why is
reported in `summary`, `changes` and `reasoning`, which are read in the studio and never filed.
The application text itself carries only the invention.

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
Every reference listed in prior_art/INDEX.md must be addressed by an accurate citation in the
Background. If a reference is peripheral, state only the supported teaching that matters and why
it does not bear on the claimed combination.
Never omit a listed reference solely because it is less relevant.

REFERENCE NUMERALS
Keep draft/numerals.md in step with the text at all times: every numeral used anywhere appears
there exactly once against one part name, and every part has one numeral. Introduce a numeral the
first time its part is named ("a suction cup 10"), and use the same words for it every time after.
Figures live in figures/, one file per drawing, listing the numerals that appear on it - a numeral
on a drawing that is not in the table, or a part described as visible in a figure whose file does
not list it, is a defect the review will find. Every application must include at least one figure.
Never leave two figure files with the same FIG. number. When renaming or replacing a figure brief,
use the figure deletion tool to delete the superseded file before returning.
Use a structural view, system diagram, or process flow as appropriate to the disclosed invention.
Normally use two to four figures. Do not list more than eight numerals on one sheet. When more
structure must be shown, add a focused detail or sectional sheet instead of overcrowding one image,
then synchronize the Brief Description of the Drawings and Detailed Description.
When moving a numeral to another or focused sheet, remove every use of that numeral and its
canonical part name from the old sheet brief. If the old sheet still needs the structure only as
context, describe it generically as an unnumbered block, slab, housing, line, or other simple
shape without its canonical part name or numeral; otherwise omit it. Never delete the focused
sheet merely to fix stale references on the old brief.
Keep each figure brief at or below 2800 characters. Include only disclosure-grounded geometry and
relationships needed to identify the listed parts. Never invent arbitrary exact counts,
proportions, relative heights, corner shapes, line counts, or placement constraints merely to
control the renderer. If a visual constraint is not in the disclosure or specification, omit it.
Do not demand open paper between solid bodies merely to create label room. State a
source-grounded physical spacing or omit it.
Always keep all line work inside the drawing area, clear of physical sheet edges and filing
margins. Describe placement against the drawing area, never against a sheet edge.
When the source discloses a part but not its appearance, the image still needs a visible outline.
Use the simplest generic outline and identify it in the figure brief as "shown schematically".
That outline is a depiction convention only. Never add its chosen shape, proportion, or page
placement to the patent text or claims, and never imply that the convention is an embodiment.
For a disclosed functional face, slot, joint, cam, ramp, seal, port, or flow boundary whose exact
geometry was not disclosed, use the least-specific schematic outline that shows only the
disclosed function. A generic short face or opening may be placed for visibility as a depiction
convention, but never state which end is deeper, radial or circumferential end positions, runout
direction, taper, precise angle, or contact topology unless the inventor source states it.
Describe a cord, cable, wire, hose, or pulling element as one curved path and target that path.
Never define it as a white-interior strip or by counting outline strokes.
Never write generic negative bans on linework such as no rim, ledge, chamfer, parallel line,
doubled line, second boundary, or internal stroke. State each required body and relationship
positively. Necessary outer edges of separate solids remain permitted where the solids meet or
overlap in a perspective or sectional view.
Do not prescribe every outline as one line or require a shared face edge to be drawn once. Those
are generic renderer controls, not invention geometry. Describe the solids, contacts, openings,
and occlusions that must be visible.
Numeral endpoint instructions identify the part, not an aesthetic coordinate. Prefer a broad
interior target such as well inside the part or its hatching. Do not require a midpoint, quarter,
exact depth, or toward-an-end placement unless that location is disclosure-grounded or necessary
to distinguish the intended part from another visible part. Name repeated shapes by a stable
position such as outermost, middle, innermost, left, or right instead of an ordinal whose direction
could be read more than one way.
When one figure is a sectional view taken on line N-N of another figure, the source-view brief
must specify a broken cutting-plane line, both physical endpoints, its alignment, and the viewing
direction of both arrows. Put the same repeated designation N at both ends so the brief expressly
says line N-N. A section designation is drawing annotation, not a reference numeral: never add it
to numerals.md or the figure's reference-numeral list.
An axial section through a hollow cylindrical part shows two opposed sectioned walls separated by
the open bore. An annulus is the appearance in a transverse section. Keep the view orientation,
the sectioned walls, and the through-bore consistent instead of changing one to excuse rendered
pixels.
A longitudinal slot's axis runs along the slot. A vertical bore axis can intersect the open slot
or lie in its center plane, but it cannot be collinear with the longitudinal slot axis. State the
actual intersection or center-plane relationship instead of saying those perpendicular axes are
aligned.
Figure files are Markdown specifications only. Never create SVG, PNG, or other image files. The
image pipeline generates unlabeled geometry, then adds the listed numerals, FIG. label, callouts,
leader lines, cutting lines, arrows, and section designations deterministically. Describe the
required geometry and relationships, and list the numerals, but never ask the geometry image to
draw text or labels itself. In every process-flow figure, give each process and decision shape a
distinct reference numeral, list those numerals in the figure file and numerals.md, and identify
the same numbered steps in the Detailed Description. Never put a verbal step name, question,
YES/NO word, equation, or other phrase inside a process box or decision diamond. Never erase a
figure's geometry brief merely to remove verbal labels. Every figure file must still describe all
visible shapes, their order and routes, and the target geometry for every listed numeral. Never
address or mention a draftsperson, drafter, illustrator, reviewer, attorney, or other person in a figure
brief. When a part name is only a semantic identifier, say that it does not appear as drawing
text.

FILES
  input/disclosure.md     the invention (read-only authority)
  input/brief.md          title, applicant, inventors, notes
  input/conversation.md   what has been said so far
  input/request.md        what the user is asking for THIS turn
  input/materials/        anything else the user uploaded
  prior_art/              the references, with INDEX.md listing the citation keys
  draft/01-title.md, draft/02-cross-reference.md, draft/03-government-support.md,
  draft/04-field.md, draft/05-background.md, draft/06-summary.md, draft/07-drawings.md,
  draft/08-detailed-description.md, draft/09-claims.md, draft/10-abstract.md
                          the only application body files, with no heading lines
  draft/numerals.md       the reference-numeral table
  figures/                one file per drawing
  review/previous-qa.md   what the reviewer found last time - fix it
  tools/patent_lookup.py  `python3 tools/patent_lookup.py US-9108319-B2` reads a publication out
                          of the local corpus of millions of patents. Use it when you need what a
                          reference ACTUALLY says, or to check a publication before citing it.

HOW TO WORK
READ WHAT THE REQUEST NEEDS, NOT THE WHOLE WORKSPACE. On a revision or repair pass, begin with
input/request.md, review/previous-qa.md, and the draft or figure files those instructions name.
Read disclosure, prior-art, or other files when the requested change or a support check requires
them. The first drafting pass still follows FIRST_TURN_PROMPT and reads its required sources.
Read the current request and review before writing. Read the draft files the work affects and the
disclosure or conversation passages needed to verify support. Then edit only what the request and
the review require. A request to narrow one claim is not licence to reword the background - an
unnecessary rewrite destroys the user's own edits and makes the change log useless.

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

READ ONLY WHAT YOU NEED. Begin with review/previous-qa.md and the draft or figure files named by
its findings. Read the inventor sources when a finding concerns source support. Do not reread the
whole workspace merely because another repair round started.

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
Replace generic negative linework controls with positive descriptions of the required bodies,
edges, contacts, openings, and paths. Do not ban second boundaries, parallel lines, inner lines,
rims, ledges, or chamfers merely to suppress a prior rendering artifact.
Remove generic face-linework controls such as prescribing one stroke for every outline or a
shared edge drawn once. Keep only the positive physical geometry and relationship.
For an endpoint finding, choose a broad interior target on the intended part whenever possible.
Remove exact midpoint, depth, quarter, or toward-an-end modifiers that are not disclosure-grounded
or necessary to distinguish the intended part. Replace ordinal geometry references with explicit
outermost, middle, innermost, left, or right terms whenever the order could be read in more than
one direction.

Leave no note, placeholder, question, or instruction for a person. Return the structured answer
with `action` set to "revised" and `questions` as an empty array."""

QUESTION_PROMPT = """The user has asked a question about this draft rather than asking for a
change. Their question is in input/request.md.

Read whatever you need in order to answer it accurately - the draft, the prior art, the
disclosure. Answer in `answer`, set `action` to "answered", and do not edit any file.

If answering honestly requires a change to the draft, say so in your answer and ask them to
confirm; do not make the change unasked."""


# =============================================================================================
# The section edit: full context in, a patch out
# =============================================================================================
#  A REVISION TURN SENDS EVERYTHING AND GETS EVERYTHING BACK, AND THAT IS THE COST.
#  Asking for one sentence in the Field of the Disclosure used to run the whole machine: the agent
#  rewrote files, every drawing was re-inspected, an independent reviewer read the application, and
#  the repair loop stood ready to do it six more times. Hours, and dollars, for a clause.
#
#  This lane keeps the input and throws away the output. The agent still reads the entire
#  application, the disclosure, the prior art and the conversation, because a clause that
#  contradicts claim 1 is worse than no clause. It is given NO write tools, so the only thing it
#  can return is a list of exact find/replace pairs inside one named section, which this process
#  applies itself. The output is a few hundred tokens instead of twenty thousand, the numeral table
#  and every drawing are carried forward untouched because nothing could have changed them, and the
#  version is published after the deterministic checks rather than after a second model reads it.
SECTION_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["revised", "answered"]},
        "summary": {"type": "string"},
        "edits": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "properties": {
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["find", "replace", "why"],
                "additionalProperties": False,
            },
        },
        "replacement": {"type": "string"},
        "consequences": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string"},
    },
    "required": ["action", "summary", "edits", "replacement", "consequences", "answer"],
    "additionalProperties": False,
}

SECTION_EDIT_SYSTEM = """You are editing one section of a US utility patent application that is
already drafted. You have read access to the whole workspace and no write access at all: your
answer IS the edit, and the application applies it.

THE SCOPE IS ABSOLUTE
You may change the text of exactly one section, named in the request. Do not propose a change to
any other section, to draft/numerals.md, or to any file in figures/. If the requested change would
make another section wrong, make the change anyway and say precisely what is now inconsistent in
`consequences`; the user decides whether to ask for that follow-up. Never widen the request.

HOW TO ANSWER
Prefer `edits`: a list of find/replace pairs. `find` must be text that appears in the target
section VERBATIM, copied character for character including its punctuation and spacing, and must
appear exactly once in that section. Keep each `find` as short as it can be while still being
unique. `replace` is what it becomes, and may be empty to delete. Return `replacement` as an empty
string when you use `edits`.
Use `replacement` instead, carrying the complete new text of the section, only when the change is
so extensive that find/replace pairs would cover most of the section. Return `edits` as an empty
array when you use `replacement`. Never return both.
If the user asked a question rather than for a change, set `action` to "answered", answer in
`answer`, and return no edits.

THE AUTHORITY ORDER IS UNCHANGED
The inventor's disclosure and what the user has said are the only authority for what the invention
is. Prior art is context to write around, never a source to borrow from. Never invent a structure,
relationship, measurement, or result. Keep the terminology, the reference numerals and the
antecedent basis exactly as the rest of the application uses them: a numeral or a part name that
appears in your replacement text must already be defined in draft/numerals.md with that same name.

FILING-CLEAN OUTPUT IS ABSOLUTE
The replaced text is filing text. No placeholder, note, question, TODO, instruction to a person, or
legal conclusion about patentability, novelty, validity or infringement. No version or draft
number, no change log, no editorial aside, no sentence addressed to anyone but the examiner. Use
commas, colons, full stops, or ordinary hyphens; never use an em dash."""

SECTION_EDIT_PROMPT = """Edit ONE section of this application: %(heading)s (the file
draft/%(filename)s).

What the user asked for is in input/request.md.

Read before you write anything: input/request.md, draft/%(filename)s, draft/numerals.md, the rest
of draft/, input/disclosure.md, input/conversation.md, and prior_art/INDEX.md with any reference
that bears on the request. Use tools/patent_lookup.py if you need what a reference actually says.

Then return the smallest set of find/replace pairs inside draft/%(filename)s that does what was
asked. Change nothing else, and touch no file: your structured answer is the only output.

The current text of that section is between the markers below, and a `find` value must be copied
from it verbatim.
--- BEGIN %(heading)s ---
%(current)s
--- END %(heading)s ---"""


class SectionEditError(StudioError):
    """A returned patch could not be applied to the section it names."""


def apply_section_edits(current: str, result: Mapping[str, Any]) -> str:
    """Apply an agent's patch to one section's text, or say exactly why it does not fit.

    An edit that does not apply is a hard failure rather than a partial one. Applying three of four
    pairs would publish a section the agent never wrote and never read back, which is a worse
    outcome than telling the user the attempt missed and letting it run again.
    """
    replacement = str(result.get("replacement") or "")
    edits = [dict(item) for item in (result.get("edits") or ())
             if isinstance(item, Mapping)]
    if replacement.strip() and edits:
        raise SectionEditError(
            "The edit returned both a whole-section replacement and a list of changes. "
            "Only one of them can be the intended edit.")
    if replacement.strip():
        return str(human_text(replacement)).replace("\x00", "").strip()
    if not edits:
        raise SectionEditError("The edit returned no change to apply.")
    text = str(current or "")
    for index, item in enumerate(edits, 1):
        find = str(human_text(item.get("find") or ""))
        if not find.strip():
            raise SectionEditError(f"Change {index} has nothing to find.")
        replace = str(human_text(item.get("replace") or ""))
        occurrences = text.count(find)
        if occurrences == 0:
            #  Whitespace is the usual near miss: the agent quotes a wrapped paragraph back with
            #  its line breaks collapsed. Try that reading once, and only when it is unambiguous.
            loose = _loose_find(text, find)
            if loose is None:
                raise SectionEditError(
                    f"Change {index} looks for text that is not in this section: {find[:160]!r}.")
            start, end = loose
            text = text[:start] + replace + text[end:]
            continue
        if occurrences > 1:
            raise SectionEditError(
                f"Change {index} looks for text that appears {occurrences} times in this section, "
                f"so where it belongs is ambiguous: {find[:160]!r}.")
        text = text.replace(find, replace, 1)
    return str(human_text(text)).replace("\x00", "").strip()


def _loose_find(text: str, find: str) -> tuple[int, int] | None:
    """Locate ``find`` in ``text`` ignoring how whitespace is broken up, if that is unambiguous."""
    pattern = re.compile(r"\s+".join(re.escape(part) for part in find.split()))
    if not pattern.pattern:
        return None
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return None
    return matches[0].start(), matches[0].end()


def build_section_edit_prompt(section_key: str, sections: Mapping[str, str]) -> str:
    entry = draft_workspace.SECTION_BY_KEY.get(section_key)
    if not entry:
        raise SectionEditError(f"{section_key!r} is not a section of this application.")
    filename, heading = entry
    return SECTION_EDIT_PROMPT % {
        "heading": heading, "filename": filename,
        "current": str(sections.get(section_key) or "").strip() or "(this section is empty)"}


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
    return _blockers(report, drawings=True, text=True)


def text_blockers(report: Mapping[str, Any]) -> list[str]:
    """Classify text findings for the studio without weakening the publication gate."""
    return _blockers(report, drawings=False, text=True)


def drawing_blockers(report: Mapping[str, Any]) -> list[str]:
    """Classify drawing findings for display and targeted automatic repair."""
    return _blockers(report, drawings=True, text=False)


def _blockers(report: Mapping[str, Any], *, drawings: bool, text: bool) -> list[str]:
    blockers: list[str] = []
    if text and str(report.get("status") or "") != "complete":
        blockers.append("The independent review did not complete.")
    for check in report.get("checks") or ():
        if str(check.get("status") or "") == "pass":
            continue
        is_drawing = _report_item_category(check) == "figures_and_numerals"
        if (is_drawing and drawings) or (not is_drawing and text):
            blockers.append(f"Mechanical check did not pass: {check.get('name') or 'unnamed'}")
    for finding in report.get("findings") or ():
        is_drawing = _report_item_category(finding) == "figures_and_numerals"
        if (is_drawing and drawings) or (not is_drawing and text):
            blockers.append(f"Independent review finding: {finding.get('title') or 'unnamed'}")
    return list(dict.fromkeys(blockers))


_DRAWING_INSPECTION_CHECK = "Every drawing sheet passes geometry, leader, and OCR inspection"
_FIGURE_PLAN_PREFLIGHT_CHECK = "Drawing plans pass deterministic preflight"
_FIGURE_PLAN_CHECKS = frozenset({
    _FIGURE_PLAN_PREFLIGHT_CHECK,
    "Each drawing numeral appears once",
    "Drawing sheets are not overcrowded",
    "Numerals on the drawings are defined",
    "Every drawing numeral appears in the specification",
    "Every specification numeral appears in a drawing",
    "Application includes a drawing plan",
    "Drawing briefs are concise and renderable",
    "Figure brief numeral declarations match sheet lists",
    "Figure-sheet numbering is unique and contiguous",
    "Every figure used is described",
    "Every drawing sheet is described",
    "Each described figure has a drawing sheet",
})
_DRAWING_EVIDENCE_CHECKS = frozenset({
    "Drawing pixels were inspected",
    "Section views have matching source-view cutting lines",
    "Drawing content matches its specification",
    "Drawing leaders identify the named features",
})


def _report_item_category(item: Mapping[str, Any]) -> str:
    category = str(item.get("category") or "")
    if category:
        return category
    name = str(item.get("name") or "")
    if (name == _DRAWING_INSPECTION_CHECK or name in _FIGURE_PLAN_CHECKS or
            name in _DRAWING_EVIDENCE_CHECKS):
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
    mentioned_figures = draft_qa.figures_mentioned(json.dumps(
        [*checks, *findings], ensure_ascii=False, sort_keys=True))
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
        caption_source = current if (
            current and (not mentioned_figures or baseline_number in mentioned_figures)
        ) else baseline
        locked_figures.append({
            "label": str(baseline.get("label") or f"FIG. {index + 1}"),
            "caption": str(caption_source.get("caption") or ""),
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


def normalize_sections(sections: Mapping[str, str]) -> dict[str, str]:
    """Every section, trimmed and with the disallowed punctuation removed. No judgement."""
    return {key: str(human_text(sections.get(key) or "")).replace("\x00", "").strip()
            for key, _name, _heading in draft_workspace.SECTION_FILES}


def section_problems(sections: Mapping[str, str],
                     allowed_references: Sequence[str] = ()) -> list[str]:
    """EVERY reason this text could not be filed, one line each, rather than the first one.

    ``validate_sections`` raises on the first thing it finds, which is right when it is judging a
    draft the agent has just written: the whole turn is rejected either way. It is wrong for an
    edit scoped to one section, because it cannot tell "you broke this" from "this was already
    broken". A scoped edit has to be allowed to leave the rest of the application exactly as bad as
    it found it, or a placeholder somebody left in the Cross-Reference two months ago makes every
    other section uneditable for ever. That happened, on a real project, to a request that only
    wanted a phrase removed from the Field of the Disclosure.
    """
    out: list[str] = []
    normalised = normalize_sections(sections)
    allowed = {draft_cite.normalize(item) for item in allowed_references
               if draft_cite.normalize(item)}
    total = 0
    for key, _name, heading in draft_workspace.SECTION_FILES:
        value = normalised[key]
        total += len(value)
        if not value:
            out.append(f"{heading} is empty.")
        for raw in draft_cite.malformed_citations_in(value):
            out.append(f"{heading} contains a malformed citation [REF:{raw[:40]}].")
        for citation in draft_cite.citations_in(value):
            canonical = draft_cite.normalize(citation)
            if not canonical:
                out.append(
                    f"{heading} cites an unusable publication number [REF:{citation[:40]}].")
            elif canonical not in allowed:
                out.append(
                    f"{heading} cites {canonical}, which is not among this project's sources.")
        for pattern in drafting._LEGAL_CONCLUSION_PATTERNS:
            found = pattern.search(value)
            if found:
                out.append(f"{heading} states a legal conclusion ({found.group(0)!r}).")
    if total > drafting.MAX_GENERATED_CHARS:
        out.append("The draft is too large to store safely.")
    for marker in draft_qa.find_placeholders(normalised):
        out.append(f"The draft contains an unresolved placeholder: {marker}.")
    return list(dict.fromkeys(out))


def validate_snapshot(snapshot: Mapping[str, Any],
                      allowed_references: Sequence[str] = (),
                      drawing_problems: list[str] | None = None) -> dict[str, Any]:
    """Validate every agent-owned filing artifact before any image call or version save.

    Pass ``drawing_problems`` and everything about the SHEETS, the numeral table and the figure
    plan is appended to it instead of raised. That is what lets a wording change publish while a
    drawing is still wrong: the text is judged on the text's own merits and the drawing state is
    carried forward as a reported defect. Leave it out and the old all-or-nothing gate applies,
    which is what the retry-candidate path still wants.
    """
    collect = drawing_problems is not None

    def refuse(message: str, category: str) -> None:
        if collect and category == "figures_and_numerals":
            drawing_problems.append(message)
            return
        raise FilingPreflightError(message, category=category)

    sections = validate_sections(snapshot.get("sections") or {}, allowed_references)
    numerals = [human_text(dict(item)) for item in (snapshot.get("numerals") or ())]
    figures = [human_text(dict(item)) for item in (snapshot.get("figures") or ())]
    markers = []
    markers.extend(draft_qa.placeholders_in_text(
        "Reference numeral table", json.dumps(numerals, ensure_ascii=False)))
    markers.extend(draft_qa.placeholders_in_text(
        "Drawing specifications", json.dumps(figures, ensure_ascii=False)))
    if markers:
        refuse("The filing artifacts contain an unresolved placeholder: " + markers[0] + ".",
               "figures_and_numerals")
    for figure in figures:
        values = {draft_qa._drawing_numeral(value)
                  for value in (figure.get("numerals") or [])}
        values.discard("")
        if len(values) > draft_qa.MAX_NUMERALS_PER_SHEET:
            label = str(figure.get("label") or "Drawing")[:80]
            refuse(f"{label} lists {len(values)} numerals, which is more than "
                   f"{draft_qa.MAX_NUMERALS_PER_SHEET} numerals on one sheet. Split it into "
                   "focused views and synchronize the drawing descriptions.",
                   "figures_and_numerals")
    mechanical = draft_qa.run_checks(
        sections=sections, numerals=numerals, figures=figures,
        allowed_references=allowed_references, allow_remote=False)
    failures = [item for item in mechanical if str(item.get("status") or "") == "fail"]
    #  Classified ONE BY ONE. The old rule labelled a mixed set "internal_logic" and refused the
    #  lot, so a single drawing-plan failure alongside a text one hid both behind a text error.
    drawing_side = [item for item in failures
                    if _report_item_category(item) == "figures_and_numerals"]
    text_side = [item for item in failures if item not in drawing_side]
    for group, category in ((text_side, "internal_logic"),
                            (drawing_side, "figures_and_numerals")):
        if not group:
            continue
        details = []
        for item in group[:8]:
            evidence = list(item.get("items") or [])
            evidence_text = " | ".join(str(value)[:180] for value in evidence[:6])
            details.append(
                f"{item.get('name') or 'Unnamed check'}: " +
                (evidence_text or str(item.get("detail") or "failed")[:300]))
        refuse("The candidate failed the mechanical filing preflight. " + "; ".join(details),
               category)
    return {"sections": sections, "numerals": numerals, "figures": figures}


_PART_ORDINAL_RE = re.compile(
    r"\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b",
    re.IGNORECASE)
_PROTECTED_DRAWING_NUMBER_PREFIX_RE = re.compile(
    r"\b(?:fig(?:ure)?\.?|sheet|claim|line|section|no\.?)\s*$", re.IGNORECASE)
_MEASUREMENT_SUFFIX_RE = re.compile(
    r"^\s*(?:%|percent|mm|cm|m\b|in\.?|inch(?:es)?|ft|kg|g\b|lb|psi|kpa|mpa|bar|"
    r"deg|degrees?|hz|khz|mhz|v\b|volts?|a\b|amps?|w\b|watts?|sec(?:onds?)?|"
    r"min(?:utes?)?|hours?|rpm|newtons?)\b", re.IGNORECASE)
_FOCUSED_FIGURE_RE = re.compile(
    r"\b(?:cross[ -]section(?:al)?|sectional|fragmentary|detail(?:ed)?|enlarged|exploded|"
    r"focused)\b", re.IGNORECASE)
_PART_STOPWORDS = frozenset({"a", "an", "the", "of", "for", "and", "or", "to", "in", "on"})


def _part_role(part: Any) -> str:
    """Group corresponding leaf parts while ignoring first/second view qualifiers."""
    value = re.sub(r"[^a-z0-9]+", " ", str(part or "").lower())
    value = _PART_ORDINAL_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def _strip_reference_numeral_from_brief(caption: Any, numeral: str, part: str) -> str:
    """Make a retained context part unnumbered without touching view or section numbers."""
    value = str(caption or "")
    part_words = set(re.findall(
        r"[a-z0-9]+", _PART_ORDINAL_RE.sub(" ", str(part or "").lower())))
    part_words -= _PART_STOPWORDS
    pattern = re.compile(
        rf"(?<![A-Za-z0-9-]){re.escape(numeral)}(?![A-Za-z0-9-])",
        re.IGNORECASE)

    def replace(match: re.Match[str]) -> str:
        before = value[max(0, match.start() - 32):match.start()]
        after = value[match.end():match.end() + 20]
        if _PROTECTED_DRAWING_NUMBER_PREFIX_RE.search(before):
            return match.group(0)
        if _MEASUREMENT_SUFFIX_RE.search(after):
            return match.group(0)
        preceding_words = re.findall(r"[a-z0-9]+", before.lower())[-4:]
        if not part_words.intersection(preceding_words):
            return match.group(0)
        return ""

    value = pattern.sub(replace, value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"[ \t]+([,.;:])", r"\1", value)
    return value.strip()


def normalize_overcrowded_figure_plans(
        snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove only redundant leaf labels from sheets above the deterministic limit.

    The complete numeral table remains authoritative. A label is eligible only when another
    sheet retains it, so this normalization cannot erase a disclosed component from the drawing
    set. Corresponding leaf parts are removed as a group when possible, which keeps paired views
    symmetric. The component remains in the brief as unnumbered context, while figure numbers,
    cutting-plane designations, and measurements remain unchanged.
    """
    out = human_text({
        "sections": dict(snapshot.get("sections") or {}),
        "numerals": [dict(item) for item in (snapshot.get("numerals") or ())
                      if isinstance(item, Mapping)],
        "figures": [dict(item) for item in (snapshot.get("figures") or ())
                    if isinstance(item, Mapping)],
    })
    figures = out["figures"]
    table = {
        str(item.get("numeral") or "").strip().upper(): str(item.get("part") or "").strip()
        for item in out["numerals"]
    }
    coverage: dict[str, int] = {}
    focused_coverage: dict[str, int] = {}
    for figure in figures:
        seen = {draft_qa._drawing_numeral(item)
                for item in (figure.get("numerals") or ())}
        for numeral in seen - {""}:
            coverage[numeral] = coverage.get(numeral, 0) + 1
            if _FOCUSED_FIGURE_RE.search(str(figure.get("caption") or "")):
                focused_coverage[numeral] = focused_coverage.get(numeral, 0) + 1

    changes: list[str] = []
    for figure in figures:
        entries = list(figure.get("numerals") or ())
        ordered = list(dict.fromkeys(
            draft_qa._drawing_numeral(item) for item in entries))
        ordered = [item for item in ordered if item]
        excess = len(ordered) - draft_qa.MAX_NUMERALS_PER_SHEET
        if excess <= 0:
            continue
        current_focused = bool(
            _FOCUSED_FIGURE_RE.search(str(figure.get("caption") or "")))
        eligible = [
            numeral for numeral in ordered
            if coverage.get(numeral, 0) > 1 and
            focused_coverage.get(numeral, 0) - int(current_focused) > 0 and
            re.fullmatch(r"[A-Z]?\d{2,4}[A-Z]?", numeral) and table.get(numeral)
        ]
        if len(eligible) < excess:
            continue

        groups: dict[str, list[str]] = {}
        for numeral in eligible:
            groups.setdefault(_part_role(table[numeral]), []).append(numeral)
        selected: list[str] = []
        remaining = excess
        grouped = [values for values in groups.values()
                   if 1 < len(values) <= remaining]
        grouped.sort(key=lambda values: (
            -max(len(table[item].split()) for item in values),
            -len(values),
            ordered.index(values[0]),
        ))
        for values in grouped:
            if len(values) > remaining:
                continue
            selected.extend(values)
            remaining -= len(values)
            if not remaining:
                break
        if len(selected) != excess:
            continue

        removed = set(selected)
        figure["numerals"] = [
            item for item in entries
            if draft_qa._drawing_numeral(item) not in removed
        ]
        caption = str(figure.get("caption") or "")
        for numeral in selected:
            caption = _strip_reference_numeral_from_brief(
                caption, numeral, table[numeral])
        figure["caption"] = caption
        label = str(figure.get("label") or "Drawing")[:80]
        changes.append(
            f"{label}: moved redundant labels {', '.join(selected)} to focused sheets")
    return out, changes


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
        if key == "government_support" and (value is None or value == ""):
            sections[key] = ""
            continue
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


def _manual_change_note(keys: Sequence[str]) -> str:
    """What History shows for a version the user typed."""
    headings = [draft_workspace.SECTION_BY_KEY[key][1]
                for key in keys if key in draft_workspace.SECTION_BY_KEY]
    if not headings:
        return "Edited by hand."
    if len(headings) == 1:
        return f"Edited by hand: {headings[0]}."
    return f"Edited by hand: {', '.join(headings[:-1])} and {headings[-1]}."


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
        for path in _MIGRATIONS:
            if not path.exists():
                continue
            sql = path.read_text(encoding="utf-8")
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
            cur.execute(
                "SELECT * FROM (SELECT * FROM app_draft_messages WHERE project_id=%s "
                "ORDER BY id DESC LIMIT %s) recent ORDER BY id",
                (int(project_id), int(limit)))
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
                     project_revision: int, idempotency_key: str | None = None,
                     section_key: str = "") -> dict[str, Any]:
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
                "project_revision,kind,user_message,idempotency_key,stage,section_key) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',%s) RETURNING *",
                (int(project_id), turn_no, int(user_id), int(project_revision), kind,
                 str(user_message or "")[:MAX_MESSAGE_CHARS], idempotency_key,
                 str(section_key or "")[:40]))
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

    def source_review_cache(self, source_hash: str) -> dict[str, Any] | None:
        """Return a completed review for the exact content hash, never for similar text."""
        self._ready()
        source_hash = str(source_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise drafting.DraftingValidationError("Source-review cache key is invalid.")
        with self._cursor() as cur:
            cur.execute(
                "SELECT report FROM app_draft_source_review_cache WHERE source_hash=%s",
                (source_hash,))
            row = cur.fetchone()
        report = _json((row or {}).get("report"), {})
        return dict(report) if isinstance(report, Mapping) and report else None

    def save_source_review_cache(self, source_hash: str, report: Mapping[str, Any]) -> None:
        """Persist one completed independent review for safe reuse after worker retries."""
        self._ready()
        source_hash = str(source_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise drafting.DraftingValidationError("Source-review cache key is invalid.")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO app_draft_source_review_cache (source_hash,report) "
                "VALUES (%s,%s::jsonb) ON CONFLICT (source_hash) DO NOTHING",
                (source_hash, _dumps(dict(report))))

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

    def queue_ahead(self, turn_id: int) -> int:
        """How many other applications' turns this one is behind.

        The worker pool is shared by every project on the host, so a queued turn is not always
        "about to start": it can be behind a drawing repair on somebody else's application that has
        been running for hours. Saying so is the difference between a page that looks broken and a
        page that is telling the truth.
        """
        self._ready()
        with self._cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM app_draft_turns ahead "
                "JOIN app_draft_turns self_ ON self_.id=%s "
                "JOIN app_drafting_projects p ON p.id=ahead.project_id "
                "WHERE ahead.id<>self_.id AND p.status<>'archived' "
                "AND ahead.status IN ('queued','running') "
                "AND (ahead.status='running' "
                "     OR (ahead.next_attempt_at,ahead.id)<(self_.next_attempt_at,self_.id))",
                (int(turn_id),))
            row = cur.fetchone()
        return int((row or {}).get("n") or 0)

    def save_settings(self, project_id: int, values: Mapping[str, Any]) -> None:
        """Store this project's advanced settings whole."""
        self._ready()
        with self._cursor() as cur:
            cur.execute("UPDATE app_drafting_projects SET settings=%s::jsonb,updated_at=now() "
                        "WHERE id=%s", (_dumps(dict(values)), int(project_id)))

    def set_draft_model(self, project_id: int, model: str) -> str:
        """Choose the model tier this project drafts on. '' means the host default."""
        self._ready()
        chosen = draft_agent.normalize_model(model)
        with self._cursor() as cur:
            cur.execute("UPDATE app_drafting_projects SET draft_model=%s,updated_at=now() "
                        "WHERE id=%s", (chosen, int(project_id)))
        return chosen

    def cancel_turn(self, project_id: int, turn_id: int) -> None:
        self._ready()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE app_draft_turns SET status='cancelled',stage='cancelled',"
                "completed_at=now(),updated_at=now(),lease_token_hash=NULL,lease_expires_at=NULL,"
                "last_error='Cancelled by the user' WHERE project_id=%s AND id=%s "
                "AND status IN ('queued','running') RETURNING kind,idempotency_key",
                (int(project_id), int(turn_id)))
            cancelled = cur.fetchone()
            if not cancelled:
                return
            automatic_filing = bool(
                cancelled.get("kind") == "gate_resume" or
                _AUTOMATIC_GATE_RESUME_TURN_KEY.match(
                    str(cancelled.get("idempotency_key") or "")))
            if automatic_filing:
                # A published text version is not filing-ready while its mandatory drawing
                # continuation is incomplete. Keep the candidate so the exact package can resume.
                cur.execute("UPDATE app_drafting_projects SET status='active',updated_at=now() "
                            "WHERE id=%s", (int(project_id),))
            else:
                cur.execute("UPDATE app_drafting_projects SET status=CASE "
                            "WHEN latest_version_no>0 THEN 'ready' ELSE 'active' END,"
                            "updated_at=now() WHERE id=%s", (int(project_id),))
                cur.execute("DELETE FROM app_draft_turn_candidates WHERE turn_id=%s",
                            (int(turn_id),))

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

    def record_spend(self, turn_id: int, *, runs: int = 1, cost_usd: float = 0.0,
                     duration_ms: int = 0,
                     tokens: Mapping[str, int] | None = None) -> dict[str, Any]:
        """Add one agent run to the turn's running total, and return the total.

        Written as it happens rather than at completion, because the number nobody could see was
        the whole problem: a turn that had spent hundreds of dollars over eight hours still read
        cost 0.00 in the database and "independent review" on the page. No lease is required and
        none is checked: this is bookkeeping, not ownership, and losing the record because a lease
        turned over mid-turn would defeat the point.
        """
        self._ready()
        counts = dict(tokens or {})
        with self._cursor() as cur:
            cur.execute(
                "UPDATE app_draft_turns SET agent_runs=agent_runs+%s,"
                "spend_usd=spend_usd+%s,model_ms=model_ms+%s,"
                "tokens_input=tokens_input+%s,tokens_output=tokens_output+%s,"
                "tokens_cache_read=tokens_cache_read+%s,"
                "tokens_cache_write=tokens_cache_write+%s,updated_at=now() "
                "WHERE id=%s RETURNING agent_runs,spend_usd,model_ms,tokens_input,"
                "tokens_output,tokens_cache_read,tokens_cache_write",
                (int(runs), round(float(cost_usd or 0), 4), int(duration_ms or 0),
                 int(counts.get("input") or 0), int(counts.get("output") or 0),
                 int(counts.get("cache_read") or 0), int(counts.get("cache_write") or 0),
                 int(turn_id)))
            row = cur.fetchone()
        out = dict(row or {})
        out["spend_usd"] = float(out.get("spend_usd") or 0)
        out["tokens_total"] = sum(int(out.get(key) or 0) for key in
                                  ("tokens_input", "tokens_output",
                                   "tokens_cache_read", "tokens_cache_write"))
        return out

    def current_spend(self, turn_id: int) -> dict[str, Any]:
        """Return the charged total before another paid run is allowed to start."""
        self._ready()
        with self._cursor() as cur:
            cur.execute(
                "SELECT agent_runs,spend_usd,model_ms,tokens_input,tokens_output,"
                "tokens_cache_read,tokens_cache_write FROM app_draft_turns WHERE id=%s",
                (int(turn_id),))
            row = cur.fetchone()
        out = dict(row or {})
        out["spend_usd"] = float(out.get("spend_usd") or 0)
        out["tokens_total"] = sum(int(out.get(key) or 0) for key in
                                  ("tokens_input", "tokens_output",
                                   "tokens_cache_read", "tokens_cache_write"))
        return out

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

    def save_manual_version(self, project_id: int, user_id: int, *,
                            sections: Mapping[str, str], citations: Sequence[str],
                            edited_sections: Sequence[str],
                            numerals: Sequence[Mapping[str, Any]] = (),
                            figures: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """Publish a version the USER typed, continuing their editing session where there is one.

        Autosave and a version are at odds: a version per debounced keystroke turns History into
        noise, and one version an hour loses the intermediate states that make an undo possible.
        The compromise is a session. While the newest version is one this same person typed within
        the last quarter of an hour, and no agent turn and no review has read it since, their next
        save rewrites it in place and adds the newly touched section to its list. Anything else,
        including the first hand edit after the agent has published, opens a new version.

        A manual version is marked ``origin='manual'`` and is deliberately NOT put through the
        filing gates here. The user is allowed to write what they mean and see it saved; the Review
        tab is what tells them whether it still passes, and it can be re-run on demand.
        """
        self._ready()
        touched = [str(key) for key in edited_sections if key]
        with self._cursor() as cur:
            cur.execute("SELECT * FROM app_drafting_projects WHERE id=%s FOR UPDATE",
                        (int(project_id),))
            project = cur.fetchone()
            if not project:
                raise drafting.DraftingNotFound("Draft project was not found.")
            project = dict(project)
            head_no = int(project.get("latest_version_no") or 0)
            head = None
            if head_no:
                cur.execute(
                    "SELECT id,version_no,origin,created_by_user_id,edited_sections,turn_id,"
                    "created_at>now()-interval '15 minutes' AS fresh "
                    "FROM app_draft_versions WHERE project_id=%s AND version_no=%s FOR UPDATE",
                    (int(project_id), head_no))
                head = cur.fetchone()
            continuing = bool(
                head and str(head.get("origin") or "") == "manual" and
                int(head.get("created_by_user_id") or 0) == int(user_id) and
                head.get("fresh") and not head.get("turn_id"))
            if continuing:
                cur.execute("SELECT count(*)::int AS n FROM app_draft_qa_reports "
                            "WHERE project_id=%s AND version_no=%s",
                            (int(project_id), head_no))
                if int(cur.fetchone()["n"]):
                    continuing = False
            already = set(_json(head.get("edited_sections"), []) if continuing else [])
            markdown = render_markdown(sections)
            note = _manual_change_note(sorted(already | set(touched)))
            if continuing:
                cur.execute(
                    "UPDATE app_draft_versions SET sections=%s::jsonb,markdown=%s,"
                    "citations=%s::jsonb,numerals=%s::jsonb,figure_specs=%s::jsonb,"
                    "change_note=%s,edited_sections=%s::jsonb,created_at=now() "
                    "WHERE project_id=%s AND version_no=%s RETURNING *",
                    (_dumps(dict(sections)), markdown, _dumps(list(citations)),
                     _dumps([dict(item) for item in numerals]),
                     _dumps([dict(item) for item in figures]), note,
                     _dumps(sorted(already | set(touched))), int(project_id), head_no))
                version_no = head_no
            else:
                version_no = head_no + 1
                cur.execute(
                    "INSERT INTO app_draft_versions (project_id,version_no,base_version_no,"
                    "project_revision,sections,markdown,citations,model_name,created_by_user_id,"
                    "change_note,numerals,figure_specs,origin,edited_sections) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,'',%s,%s,%s::jsonb,%s::jsonb,"
                    "'manual',%s::jsonb) RETURNING *",
                    (int(project_id), version_no, head_no or None, project["revision"],
                     _dumps(dict(sections)), markdown, _dumps(list(citations)), int(user_id),
                     note, _dumps([dict(item) for item in numerals]),
                     _dumps([dict(item) for item in figures]), _dumps(sorted(set(touched)))))
            version = dict(cur.fetchone())
            version["sections"] = _json(version.get("sections"), {})
            for key in ("citations", "numerals", "figure_specs", "edited_sections"):
                version[key] = _json(version.get(key), [])
            adopted = project_title_from(version_no, sections)
            if adopted:
                cur.execute("UPDATE app_drafting_projects SET latest_version_no=%s,title=%s,"
                            "status=CASE WHEN status='archived' THEN status ELSE 'ready' END,"
                            "updated_at=now() WHERE id=%s",
                            (version_no, adopted, int(project_id)))
            else:
                cur.execute("UPDATE app_drafting_projects SET latest_version_no=%s,"
                            "status=CASE WHEN status='archived' THEN status ELSE 'ready' END,"
                            "updated_at=now() WHERE id=%s", (version_no, int(project_id)))
            version["continued"] = continuing
            return version

    def complete_turn(self, turn_id: int, lease_token: str, *, result: Mapping[str, Any],
                      session_id: str, cost_usd: float, duration_ms: int, model_name: str,
                      transcript_path: str = "", discard_candidates: bool = True,
                      continuation: Mapping[str, Any] | None = None,
                      required_figure_count: int = 0
                      ) -> dict[str, Any]:
        self._ready()
        with self._cursor() as cur:
            turn = self._verify(cur, turn_id, lease_token)
            required_figures = max(0, int(required_figure_count or 0))
            if required_figures:
                # Lock the project while the final checked PNG inventory is compared and the
                # ready state is stored. A passing review is evidence about exact image bytes,
                # not permission to publish after those rows have disappeared.
                cur.execute("SELECT user_id FROM app_drafting_projects WHERE id=%s FOR UPDATE",
                            (int(turn["project_id"]),))
                project = cur.fetchone() or {}
                cur.execute(
                    "SELECT count(DISTINCT f.id)::int AS figure_count,"
                    "count(DISTINCT f.id) FILTER (WHERE v.id IS NOT NULL AND v.png IS NOT NULL "
                    "AND v.status='ready')::int AS active_png_count "
                    "FROM app_draft_figures f LEFT JOIN app_draft_figure_versions v "
                    "ON v.figure_id=f.id AND v.version_no=f.active_version "
                    "WHERE f.project_id=%s AND f.user_id=%s AND f.archived_at IS NULL",
                    (int(turn["project_id"]), int(project.get("user_id") or 0)))
                inventory = cur.fetchone() or {}
                if (int(inventory.get("figure_count") or 0) != required_figures or
                        int(inventory.get("active_png_count") or 0) != required_figures):
                    raise drafting.DraftingValidationError(
                        "The checked drawing set changed before filing readiness could be "
                        "stored. Automatic drawing repair will run again.")
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
            if continuation:
                kind = str(continuation.get("kind") or "gate_resume")
                if kind not in {"gate_resume", "qa_fix"}:
                    raise drafting.DraftingValidationError(
                        "Automatic continuation kind is invalid.")
                idempotency_key = str(continuation.get("idempotency_key") or "")[:180]
                if not idempotency_key:
                    raise drafting.DraftingValidationError(
                        "Automatic continuation requires an idempotency key.")
                cur.execute(
                    "SELECT coalesce(max(turn_no),0)+1 AS n FROM app_draft_turns "
                    "WHERE project_id=%s", (int(turn["project_id"]),))
                continuation_turn_no = int(cur.fetchone()["n"])
                cur.execute(
                    "INSERT INTO app_draft_turns (project_id,turn_no,requested_by_user_id,"
                    "project_revision,kind,user_message,idempotency_key,stage) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'queued') RETURNING id",
                    (int(turn["project_id"]), continuation_turn_no,
                     int(turn["requested_by_user_id"]), int(turn["project_revision"]), kind,
                     str(continuation.get("user_message") or "")[:MAX_MESSAGE_CHARS],
                     idempotency_key))
                out["continuation_turn_id"] = int(cur.fetchone()["id"])
                cur.execute(
                    "UPDATE app_drafting_projects SET status='queued',agent_session_id=%s,"
                    "agent_turn_no=%s,updated_at=now() WHERE id=%s",
                    (str(session_id)[:80], turn["turn_no"], turn["project_id"]))
            else:
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
                 workspace=draft_workspace,
                 stop_event: threading.Event | None = None):
        self.repository = repository
        self.drafting = drafting_repository
        self.agent = agent
        self.qa = qa
        self.workspace = workspace
        self.stop_event = stop_event
        self._source_review_cache: dict[str, dict[str, Any]] = {}

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
        history = [m for m in self.repository.messages(project_id, limit=400)
                   if m["role"] in ("user", "agent")]
        latest_qa = self.repository.latest_qa(project_id)
        sections = loaded["sections"]
        seeded = False
        retry_snapshot = None

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
                "published_snapshot": {
                    "sections": loaded["sections"] or {},
                    "numerals": loaded["numerals"],
                    "figures": loaded["figures"],
                },
                "prepared_qa": latest_qa or {},
                "previous_sections": loaded["sections"] or {}}

    # -- the turn --------------------------------------------------------------------------------
    def _run_agent(self, *, turn_id: int, lease: str, workspace: Path, prompt: str,
                   session_id: str, resume: bool, transcript: Path,
                   stage: str, model: str = "", system_prompt: str = DRAFT_SYSTEM,
                   schema: Mapping[str, Any] = TURN_SCHEMA,
                   tools: str = "", house: str = "") -> draft_agent.AgentRun:
        # Resolve the method on the class. Test doubles commonly use an unconstrained Mock,
        # whose instance-level getattr fabricates a callable for every missing attribute.
        current_spend = getattr(type(self.repository), "current_spend", None)
        if callable(current_spend):
            spent = current_spend(self.repository, turn_id)
            if isinstance(spent, Mapping):
                self._check_budget(turn_id, spent)
        self.repository.heartbeat(turn_id, lease, stage=stage)
        beat = _Heartbeat(self.repository, turn_id, lease, stage)
        beat.start()
        try:
            extra = {"tools": tools} if tools else {}
            cancel = (_AnyEvent(beat.cancelled, self.stop_event)
                      if self.stop_event is not None else beat.cancelled)
            run = self.agent.run(
                workspace=workspace, prompt=prompt,
                system_prompt=system_prompt + str(house or ""),
                schema=schema, session_id=session_id, resume=resume,
                model=model or self.agent.DRAFT_MODEL, timeout=self.agent.DRAFT_TIMEOUT,
                transcript=transcript, cancel=cancel, **extra)
        finally:
            beat.stop()
        try:
            spent = self.repository.record_spend(
                turn_id, cost_usd=run.cost_usd, duration_ms=run.duration_ms, tokens=run.tokens)
            self._check_budget(turn_id, spent)
        except TurnBudgetSpent:
            raise
        except Exception:                                      # noqa: BLE001 - never lose a run
            traceback.print_exc()
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
        prior_report: dict[str, Any] = {}
        try:
            candidate = self.repository.retry_candidate(turn_id)
            stored_report = candidate.get("qa_report") if isinstance(candidate, Mapping) else None
            if isinstance(stored_report, Mapping) and (
                    stored_report.get("checks") or stored_report.get("findings")):
                prior_report = human_text(dict(stored_report))
        except Exception:
            pass
        if prior_report:
            summary = str(prior_report.get("summary") or "").strip()
            prior_report["summary"] = (
                summary + " The automatic repair run stopped after preserving this candidate; "
                "resume the listed filing-gate repairs."
            ).strip()
            prior_report["last_error"] = detail
            prior_report["interruption"] = {
                "detail": detail,
                "action": "Resume the saved candidate and complete the listed repairs.",
            }
            self.repository.save_retry_candidate(
                turn_id, lease, snapshot=snapshot, report=prior_report)
            try:
                self.workspace._write_review(workspace, prior_report)
            except Exception:
                pass
            return
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

    def _reconcile_drawings(self, *, turn_id: int, lease: str, project_id: int, user_id: int,
                            sections: Mapping[str, str],
                            numerals: Sequence[Mapping[str, str]],
                            figures: Sequence[Mapping[str, Any]], disclosure: str,
                            workspace: Path, deadline: float = 0.0) -> list[str]:
        """The drawing pass, run on its own and never inside a drafting turn.

        Returns ordinary visual defects for repair. Capacity, deadline, source-review, and lease
        failures raise so the durable worker retries the same saved candidate without rewriting
        accepted sheets. `filing_blockers` refuses a package while any defect is outstanding.
        """
        import draft_figures

        if self._drawings_already_match(project_id, user_id, numerals, figures):
            written = draft_figures.materialize_review_images(
                project_id, user_id, workspace)
            expected = len(list(figures or ()))
            if written == expected:
                return []
            return [
                f"Only {written} of {expected} checked drawing sheet(s) could be copied into "
                "the independent review workspace. The final review was not run."
            ]
        try:
            generated = self._ensure_figures(
                turn_id=turn_id, lease=lease, project_id=project_id, user_id=user_id,
                sections=sections, numerals=numerals, figures=figures,
                disclosure=disclosure, workspace=workspace,
                deadline=deadline or (time.time() + DRAWING_BUDGET_SECONDS))
        except (drafting.DraftingConflict, SourceFidelityInspectionError,
                SourceReviewUnavailable, DrawingBudgetSpent):
            raise
        except draft_figures.FigureTransientError:
            raise
        except Exception as exc:                               # noqa: BLE001 - never fatal here
            traceback.print_exc()
            return [f"The drawing pass did not finish: {type(exc).__name__}: {str(exc)[:400]}"]
        if generated.get("ok"):
            written = int(generated.get("review_images") or 0)
            expected = len(list(figures or ()))
            if written == expected:
                return []
            return [
                f"Only {written} of {expected} checked drawing sheet(s) could be copied into "
                "the independent review workspace. The final review was not run."
            ]
        errors = [str(item) for item in generated.get("errors") or ()]
        if errors:
            return errors
        if generated.get("budget_spent"):
            raise DrawingBudgetSpent("The drawing pass reached its time budget.")
        return ["One or more sheets did not pass geometry, leader, and OCR inspection."]

    @staticmethod
    def _drawings_already_match(project_id: int, user_id: int,
                                numerals: Sequence[Mapping[str, str]],
                                figures: Sequence[Mapping[str, Any]]) -> bool:
        """Has every sheet already been inspected, and passed, against exactly this brief?

        Deliberately asks the current gates. A changed OCR, semantic, leader, endpoint, or section
        rule invalidates an older approval and the automatic drawing continuation refreshes it.

        Fails closed. Anything unreadable, missing or failing returns False and the pass runs.
        """
        try:
            import draft_figures
            specs = list(figures or ())
            if not specs:
                return False
            stored = {draft_figures.figure_key(item.get("figure_label")): item
                      for item in draft_figures.listing(project_id, user_id)}
            if len(stored) != len(specs):
                return False                       # an orphan or a missing sheet is a real fault
            for sheet_index, spec in enumerate(specs, 1):
                figure = stored.get(draft_figures.figure_key(spec.get("label")))
                if not figure:
                    return False
                active = next((item for item in figure.get("versions") or ()
                               if int(item.get("version_no") or 0) ==
                               int(figure.get("active_version") or 0)), None)
                if not active:
                    return False
                want = draft_figures.specification_hash(
                    str(spec.get("label") or ""), str(spec.get("caption") or ""),
                    draft_figures.expected_entries(spec, numerals))
                semantic = active.get("semantic_audit") or {}
                leader = active.get("leader_audit") or {}
                ocr = active.get("numeral_audit") or {}
                if (semantic.get("specification_hash") != want or
                        leader.get("specification_hash") != want or
                        not draft_figures.current_geometry_binding(
                            figure, user_id, active, str(spec.get("caption") or "")) or
                        not draft_figures.current_semantic_audit(semantic) or
                        not draft_figures.current_leader_audit(leader) or
                        not draft_figures.current_ocr_audit(
                            ocr, expected_sheet_number=f"{sheet_index}/{len(specs)}",
                            expected_section_designations=draft_figures.section_designations(
                                str(spec.get("caption") or "")))):
                    return False
            return True
        except Exception:                                      # noqa: BLE001 - never skip on doubt
            traceback.print_exc()
            return False

    def _record_review_spend(self, turn_id: int, outcome: Mapping[str, Any]) -> None:
        """A reviewer's spend counts too. It was two thirds of the runs on the worst turn seen."""
        try:
            spent = self.repository.record_spend(
                turn_id, cost_usd=float(outcome.get("cost_usd") or 0),
                duration_ms=int(outcome.get("duration_ms") or 0),
                tokens=dict(outcome.get("tokens") or {}))
            self._check_budget(turn_id, spent)
        except TurnBudgetSpent:
            raise
        except Exception:                                      # noqa: BLE001 - never lose a run
            traceback.print_exc()

    def _review_sources(self, *, turn_id: int, lease: str, sections: Mapping[str, str],
                        numerals: Sequence[Mapping[str, str]],
                        figures: Sequence[Mapping[str, Any]], disclosure: str,
                        workspace: Path, model: str = "") -> dict[str, Any]:
        """Does every claim limitation and numbered part trace to what the inventor disclosed?

        A TEXT gate, and the most valuable one in the system: it is what catches the agent inventing
        a structure or a definition to make a claim work. It used to live inside the drawing pass,
        which meant that taking drawings out of a drafting turn silently took this out with them.
        It belongs here, on its own, and it still blocks.
        """
        self.repository.heartbeat(turn_id, lease, stage="checking source fidelity")
        conversation_path = workspace / "input" / "conversation.md"
        conversation = (conversation_path.read_text(encoding="utf-8")
                        if conversation_path.exists() else "")
        brief_path = workspace / "input" / "brief.md"
        brief = (brief_path.read_text(encoding="utf-8")
                 if brief_path.exists() else "")
        configured_version = getattr(self.qa, "SOURCE_REVIEW_VERSION", "")
        source_review_version = (configured_version if isinstance(configured_version, str)
                                 and configured_version else draft_qa.SOURCE_REVIEW_VERSION)
        source_material = {
            "version": source_review_version,
            "model": model or draft_agent.QA_MODEL,
            "disclosure": disclosure,
            "conversation": conversation,
            "brief": brief,
            "sections": dict(sections),
            "numerals": [dict(item) for item in numerals],
            "figures": [dict(item) for item in figures],
        }
        source_hash = hashlib.sha256(json.dumps(
            source_material, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        report = self._source_review_cache.get(source_hash)
        if report is None:
            try:
                durable_report = self.repository.source_review_cache(source_hash)
            except Exception:                                  # cache failure never skips review
                durable_report = None
            if (isinstance(durable_report, Mapping) and
                    durable_report.get("status") == "complete" and
                    durable_report.get("verdict") in ("pass", "fail") and
                    isinstance(durable_report.get("checks"), list) and
                    isinstance(durable_report.get("findings"), list)):
                report = human_text(dict(durable_report))
                self._source_review_cache[source_hash] = report
        if report is None:
            transcript = workspace / ".agent" / (
                f"source-review-{turn_id:04d}-{source_hash[:12]}.jsonl")
            transcript.parent.mkdir(parents=True, exist_ok=True)
            beat = _Heartbeat(self.repository, turn_id, lease, "checking source fidelity")
            beat.start()
            try:
                cancel = (_AnyEvent(beat.cancelled, self.stop_event)
                          if self.stop_event is not None else beat.cancelled)
                outcome = self.qa.review_sources(
                    workspace, transcript=transcript, model=model, cancel=cancel)
            finally:
                beat.stop()
            self._record_review_spend(turn_id, outcome)
            if outcome.get("cancelled"):
                raise drafting.DraftingConflict("Stopped at your request.")
            findings = list(outcome.get("findings") or [])
            completed = bool(outcome.get("ok"))
            if not completed:
                detail = (str(outcome.get("error") or "").strip() or
                          "The independent source reviewer did not return a valid result.")
                raise SourceReviewUnavailable(detail)
            passed = completed and not findings
            detail = (str(outcome.get("summary") or "").strip() or
                      ("The independent source-fidelity review completed without findings."
                       if passed else str(outcome.get("error") or "").strip() or
                       "The independent source-fidelity review did not pass."))
            check = {
                "name": "Source fidelity is clean before rendering",
                "status": "pass" if passed else "fail",
                "severity": "info" if passed else "error",
                "category": "disclosure_fidelity",
                "detail": detail[:4000],
                "items": [str(item.get("title") or "Source-fidelity finding")[:600]
                          for item in findings],
            }
            report = human_text({
                "status": "complete" if completed else "failed",
                "verdict": "pass" if passed else "fail",
                "summary": detail[:8000],
                "checks": [check],
                "findings": findings,
                "counts": draft_qa.counts_for([check], findings),
                "cost_usd": outcome.get("cost_usd") or 0.0,
                "duration_ms": int(outcome.get("duration_ms") or 0),
                "model_name": outcome.get("model") or "",
                "last_error": outcome.get("error") or "",
            })
            self._source_review_cache[source_hash] = report
            try:
                self.repository.save_source_review_cache(source_hash, report)
            except Exception:                                  # a cache write is only an optimization
                traceback.print_exc()
        enforced_report = human_text(
            draft_qa.enforce_deterministic_source_fidelity(report, workspace))
        if enforced_report != report:
            report = enforced_report
            self._source_review_cache[source_hash] = report
            try:
                self.repository.save_source_review_cache(source_hash, report)
            except Exception:                                  # a cache write is only an optimization
                traceback.print_exc()
        return report

    def _ensure_figures(self, *, turn_id: int, lease: str, project_id: int, user_id: int,
                        sections: Mapping[str, str], numerals: Sequence[Mapping[str, str]],
                        figures: Sequence[Mapping[str, Any]], disclosure: str,
                        workspace: Path, deadline: float = 0.0) -> dict[str, Any]:
        import draft_figures

        report = self._review_sources(
            turn_id=turn_id, lease=lease, sections=sections, numerals=numerals,
            figures=figures, disclosure=disclosure, workspace=workspace)
        if filing_blockers(report):
            raise SourceFidelityInspectionError(report)

        draft_figures.checkpoint_project_figures(turn_id, project_id, user_id)

        def check_cancel() -> bool:
            self.repository.heartbeat(
                turn_id, lease, stage="drawing and inspecting figures")
            if self.stop_event is not None and self.stop_event.is_set():
                return False
            return not deadline or time.time() <= deadline

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

    def _run_section_edit(self, turn: Mapping[str, Any], context: Mapping[str, Any]
                          ) -> dict[str, Any]:
        """One section, changed by a patch this process applies, published in a single model call.

        Nothing else in the application moves. The numeral table and every figure specification are
        carried forward from the version that was already checked, so no drawing is redrawn and no
        drawing can go stale: a specification that did not change cannot disagree with a sheet that
        did not change. The independent reviewer is not run either; the deterministic checks are,
        and their report is published so the studio still says whether the application is
        consistent.
        """
        turn_id, lease = int(turn["id"]), turn["lease_token"]
        project_id = int(turn["project_id"])
        project = context["project"]
        workspace: Path = context["workspace"]
        allowed = allowed_reference_keys(context["references"], context["documents"])
        section_key = str(turn.get("section_key") or "")
        if section_key not in draft_workspace.SECTION_BY_KEY:
            raise drafting.DraftingValidationError(
                "This edit did not name a section of the application.")

        #  The PUBLISHED version, never a leftover repair candidate. `prepare` prefers a saved
        #  candidate so a blocked full revision can resume, which is right for that lane and wrong
        #  here: the user is looking at the published text, their `find` values come from it, and a
        #  scoped edit must not quietly adopt unpublished work. The workspace is rewritten to the
        #  same state so what the agent reads is exactly what this process will patch.
        loaded = self._load(project_id)
        sections = dict(loaded["sections"] or {})
        if not sections:
            raise drafting.DraftingValidationError(
                "There is no draft to edit yet. Ask for a full draft first.")
        base_numerals = [dict(item) for item in (loaded["numerals"] or ())]
        base_figures = [dict(item) for item in (loaded["figures"] or ())]
        self.workspace.write_sections(workspace, sections)
        self.workspace.write_numerals(workspace, base_numerals)
        self.workspace.write_figures(workspace, base_figures)
        heading = draft_workspace.SECTION_BY_KEY[section_key][1]

        transcript = workspace / ".agent" / f"turn-{turn['turn_no']:04d}.jsonl"
        run = self._run_agent(
            turn_id=turn_id, lease=lease, workspace=workspace,
            prompt=build_section_edit_prompt(section_key, sections),
            session_id=self.agent.new_session_id(), resume=False, transcript=transcript,
            stage=f"editing {heading}"[:60], model=self._model_for(project),
            house=draft_settings.prompt_additions(self._settings_for(project)),
            system_prompt=SECTION_EDIT_SYSTEM, schema=SECTION_EDIT_SCHEMA,
            #  No Write and no Edit. The tool set is what makes this a patch rather than a rewrite:
            #  the agent cannot change the workspace, so its structured answer is the only output
            #  there is, and it is small.
            tools="Read,Glob,Grep,Bash")
        result = human_text(dict(run.result))
        action = str(result.get("action") or "revised")

        if action == "answered":
            self.repository.add_message(
                project_id, "agent",
                str(result.get("answer") or result.get("summary") or "")[:MAX_MESSAGE_CHARS],
                turn_id=turn_id,
                payload={"action": action, "summary": result.get("summary"),
                         "section_key": section_key, "section_heading": heading,
                         "version_no": None, "cost_usd": run.cost_usd,
                         "steps": run.steps[-40:]})
            completed = self.repository.complete_turn(
                turn_id, lease, result=result,
                session_id=str(project.get("agent_session_id") or ""),
                cost_usd=run.cost_usd, duration_ms=run.duration_ms, model_name=run.model,
                transcript_path=str(transcript), discard_candidates=False)
            return {"turn": completed, "version": None}

        self.repository.heartbeat(turn_id, lease, stage="applying the change")
        edited = apply_section_edits(sections.get(section_key) or "", result)
        if edited == str(sections.get(section_key) or "").strip():
            raise SectionEditError(f"The edit to {heading} would not have changed anything.")
        sections[section_key] = edited
        #  Validate the WHOLE application, not only the edited section: a citation key or a legal
        #  conclusion introduced here is exactly as unfilable as one written by a full revision.
        #  Refuse what this edit BROKE, carry what it merely inherited. Anything already wrong
        #  with the published application is reported to the user rather than used to refuse a
        #  change to a different section.
        inherited = set(section_problems(loaded["sections"] or {}, allowed))
        #  Every other section is carried through BYTE FOR BYTE. Normalising the whole application
        #  here would silently rewrite text nobody asked about: the first live run of this lane
        #  turned an em dash into a hyphen in the Background and the Detailed Description, which is
        #  a house rule doing the right thing in the wrong place. Tidying the rest of the draft is
        #  the drafting path's job, not a side effect of changing one clause.
        checked = dict(loaded["sections"] or {})
        checked[section_key] = normalize_sections(sections)[section_key]
        introduced = [item for item in section_problems(checked, allowed)
                      if item not in inherited]
        if introduced:
            raise drafting.DraftingValidationError(
                f"That change to {heading} would leave the application unfilable: "
                + introduced[0])
        carried = [item for item in section_problems(checked, allowed) if item in inherited]
        numerals, figures = base_numerals, base_figures

        version = self.repository.save_version(
            turn_id, lease, sections=checked, citations=citations_of(checked),
            change_note=str(result.get("summary") or f"Edited {heading}.")[:4000],
            model_name=run.model, numerals=numerals, figures=figures)
        version_no = int(version["version_no"])

        self.repository.add_message(
            project_id, "agent", str(result.get("summary") or "")[:MAX_MESSAGE_CHARS],
            turn_id=turn_id,
            payload={"action": "revised", "summary": result.get("summary"),
                     "section_key": section_key, "section_heading": heading,
                     "changes": self.agent.strings(
                         [str(item.get("why") or "") for item in (result.get("edits") or ())
                          if isinstance(item, Mapping)]),
                     "consequences": self.agent.strings(
                         list(result.get("consequences") or []) +
                         [f"Already in the draft before this change: {item}"
                          for item in carried]),
                     "questions": [], "version_no": version_no,
                     "cost_usd": run.cost_usd, "steps": run.steps[-40:]})
        self._publish_review(
            project_id, turn_id=turn_id, version_no=version_no, workspace=workspace,
            report=self.mechanical_report(
                project_id, sections=checked, numerals=numerals, figures=figures,
                allowed=allowed, scope=heading, carried=carried))
        completed = self.repository.complete_turn(
            turn_id, lease, result=result,
            session_id=str(project.get("agent_session_id") or ""),
            cost_usd=run.cost_usd, duration_ms=run.duration_ms, model_name=run.model,
            transcript_path=str(transcript), discard_candidates=False)
        return {"turn": completed, "version": version}

    def _check_budget(self, turn_id: int, spent: Mapping[str, Any]) -> None:
        """Stop a turn that is running away, and say exactly what it spent.

        THE CEILING EXISTS BECAUSE THERE WAS NONE. A turn on this database made 76 agent runs over
        eight hours and twenty minutes, put about 196 million tokens through the models and cost
        roughly $343, and no part of the system was in a position to notice. Attempts multiply by
        repair rounds, and every worker restart begins the whole thing again, so the real bound on
        one turn's spend was the patience of whoever was watching it.
        """
        limits = getattr(self, "_budget", None) or {}
        runs = int(spent.get("agent_runs") or 0)
        usd = float(spent.get("spend_usd") or 0)
        max_runs = int(limits.get("max_agent_runs") or 0)
        max_usd = float(limits.get("max_spend_usd") or 0)
        if max_runs and runs >= max_runs:
            raise TurnBudgetSpent(
                f"This turn reached its ceiling of {max_runs} agent runs "
                f"(${usd:.2f} spent, {int(spent.get('tokens_total') or 0):,} tokens). The draft it "
                "had reached is saved; nothing was published. A complete saved candidate will "
                "continue automatically in a fresh bounded turn. No manual ceiling change is "
                "required.")
        if max_usd and usd >= max_usd:
            raise TurnBudgetSpent(
                f"This turn reached its ceiling of ${max_usd:.2f} "
                f"({runs} agent runs, {int(spent.get('tokens_total') or 0):,} tokens). The draft "
                "it had reached is saved; nothing was published. A complete saved candidate will "
                "continue automatically in a fresh bounded turn. No manual ceiling change is "
                "required.")

    @staticmethod
    def _settings_for(project: Mapping[str, Any]) -> dict[str, Any]:
        return draft_settings.resolve(_json(project.get("settings"), {}))

    def _model_for(self, project: Mapping[str, Any]) -> str:
        #  Deliberately the module function rather than ``self.agent``: normalising a tier name is
        #  a fact about which models this host will run, not a capability of an injected agent, and
        #  a test double that only needs to answer ``run`` must not have to know about it.
        chosen = self._settings_for(project).get("draft_model")
        return draft_agent.normalize_model(chosen or project.get("draft_model"))

    def mechanical_report(self, project_id: int, *, sections: Mapping[str, str],
                          numerals: Sequence[Mapping[str, Any]],
                          figures: Sequence[Mapping[str, Any]],
                          allowed: Sequence[str], scope: str = "",
                          carried: Sequence[str] = ()) -> dict[str, Any]:
        """The deterministic half of a review, with no model in it.

        Used wherever the text changed and the drawings provably did not: a section edit, and a
        hand edit. It decides in code, the same way every time, so it costs nothing and can run on
        every save. What it deliberately does NOT carry is the reviewer's opinion, so its summary
        says which half ran rather than letting a clean verdict read as a full review.
        """
        started = time.time()
        qa_figures = list(figures)
        try:
            loaded = self._load(project_id)
            qa_figures = figures_for_qa(project_id, int(loaded["project"]["user_id"]), figures)
        except Exception:                                       # noqa: BLE001 - checks still run
            pass
        try:
            checks = self.qa.run_checks(sections=sections, numerals=numerals, figures=qa_figures,
                                        allowed_references=allowed)
        except Exception as exc:                                # noqa: BLE001
            traceback.print_exc()
            checks = [{"name": "Mechanical checks", "status": "fail", "severity": "warn",
                       "detail": f"The checks could not run ({type(exc).__name__}).", "items": []}]
        checks = list(checks)
        if carried:
            checks.append({
                "name": "Defects this change inherited rather than caused",
                "status": "fail", "severity": "error", "category": "internal_logic",
                "detail": "These were already in the published application before this edit. They "
                          "were not allowed to block it, and they still have to be fixed.",
                "items": [str(item)[:600] for item in carried][:12]})
        verdict = self.qa.verdict_for(checks, [])
        what = f"{scope} was changed. " if scope else ""
        return human_text({
            "status": "complete", "verdict": verdict,
            "summary": (what + self.qa.summarize(checks, [], verdict) +
                        " Only the automatic checks ran, because the drawings and the numeral "
                        "table were carried forward unchanged. Re-run the review for the "
                        "independent reading."),
            "checks": checks, "findings": [],
            "counts": self.qa.counts_for(checks, []),
            "cost_usd": 0.0, "duration_ms": int((time.time() - started) * 1000),
            "model_name": "the deterministic checks", "last_error": "",
        })

    def run(self, turn: Mapping[str, Any]) -> dict[str, Any]:
        turn_id, lease = int(turn["id"]), turn["lease_token"]
        project_id = int(turn["project_id"])
        context = self.prepare(turn)
        workspace: Path = context["workspace"]
        project = context["project"]
        allowed = allowed_reference_keys(context["references"], context["documents"])

        self._budget = self._settings_for(project)
        kind = str(turn.get("kind") or "revise")
        if kind == "section_edit" and context["had_version"]:
            return self._run_section_edit(turn, context)
        drawing_continuation = bool(
            kind == "gate_resume" and context.get("resuming_candidate"))
        first = not context["had_version"] and not context.get("resuming_candidate")
        prompt_kind = "initial" if first else ("revise" if kind == "initial" else kind)
        prompt = build_prompt(prompt_kind, seeded=context["seeded"])
        transcript = workspace / ".agent" / f"turn-{turn['turn_no']:04d}.jsonl"

        #  The workspace and previous review are the durable handoff. A project-level model
        #  transcript mixes prior turns into the next invention request and grows without bound.
        prior_session = ""
        run = _gate_resume_run(context, turn)
        if run is None:
            try:
                run = self._run_agent(
                    turn_id=turn_id, lease=lease, workspace=workspace, prompt=prompt,
                    session_id=prior_session or self.agent.new_session_id(),
                    resume=bool(prior_session), transcript=transcript, stage="drafting",
                    model=self._model_for(project),
                    house=draft_settings.prompt_additions(self._settings_for(project)))
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
        rounds_allowed = max(2, min(int(self._settings_for(project).get(
            "finalization_rounds") or MAX_FINALIZATION_ROUNDS), MAX_FINALIZATION_ROUNDS))
        for review_index in range(rounds_allowed):
            if review_index:
                prior_snapshot, prior_report = snapshot, report or {}
                try:
                    repair = self._run_agent(
                        turn_id=turn_id, lease=lease, workspace=workspace,
                        prompt=FINALIZE_PROMPT, session_id=self.agent.new_session_id(),
                        resume=False, transcript=transcript, stage="repairing the draft",
                        model=self._model_for(project),
                        house=draft_settings.prompt_additions(
                            self._settings_for(project)))
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
                        raw_snapshot, allocation_changes = \
                            normalize_overcrowded_figure_plans(repair_snapshot)
                        snapshot = raw_snapshot
                        if allocation_changes:
                            draft_workspace.write_figures(
                                workspace, raw_snapshot["figures"])
                            changes = list(result.get("changes") or [])
                            changes.extend(
                                "Automatically normalized drawing allocation: " + item
                                for item in allocation_changes)
                            result["changes"] = changes
                    #  Figure-plan defects are collected, not raised: they are about the sheets,
                    #  and the text publishes on the text's merits.
                    drawing_faults: list[str] = []
                    snapshot = validate_snapshot(raw_snapshot, allowed, drawing_faults)
                    sections = snapshot["sections"]
                    self.repository.save_retry_candidate(
                        turn_id, lease, snapshot=snapshot,
                        report=_gate_resume_report(runs, result))
                    # Source fidelity is a text gate. It runs before any drawing work so an
                    # unsupported claim or numbered component is repaired before image generation.
                    self.repository.heartbeat(
                        turn_id, lease, stage="checking source fidelity")
                    source_report = self._review_sources(
                        turn_id=turn_id, lease=lease, sections=sections,
                        numerals=snapshot["numerals"], figures=snapshot["figures"],
                        disclosure=str(project.get("disclosure_text") or ""),
                        workspace=workspace,
                        model=self._settings_for(project).get("review_model") or "")
                    if filing_blockers(source_report):
                        raise SourceFidelityInspectionError(source_report)
                    # Text drafting and image work stay separate. Once text passes, a durable
                    # gate-resume turn runs the bounded drawing phase automatically. It can resume
                    # after a restart and cannot mark the project ready until every current image
                    # audit and the final independent review pass.
                    if drawing_continuation:
                        # A plan defect is cheaper and safer to repair before touching the image
                        # lane. Drawing an overcrowded, contradictory, or otherwise invalid brief
                        # cannot cure that brief and only creates pixels that the same turn must
                        # discard. Return the exact preflight findings to the drafting agent first.
                        if drawing_faults:
                            raise FigurePlanInspectionError(drawing_faults)
                        self.repository.heartbeat(
                            turn_id, lease, stage="drawing and inspecting figures")
                        drawing_faults.extend(self._reconcile_drawings(
                            turn_id=turn_id, lease=lease, project_id=project_id,
                            user_id=int(project["user_id"]), sections=sections,
                            numerals=snapshot["numerals"], figures=snapshot["figures"],
                            disclosure=str(project.get("disclosure_text") or ""),
                            workspace=workspace))
                        if drawing_faults:
                            # The final reviewer is evidence-based and is required to open every
                            # checked sheet. Running it with missing or rejected pixels turns a
                            # mechanical drawing defect into invented edits against files that do
                            # not exist in the candidate workspace. Repair the drawing fault first,
                            # then run the independent review over the complete checked package.
                            raise DrawingInspectionError(drawing_faults)
                        self.repository.heartbeat(turn_id, lease, stage="independent review")
                        report = self.evaluate(
                            project_id,
                            version_no=int(project.get("latest_version_no") or 0) + 1,
                            workspace=workspace, allowed=allowed, sections=sections,
                            numerals=snapshot["numerals"], figures=snapshot["figures"],
                            review_index=review_index,
                            review_model=self._settings_for(project).get("review_model") or "",
                            turn_id=turn_id, lease=lease)
                    else:
                        # The source reviewer is the independent text review. The final reviewer
                        # is deliberately deferred because its contract requires opening every
                        # rendered sheet and the byte-exact audit evidence. Calling it here, before
                        # the separate drawing turn, makes it report missing pixels and propose
                        # edits to generated files that do not exist yet.
                        self.repository.heartbeat(turn_id, lease, stage="checking filing text")
                        report = dict(self.mechanical_report(
                            project_id, sections=sections, numerals=snapshot["numerals"],
                            figures=snapshot["figures"], allowed=allowed))
                        source_checks = [dict(item) for item in
                                         (source_report.get("checks") or [])]
                        report["checks"] = source_checks + list(report.get("checks") or [])
                        source_summary = str(source_report.get("summary") or "").strip()
                        if source_summary:
                            report["summary"] = (
                                source_summary + " " + str(report.get("summary") or "").strip()
                            ).strip()[:8000]
                        report["counts"] = draft_qa.counts_for(
                            report["checks"], report.get("findings") or [])
                        report["cost_usd"] = float(source_report.get("cost_usd") or 0.0)
                        report["model_name"] = str(
                            source_report.get("model_name") or report.get("model_name") or "")
                        if drawing_faults:
                            drawing_check = {
                                "name": _FIGURE_PLAN_PREFLIGHT_CHECK,
                                "status": "fail",
                                "severity": "error",
                                "category": "figures_and_numerals",
                                "detail": (
                                    "The filing text passed its text gates. The automatic drawing "
                                    "continuation must repair these drawing-plan defects before "
                                    "the package can be filing ready."
                                ),
                                "items": [str(item)[:600] for item in drawing_faults][:12],
                            }
                            report["checks"].append(drawing_check)
                            report["summary"] = (
                                str(report.get("summary") or "").strip() + " "
                                "The automatic drawing continuation will repair the remaining "
                                "drawing-plan defects."
                            ).strip()[:8000]
                            report["counts"] = draft_qa.counts_for(
                                report["checks"], report.get("findings") or [])
                            report["verdict"] = draft_qa.verdict_for(
                                report["checks"], report.get("findings") or [])
                except SourceFidelityInspectionError as exc:
                    report = exc.report
                except DrawingInspectionError as exc:
                    issue_count = _drawing_issue_count(len(exc.errors))
                    check_name = (
                        _FIGURE_PLAN_PREFLIGHT_CHECK
                        if isinstance(exc, FigurePlanInspectionError)
                        else _DRAWING_INSPECTION_CHECK
                    )
                    check = {
                        "name": check_name,
                        "status": "fail", "severity": "error",
                        "category": "figures_and_numerals",
                        "detail": (
                            f"{issue_count} failed. Each failure is listed "
                            "below so the next repair can address the full set."
                        ),
                        "items": exc.errors,
                    }
                    report = {
                        "status": "failed", "verdict": "fail",
                        "summary": (
                            f"{issue_count} "
                            f"{'requires' if len(exc.errors) == 1 else 'require'} automatic repair."
                        ),
                        "checks": [check], "findings": [],
                        "counts": draft_qa.counts_for([check], []), "cost_usd": 0.0,
                        "duration_ms": 0, "model_name": "", "last_error": str(exc),
                    }
                except drafting.DraftingConflict:
                    raise
                except Exception as exc:                         # a failed gate becomes repair input
                    if getattr(exc, "retry_without_repair", False):
                        raise
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

            # The first phase publishes text on text merits and atomically queues a drawing turn.
            # The drawing continuation is the complete filing gate and cannot publish around any
            # drawing, OCR, endpoint, or independent-review defect.
            blockers = (filing_blockers(report) if drawing_continuation else
                        text_blockers(report))
            if not blockers:
                break
            if snapshot.get("sections"):
                self.repository.save_retry_candidate(
                    turn_id, lease, snapshot=snapshot, report=report)
            self.workspace._write_review(workspace, report)
            if review_index + 1 >= rounds_allowed:
                raise drafting.DraftingValidationError(
                    "The automatic filing gate could not clear: " + "; ".join(blockers[:8]))

        version = None
        final_run = runs[-1]
        candidate_changed = _candidate_differs_from_published(
            context, {**snapshot, "sections": sections})
        if candidate_changed:
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

        # Even a text phase whose stored drawings still pass must run the final package reviewer:
        # wording may have changed relationships or citations that only the complete text-plus-
        # pixels review can judge. The continuation is automatic and uses the saved candidate.
        needs_drawing_continuation = bool(
            not drawing_continuation and snapshot.get("sections"))
        continuation = None
        if needs_drawing_continuation:
            continuation = {
                "kind": "gate_resume",
                "idempotency_key": f"auto-filing-repair-{turn_id}-1",
                "user_message": (
                    "Finish every drawing sheet automatically from the saved filing candidate. "
                    "Run current geometry, semantic, OCR, sheet-number, leader, endpoint, and "
                    "independent-review checks. Publish filing readiness only after all pass."),
            }
        completed = self.repository.complete_turn(
            turn_id, lease, result=result, session_id=final_run.session_id, cost_usd=total_cost,
            duration_ms=total_duration, model_name=final_run.model,
            transcript_path=str(transcript),
            discard_candidates=not needs_drawing_continuation,
            continuation=continuation,
            required_figure_count=(len(snapshot.get("figures") or ())
                                   if drawing_continuation else 0))
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
                 review_index: int = 0, review_model: str = "",
                 turn_id: int = 0, lease: str = "") -> dict[str, Any]:
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
        beat = (_Heartbeat(self.repository, turn_id, lease, "independent review")
                if turn_id and lease else None)
        if beat:
            beat.start()
        try:
            cancel = (None if beat is None and self.stop_event is None else
                      _AnyEvent(*([beat.cancelled] if beat else []),
                                *([self.stop_event] if self.stop_event is not None else [])))
            outcome = self.qa.review(
                workspace, checks=checks, transcript=transcript,
                model=review_model, cancel=cancel)
        finally:
            if beat:
                beat.stop()
        if turn_id:
            self._record_review_spend(turn_id, outcome)
        if outcome.get("cancelled"):
            raise drafting.DraftingConflict("Stopped at your request.")
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


class _AnyEvent:
    """An Event-compatible view that is set when any source event is set."""

    def __init__(self, *events: threading.Event):
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                interval = min(0.2, remaining)
            else:
                interval = 0.2
            self._events[0].wait(interval)
        return True


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
