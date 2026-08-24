"""The on-disk workspace a drafting agent reads, edits and is judged on.

The draft is a small tree of files rather than one blob, for three reasons that all showed up the
moment a whole application was handled as a single string:

  * an agent can Edit one section without rewriting the other eight, so a request to "narrow claim
    1" does not silently reword the background;
  * Grep across the tree is how the reviewer answers "is numeral 34 introduced before it is used"
    and "does every claim term appear in the description" - questions that need the whole document
    at once but only a few lines of it at a time;
  * a version is a diff of named files, which is what makes the change log readable.

Everything here is REBUILDABLE from Postgres.  The workspace is a cache, not the record: deleting
it loses nothing, and ``build`` recreates it from the project, its references, its uploaded
documents and the stored version.  That is deliberate - an agent has write access to this tree, so
nothing irreplaceable may live in it.

LAYOUT
    input/        the disclosure, the brief, the conversation, this turn's request  (read-only)
    prior_art/    one file per reference, plus an INDEX
    draft/        the application itself, one file per section, plus the numeral table
    figures/      one file per drawing: what it shows and which numerals appear on it
    review/       the previous QA report, so the next iteration can fix what it found
    tools/        the corpus lookup the agent is allowed to run
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import drafting
except ModuleNotFoundError:                    # prompt-only tests
    drafting = None                            # type: ignore[assignment]

# Outside $HOME on purpose.  Claude Code walks up from its working directory collecting CLAUDE.md
# files; a workspace under the operator's home directory would inherit that box's own operating
# instructions, which have nothing to do with drafting a patent and grant far wider scope.
DEFAULT_ROOT = os.environ.get("DRAFT_WORKSPACE_ROOT", "/srv/patent-drafts")
FALLBACK_ROOT = Path(__file__).resolve().parents[1] / "data" / "draft_workspaces"

SECTION_FILES = (
    ("title", "01-title.md", "Title"),
    ("cross_reference", "02-cross-reference.md", "Cross-Reference to Related Applications"),
    ("field", "03-field.md", "Field of the Disclosure"),
    ("background", "04-background.md", "Background"),
    ("summary", "05-summary.md", "Summary"),
    ("drawing_descriptions", "06-drawings.md", "Brief Description of the Drawings"),
    ("detailed_description", "07-detailed-description.md", "Detailed Description"),
    ("claims", "08-claims.md", "Claims"),
    ("abstract", "09-abstract.md", "Abstract"),
)
SECTION_BY_KEY = {key: (name, heading) for key, name, heading in SECTION_FILES}
NUMERALS_FILE = "numerals.md"

MAX_REFERENCE_CHARS = 24_000
MAX_TOTAL_REFERENCE_CHARS = 900_000
MAX_DOCUMENT_CHARS = 120_000

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_NUMERAL_CELL_RE = re.compile(r"^\d{1,4}[a-zA-Z]?$", re.IGNORECASE)


def root() -> Path:
    """The workspace root, falling back inside the repo when /srv is not writable (dev, CI)."""
    candidate = Path(DEFAULT_ROOT)
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return candidate
    except OSError:
        FALLBACK_ROOT.mkdir(parents=True, exist_ok=True)
        return FALLBACK_ROOT


def for_project(project_id: int) -> Path:
    return root() / f"p{int(project_id)}"


def destroy(project_id: int) -> None:
    shutil.rmtree(for_project(project_id), ignore_errors=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text or "").rstrip() + "\n", encoding="utf-8")


def _clean(text: Any, limit: int = 400_000) -> str:
    return str(text or "").replace("\x00", "").strip()[:limit]


# ---------------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------------
def write_sections(workspace: Path, sections: Mapping[str, str]) -> None:
    draft = Path(workspace) / "draft"
    draft.mkdir(parents=True, exist_ok=True)
    for key, name, heading in SECTION_FILES:
        body = _clean(sections.get(key), 400_000)
        _write(draft / name, body)


def read_sections(workspace: Path) -> dict[str, str]:
    """Read the section files back, tolerating an agent that added its own heading line.

    A model told "body only" will still sometimes restate the heading, and rejecting the whole
    turn over a duplicated ``## Background`` would throw away a good draft for a cosmetic reason.
    A leading heading whose text matches the section's own name is dropped; any other heading is
    left alone, because in the detailed description headings are legitimate structure.
    """
    draft = Path(workspace) / "draft"
    out: dict[str, str] = {}
    for key, name, heading in SECTION_FILES:
        path = draft / name
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            out[key] = ""
            continue
        lines = [line for line in raw.splitlines() if not line.strip().startswith("<!--")]
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines:
            match = _HEADING_RE.match(lines[0])
            if match and _same_heading(match.group(1), heading):
                lines.pop(0)
        out[key] = "\n".join(lines).strip()
    return out


def _same_heading(found: str, expected: str) -> bool:
    normal = lambda s: re.sub(r"[^a-z]", "", s.lower())     # noqa: E731 - local, one use
    a, b = normal(found), normal(expected)
    return bool(a) and (a == b or a in b or b in a)


# ---------------------------------------------------------------------------------------------
# Reference numerals - the single most-broken thing in a machine-drafted application
# ---------------------------------------------------------------------------------------------
def write_numerals(workspace: Path, numerals: Sequence[Mapping[str, Any]]) -> None:
    rows = "\n".join(
        f"| {_clean(item.get('numeral'), 8)} | {_clean(item.get('part'), 200)} |"
        for item in numerals if _clean(item.get("numeral"), 8))
    _write(Path(workspace) / "draft" / NUMERALS_FILE,
           "# Reference numerals\n\n"
           "| Numeral | Part |\n| --- | --- |\n" + (rows or "| | |"))


def read_numerals(workspace: Path) -> list[dict[str, str]]:
    path = Path(workspace) / "draft" / NUMERALS_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, str]] = []
    for line in raw.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not _NUMERAL_CELL_RE.fullmatch(cells[0]):
            continue
        numeral, part = cells[0], cells[1]
        if not numeral or part.lower() in ("part", "---", "") or set(part) <= {"-", " "}:
            continue
        out.append({"numeral": numeral, "part": part})
    return out


# ---------------------------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------------------------
def write_figures(workspace: Path, figures: Sequence[Mapping[str, Any]]) -> None:
    directory = Path(workspace) / "figures"
    directory.mkdir(parents=True, exist_ok=True)
    for existing in directory.iterdir():
        if existing.is_file() or existing.is_symlink():
            existing.unlink()
    for index, figure in enumerate(figures, 1):
        label = _clean(figure.get("label") or f"FIG. {index}", 240)
        slug = (re.sub(r"[^A-Za-z0-9]+", "-", label[:60]).strip("-").upper() or
                f"FIG-{index}")
        body = [f"# {label}", "", _clean(figure.get("caption"), 4000)]
        numerals = figure.get("numerals") or []
        if numerals:
            body += ["", "## Numerals shown on this figure", ""]
            body += [f"- {_clean(n, 200)}" for n in numerals]
        _write(directory / f"{slug}.md", "\n".join(body))


def read_figures(workspace: Path) -> list[dict[str, Any]]:
    directory = Path(workspace) / "figures"
    out = []
    for path in sorted(directory.glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = raw.splitlines()
        label = ""
        if lines and lines[0].startswith("#"):
            label = lines[0].lstrip("#").strip()
            lines = lines[1:]
        caption_lines, numerals, in_numerals = [], [], False
        for line in lines:
            if line.strip().startswith("#"):
                in_numerals = "numeral" in line.lower()
                continue
            if in_numerals and line.strip().lstrip("*").startswith(("-", "|")):
                numerals.append(line.strip().lstrip("-|").strip())
            elif not in_numerals:
                caption_lines.append(line)
        body = "\n".join(caption_lines).strip()
        if not numerals:
            #  A drawing brief written as prose rather than as a bullet list is the normal case, not
            #  a malformed one: the agent describes the view and names the parts inline. The check
            #  that matters - "is every numeral on this sheet defined in the table" - needs the
            #  numerals wherever they are, so they are read out of the whole file when no explicit
            #  list was given.
            import draft_qa
            numerals = sorted(draft_qa.numerals_used(raw), key=lambda n: int(re.sub(r"\D", "", n) or 0))
        out.append({"label": label or path.stem, "caption": body,
                    "numerals": numerals, "file": path.name})
    return out


# ---------------------------------------------------------------------------------------------
# Building the workspace
# ---------------------------------------------------------------------------------------------
def build(*, project: Mapping[str, Any], references: Sequence[Mapping[str, Any]] = (),
          documents: Sequence[Mapping[str, Any]] = (), sections: Mapping[str, str] | None = None,
          numerals: Sequence[Mapping[str, Any]] = (), figures: Sequence[Mapping[str, Any]] = (),
          conversation: Sequence[Mapping[str, Any]] = (), request: str = "",
          qa_report: Mapping[str, Any] | None = None, src_dir: str | Path | None = None) -> Path:
    """Lay out (or refresh) the workspace for one turn and return its path."""
    workspace = for_project(int(project["id"]))
    (workspace / "input").mkdir(parents=True, exist_ok=True)
    (workspace / "draft").mkdir(parents=True, exist_ok=True)
    (workspace / "prior_art").mkdir(parents=True, exist_ok=True)
    (workspace / "figures").mkdir(parents=True, exist_ok=True)
    (workspace / "review").mkdir(parents=True, exist_ok=True)

    _write(workspace / "input" / "brief.md", _brief(project))
    _write(workspace / "input" / "disclosure.md", _disclosure(project))
    _write(workspace / "input" / "conversation.md", _conversation(conversation))
    _write(workspace / "input" / "request.md", _clean(request, 60_000) or "(no request text)")
    _write_materials(workspace, documents)
    _write_prior_art(workspace, references, documents)
    _write_review(workspace, qa_report)
    install_tools(workspace, src_dir)

    if sections is not None:
        write_sections(workspace, sections)
    else:
        for _key, name, heading in SECTION_FILES:
            path = workspace / "draft" / name
            if not path.exists():
                _write(path, "")
    if numerals or not (workspace / "draft" / NUMERALS_FILE).exists():
        write_numerals(workspace, numerals)
    #  Written unconditionally, including empty: the workspace mirrors the stored version, so a
    #  figure deleted in the draft must not survive on disk and reappear in the next review.
    write_figures(workspace, figures)
    return workspace


def _brief(project: Mapping[str, Any]) -> str:
    lines = ["# Drafting brief", "",
             f"- Working title: {_clean(project.get('title'), 300) or '(none yet)'}",
             f"- Applicant: {_clean(project.get('applicant'), 300) or '(not supplied)'}",
             f"- Named inventors: {_clean(project.get('inventors'), 2000) or '(not supplied)'}"]
    kind = str(project.get("input_kind") or "description")
    lines.append("- Starting point: " + (
        "an existing draft the user already has, to be improved rather than replaced"
        if kind == "existing_draft" else
        "a description of the invention in the inventor's own words"))
    slug = _clean(project.get("search_slug"), 200)
    lines.append(f"- Prior-art search: {slug}" if slug else
                 "- Prior-art search: none was run. Work with whatever art is in prior_art/, and "
                 "say plainly in your summary that the art you were given may be incomplete.")
    notes = _clean(project.get("inventor_notes"), 40_000)
    if notes:
        lines += ["", "## Inventor and filing notes", "", notes]
    return "\n".join(lines)


def _disclosure(project: Mapping[str, Any]) -> str:
    kind = str(project.get("input_kind") or "description")
    header = ("# Source draft (the user's own)\n\nThis is the document the user brought. It is the "
              "authority for what the invention IS. Improve it; do not replace its subject "
              "matter.\n"
              if kind == "existing_draft" else
              "# Invention disclosure (the inventor's own words)\n\nThis is the ONLY authority for "
              "what the invention includes. Everything you claim must be supported here or in "
              "later messages from the user.\n")
    return header + "\n" + _clean(project.get("disclosure_text"), 400_000)


def _conversation(messages: Sequence[Mapping[str, Any]]) -> str:
    if not messages:
        return "# Conversation\n\n(this is the first turn)"
    lines = ["# Conversation so far", ""]
    for message in messages:
        role = str(message.get("role") or "user")
        who = {"user": "USER", "agent": "YOU (the drafting agent)",
               "qa": "REVIEWER", "system": "SYSTEM"}.get(role, role.upper())
        lines += [f"### {who}", "", _clean(message.get("body"), 12_000), ""]
    return "\n".join(lines)


def _write_materials(workspace: Path, documents: Sequence[Mapping[str, Any]]) -> None:
    directory = workspace / "input" / "materials"
    shutil.rmtree(directory, ignore_errors=True)
    materials = [d for d in documents if str(d.get("kind")) in ("material", "source_draft")]
    if not materials:
        return
    directory.mkdir(parents=True, exist_ok=True)
    for index, document in enumerate(materials, 1):
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(document.get("filename") or f"material-{index}"))
        header = [f"# {_clean(document.get('title') or document.get('filename'), 300)}", ""]
        if document.get("note"):
            header += [f"> {_clean(document.get('note'), 2000)}", ""]
        _write(directory / f"{index:02d}-{name}.md",
               "\n".join(header) + _clean(document.get("body"), MAX_DOCUMENT_CHARS))


def _write_prior_art(workspace: Path, references: Sequence[Mapping[str, Any]],
                     documents: Sequence[Mapping[str, Any]]) -> None:
    directory = workspace / "prior_art"
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    index_rows: list[str] = []
    budget = MAX_TOTAL_REFERENCE_CHARS

    for reference in references:
        publication = _clean(reference.get("publication_number"), 64)
        if not publication:
            continue
        snapshot = reference.get("snapshot") or {}
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except json.JSONDecodeError:
                snapshot = {}
        origin = str(reference.get("origin") or "report")
        body = _reference_body(reference, snapshot, origin)
        if budget <= 0:
            break
        body = body[:min(MAX_REFERENCE_CHARS, budget)]
        budget -= len(body)
        _write(directory / f"{publication}.md", body)
        index_rows.append(
            f"| [{publication}](./{publication}.md) | {_clean(reference.get('title'), 160)} | "
            f"{origin} |")

    for index, document in enumerate(
            [d for d in documents if str(d.get("kind")) == "prior_art"], 1):
        publication = _clean(document.get("publication_number"), 64) or f"UPLOAD-{index:02d}"
        title = _clean(document.get("title") or document.get("filename"), 300)
        body = "\n".join([
            f"# {publication} - {title}", "",
            "> Uploaded by the user. Cite it as `[REF:%s]`. It has NOT been ranked or read by the "
            "search pipeline, so nothing about its relevance is established beyond the user's own "
            "note." % publication, "",
            (f"**User's note:** {_clean(document.get('note'), 4000)}\n"
             if document.get("note") else ""),
            "## Document text", "", _clean(document.get("body"), MAX_REFERENCE_CHARS)])
        _write(directory / f"{publication}.md", body[:MAX_REFERENCE_CHARS])
        index_rows.append(f"| [{publication}](./{publication}.md) | {title} | upload |")

    header = [
        "# Prior art available to this draft", "",
        "These are the ONLY documents you may cite, and the citation form is `[REF:PUBLICATION]`",
        "using exactly the key in the first column. Never invent a citation, never cite a document",
        "that is not listed here, and never describe a reference beyond what its file actually",
        "says.", "",
        "| Key | Title | Where it came from |", "| --- | --- | --- |"]
    _write(directory / "INDEX.md", "\n".join(header + (index_rows or ["| (none) | | |"])))


def _reference_body(reference: Mapping[str, Any], snapshot: Mapping[str, Any],
                    origin: str) -> str:
    publication = _clean(reference.get("publication_number"), 64)
    provenance = {
        "report": "Ranked and read by the prior-art search for this project.",
        "manual": "Added by the user as a publication number and resolved against the corpus.",
        "agent": "Found by the drafting agent during an earlier turn.",
        "upload": "Uploaded by the user.",
    }.get(origin, origin)
    parts = [
        f"# {publication} - {_clean(reference.get('title'), 400)}", "",
        f"- Citation key: `[REF:{publication}]`",
        f"- Provenance: {provenance}",
    ]
    if reference.get("source_url"):
        parts.append(f"- Source: {_clean(reference.get('source_url'), 2048)}")
    for label, key in (("Publication date", "publication_date"), ("Filing date", "filing_date"),
                       ("Priority date", "priority_date"), ("Assignee", "assignee")):
        value = _clean(snapshot.get(key), 300)
        if value:
            parts.append(f"- {label}: {value}")
    why = _clean(reference.get("relevance_summary") or snapshot.get("relevance_summary"), 8000)
    if why:
        parts += ["", "## Why the search returned it", "", why]
    for label, key in (("Abstract", "abstract"), ("Claims", "claims"),
                       ("Passages the search matched", "prompt_context"),
                       ("Description", "description")):
        value = _clean(snapshot.get(key), MAX_REFERENCE_CHARS)
        if value:
            parts += ["", f"## {label}", "", value]
    return "\n".join(parts)


def _write_review(workspace: Path, qa_report: Mapping[str, Any] | None) -> None:
    path = workspace / "review" / "previous-qa.md"
    if not qa_report:
        _write(path, "# Previous review\n\n(no review has run yet)")
        return
    lines = [f"# Previous review - verdict: {qa_report.get('verdict', 'unknown')}", "",
             _clean(qa_report.get("summary"), 8000), ""]
    findings = qa_report.get("findings") or []
    checks = [c for c in (qa_report.get("checks") or []) if c.get("status") != "pass"]
    if checks:
        lines += ["## Mechanical checks that did not pass", ""]
        for check in checks:
            lines.append(f"- **{check.get('name')}** ({check.get('status')}): "
                         f"{_clean(check.get('detail'), 2000)}")
            for item in list(check.get("items") or ())[:60]:
                lines.append(f"  - {_clean(item, 2000)}")
        lines.append("")
    if findings:
        lines += ["## Reviewer findings", ""]
        for finding in findings:
            lines.append(
                f"- **[{finding.get('severity', 'minor')}] {_clean(finding.get('title'), 300)}** "
                f"({_clean(finding.get('where'), 120)}) - {_clean(finding.get('detail'), 3000)}")
            if finding.get("fix"):
                lines.append(f"  - Suggested fix: {_clean(finding.get('fix'), 2000)}")
        lines.append("")
    lines += ["Fix every listed item before returning. If an advisory is a false positive, make",
              "the wording or figure specification unambiguous enough that the check passes."]
    _write(path, "\n".join(lines))


def install_tools(workspace: Path, src_dir: str | Path | None = None) -> None:
    """Write the one command the agent is allowed to run.

    The corpus behind this product holds millions of publications with claims and description
    text.  Letting the drafting agent look one up on demand is the difference between "step around
    the art you were handed" and "step around the art that exists": when it is about to write a
    limitation, it can check what the closest reference actually says instead of relying on the
    fragment the search happened to quote.
    """
    src = Path(src_dir) if src_dir else Path(__file__).resolve().parent
    directory = workspace / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "patent_lookup.py").write_text(
        _LOOKUP_TOOL.replace("__SRC_DIR__", json.dumps(str(src))), encoding="utf-8")


_LOOKUP_TOOL = '''#!/usr/bin/env python3
"""Look a publication up in the local patent corpus.

    python3 tools/patent_lookup.py US-9108319-B2            full record
    python3 tools/patent_lookup.py US-9108319-B2 --claims    claims only
    python3 tools/patent_lookup.py --check US-9108319-B2 EP-1234567-A1 ...   do these exist?

Prints plain text.  Anything not in the corpus is reported as NOT FOUND rather than guessed at.
"""
import sys, json

sys.path.insert(0, __SRC_DIR__)


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    try:
        import draft_cite
    except Exception as exc:                                    # noqa: BLE001
        print("lookup unavailable: %s: %s" % (type(exc).__name__, exc))
        return 1
    if argv[0] in ("--check", "-c"):
        for pub in argv[1:][:40]:
            found = draft_cite.resolve(pub)
            print("%-24s %s  %s" % (pub, "FOUND    " if found.get("found") else "NOT FOUND",
                                    found.get("title") or found.get("reason") or ""))
        return 0
    pub = argv[0]
    want_claims = "--claims" in argv
    record = draft_cite.resolve(pub, with_text=True)
    if not record.get("found"):
        print("NOT FOUND: %s (%s)" % (pub, record.get("reason") or "not in corpus"))
        return 0
    print("PUBLICATION %s" % record.get("publication_number"))
    for key in ("title", "publication_date", "filing_date", "priority_date", "assignee", "url"):
        if record.get(key):
            print("%-18s %s" % (key + ":", record[key]))
    if record.get("abstract") and not want_claims:
        print("\\nABSTRACT\\n%s" % record["abstract"])
    if record.get("claims"):
        print("\\nCLAIMS\\n%s" % record["claims"][:60000])
    elif want_claims:
        print("\\n(no claim text held for this publication)")
    if record.get("description") and not want_claims:
        print("\\nDESCRIPTION (first part)\\n%s" % record["description"][:40000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


# ---------------------------------------------------------------------------------------------
# Seeding from a draft the user already has
# ---------------------------------------------------------------------------------------------
_SEED_HEADINGS = (
    ("cross_reference", r"cross[- ]?reference|related applications?|priority claim"),
    ("field", r"technical field|field of (the )?(invention|disclosure)|^field$"),
    ("background", r"background|prior art|description of (the )?related art"),
    ("summary", r"summary|brief summary"),
    ("drawing_descriptions", r"brief description of (the )?(drawings?|figures?)|description of "
                             r"(the )?(drawings?|figures?)"),
    ("detailed_description", r"detailed description|description of (the )?(preferred|example|"
                             r"exemplary|illustrative)"),
    ("claims", r"^what is claimed|^i claim|^we claim|^claims?$|^the claims"),
    ("abstract", r"^abstract"),
)


def seed_sections_from_document(text: str) -> dict[str, str]:
    """Best-effort split of an existing application into our section keys.

    Deliberately conservative.  Anything it cannot place with confidence goes to
    ``detailed_description`` rather than being dropped, and the agent is told in its prompt that
    this split was mechanical and may be wrong - so a mis-split shows up as something to fix on
    turn one instead of as silently lost text.
    """
    body = _clean(text, 400_000)
    if not body:
        return {}
    lines = body.splitlines()
    marks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = re.sub(r"^[\s#*_>\d.\)\[\]-]+", "", line).strip().rstrip(":.")
        if not stripped or len(stripped) > 90:
            continue
        # A heading is short and mostly not a sentence; require it to sit on its own line.
        for key, pattern in _SEED_HEADINGS:
            if re.search(pattern, stripped, re.IGNORECASE):
                marks.append((index, key))
                break
    if not marks:
        return {"detailed_description": body}

    sections: dict[str, list[str]] = {}
    preamble = "\n".join(lines[:marks[0][0]]).strip()
    for position, (index, key) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        chunk = "\n".join(lines[index + 1:end]).strip()
        if chunk:
            sections.setdefault(key, []).append(chunk)
    out = {key: "\n\n".join(value).strip() for key, value in sections.items()}
    if preamble:
        # The text above the first heading is nearly always the title, sometimes with a header
        # block.  Take the first non-empty line as the title and keep the rest.
        first = next((line.strip() for line in preamble.splitlines() if line.strip()), "")
        if first and len(first) <= 300:
            out.setdefault("title", first.lstrip("# ").strip())
            rest = preamble.replace(first, "", 1).strip()
            if rest:
                out["detailed_description"] = (rest + "\n\n" +
                                               out.get("detailed_description", "")).strip()
        else:
            out["detailed_description"] = (preamble + "\n\n" +
                                           out.get("detailed_description", "")).strip()
    return out


#  Words that can sit between a part name and its numeral without being part of the name.  Without
#  the verbs, "…has a body 12" harvests "has body" as the name of part 12.
_NOT_PART_WORDS = frozenset("""
a an the of and or to in on at by with from into onto between through over under for
is are was were be been being has have had having comprises comprising includes including
contains containing carries carrying defines defining shown shows said each wherein
figure figures fig figs claim claims page step steps element portion
""".split())


def numerals_from_sections(sections: Mapping[str, str]) -> list[dict[str, str]]:
    """Harvest an initial numeral table from prose of the form "a suction cup 10".

    A first pass over a user's existing draft, so the agent starts from what the document already
    uses rather than renumbering everything.  Only the words immediately before the numeral are
    taken, which is what a reference numeral labels; anything longer is a sentence, not a part.
    """
    text = "\n".join(str(sections.get(key) or "") for key, _n, _h in SECTION_FILES
                     if key not in ("claims", "abstract"))
    found: dict[str, str] = {}
    for match in re.finditer(r"([A-Za-z][A-Za-z\- ]{2,60}?)\s+(\d{1,4}[a-z]?)\b", text):
        phrase, numeral = match.group(1).strip(), match.group(2)
        if numeral in found:
            continue
        words = [w for w in re.split(r"\s+", phrase) if w]
        words = [w for w in words[-4:] if w.lower() not in _NOT_PART_WORDS]
        if not words:
            continue
        found[numeral] = " ".join(words).lower()
    return [{"numeral": numeral, "part": part}
            for numeral, part in sorted(found.items(), key=lambda kv: int(re.sub(r"\D", "", kv[0]) or 0))]


def snapshot(workspace: Path) -> dict[str, Any]:
    """Everything the application layer needs to persist after a turn."""
    return {"sections": read_sections(workspace), "numerals": read_numerals(workspace),
            "figures": read_figures(workspace)}


def iter_section_texts(sections: Mapping[str, str]) -> Iterable[tuple[str, str, str]]:
    for key, _name, heading in SECTION_FILES:
        yield key, heading, str(sections.get(key) or "")
