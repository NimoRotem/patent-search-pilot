"""A second reader for the finished package, with no memory of how it was written.

WHY A SEPARATE AGENT, AND WHY IT KNOWS NOTHING.  The drafting agent has spent an hour deciding
what this application says.  Ask it to review its own package and it re-approves its own
reasoning: it knows why FIG. 3 shows two arrangements, so it does not see that FIG. 3 shows two
arrangements.  Every defect that reached a real filing was of exactly that kind - obvious to a
stranger, invisible to the author.

So this runs as its own Claude Code process, with:

    a private CLAUDE_CONFIG_DIR      no memory of any other draft on this host
    --safe-mode                     no CLAUDE.md discovery, no skills, plugins, hooks or MCP
    --setting-sources ""            no user, project or local settings
    a read-only tool set            Read, Glob, Grep, and one command
    its own system prompt           it is a filing clerk, not the author

and a working directory that contains the PACKAGE rather than the draft: the .docx that will be
uploaded, the drawing sheets as they will be scanned, the declaration somebody will sign.  It is
told what the deterministic audit already found, so it spends its attention on the half a
mechanical check cannot reach: does the drawing agree with the words, does a cross-reference point
at the view that shows the thing, is a claimed feature actually drawn.

WHAT IT IS NOT.  It is not a patentability opinion and it does not rewrite anything.  It reports.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Mapping, Sequence

import draft_agent
import filing_pack
import filing_rules

QA_ROOT = os.environ.get("FILING_QA_ROOT", "")
MODEL = os.environ.get("FILING_QA_MODEL", "opus")
#  The drafting studio's own subscription, the same one the interactive terminal spends, so a
#  filing review is billed where the rest of this feature's model time is billed. Falls back to
#  the host default when that file does not exist.
TOKEN_FILE = os.environ.get("FILING_QA_OAUTH_TOKEN_FILE", "")
TIMEOUT = max(300, int(os.environ.get("FILING_QA_TIMEOUT", "1800")))
MAX_USD = float(os.environ.get("FILING_QA_MAX_USD", "8"))
CHECK_COMMAND = "python3 tools/check_pack.py"
#  Listing a directory has to be allowed, or a refused `ls` reads to the agent as an empty
#  directory. That is not a hypothetical: it is what the first run of this reviewer did.
LIST_COMMAND = "ls"

_RUNNING: dict[int, float] = {}
_LOCK = threading.Lock()

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings", "checked"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["file_it", "fix_first", "do_not_file"],
            "description": "file_it when you found nothing that would be objected to. fix_first "
                           "when everything you found is correctable without redrafting. "
                           "do_not_file when the papers are defective as they stand."},
        "summary": {"type": "string",
                    "description": "Three sentences at most. What you read, what you found, what "
                                   "the person should do next."},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "rule", "where", "title", "detail", "fix", "evidence"],
                "properties": {
                    "severity": {"type": "string",
                                 "enum": ["blocker", "formality", "note"]},
                    "rule": {"type": "string",
                             "description": "The paragraph it comes from, such as "
                                            "'37 CFR 1.84(u)'. Use 'internal consistency' where "
                                            "no rule is broken but the papers disagree."},
                    "where": {"type": "string",
                              "description": "The file, and the sheet or paragraph number."},
                    "title": {"type": "string", "description": "One line."},
                    "detail": {"type": "string",
                               "description": "What is wrong. Name the numeral, the figure, the "
                                              "claim or the paragraph."},
                    "fix": {"type": "string",
                            "description": "The change that resolves it, specifically enough to "
                                           "act on without deciding anything."},
                    "evidence": {"type": "string",
                                 "description": "What you actually read that establishes it. "
                                                "Quote the text, or say which sheet you looked "
                                                "at and what is on it."},
                },
            },
        },
        "checked": {
            "type": "array", "items": {"type": "string"},
            "description": "One line per thing you verified and found correct. This is how the "
                           "reader knows what was covered, so name the file and the check."},
    },
}

SYSTEM_PROMPT = """You are the final pre-filing reviewer for a United States utility patent
application. The package in your working directory is about to be uploaded to Patent Center. You
did not write any of it and you have no memory of how it was written, which is the point: you are
the stranger who reads it cold.

YOUR JOB. Find what would be refused at upload, objected to by the Office, or would embarrass the
applicant. Nothing else. You do not assess patentability, you do not rewrite, and you do not
comment on style.

WHAT HAS ALREADY BEEN CHECKED MECHANICALLY, so do not spend your run repeating it. AUDIT.txt lists
what a deterministic pass found and did not find: page size, margins, type size and line spacing,
paragraph numbering, the claims and abstract commencing on their own pages, non-Latin characters,
DOCX package parts, PDF font embedding, the required statements in the declaration, the ADS
section headings, the claim counts, and the reference character height on every sheet. Read it,
trust it, and go past it. You may re-run it with:

    python3 tools/check_pack.py

WHERE THE REAL DEFECTS ARE. Every one of these reached a real filing and was found by a human:

1. A FEATURE THE CLAIMS RECITE THAT NO SHEET SHOWS. 37 CFR 1.83(a). Read the independent claims,
   list every physical element they recite, and confirm each one is drawn and numbered. A port
   recited in three independent claims as being in fluid communication with the chamber carried no
   numeral and appeared in no view.
2. A VIEW WITH NO NUMBER. 37 CFR 1.84(u). Open every sheet image. A magnified circle, a second
   arrangement beside the first, anything separated by the word OR: each is a view and each needs
   its own number and its own sentence in the Brief Description.
3. A CROSS-REFERENCE THAT POINTS AT THE WRONG VIEW. "As shown in FIG. 3" in a paragraph about a
   part that only FIG. 2 shows. These multiply every time figures are renumbered or split.
4. THE DRAWING AND THE TEXT DISAGREEING ABOUT WHAT A NUMERAL MEANS. A pair swapped on one sheet,
   14 and 16, against a specification that defines 14 as the first side. The text was
   self-consistent, so nothing that reads only the text could find it. You have the sheets: look.
5. TEXT ON A SHEET THAT DOES NOT BELONG THERE. 37 CFR 1.84(o). A reference numeral key table, a
   words-label duplicating a numeral, a leader left pointing at nothing.
6. A DRAWING DESCRIPTION THAT HEDGES. A view is described in the present tense as what it is.
   Nothing in a specification "may" illustrate anything.
7. ANY PAPER THAT CONTRADICTS ANOTHER. The title on the ADS against the title on the
   specification. The inventor names on the declaration against the ADS. The drawing sheet count
   on the ADS against the number of sheets in the drawings PDF.

HOW TO WORK. Read the package. Open the sheet images in pack/figures and look at them; that is
where half of these live. Compare what you see against draft/ and against the specification text
in pack-text/. Ground every finding in something you actually read: a finding whose evidence is
"typically" or "usually" is a finding you should not report.

SEVERITY. blocker means the paper is defective or would be refused as filed. formality means the
Office will object and the filing date stands. note means worth knowing and not a defect. A
package with no blockers is filable, and saying so plainly is as much your job as finding a
defect. Do not invent work.
"""

PROMPT = """Review the filing package in this directory.

MANIFEST.txt lists every file that exists here, with its size. Read it first. A file on that list
is present: if you cannot open one, that is a fault in the tooling and you report it as such,
never as a missing document.

Then, in this order, and do not stop before the end:

  1. README.txt and AUDIT.txt, so you know what has already been checked.
  2. pack-text/01-Specification.txt, the whole document.
  3. draft/09-claims.md and draft/numerals.md. List the physical elements the independent claims
     recite and the numeral each one has.
  4. EVERY image file under pack/figures. Open each one with Read and look at it. This is the
     half of the review that only you can do, and a review that skips it is worth nothing. For
     each sheet, write down: which views are on it, which numerals you can see, what each numeral
     points at, and any words printed on the sheet.
  5. draft/07-drawings.md against what you just saw on the sheets.
  6. draft/08-detailed-description.md: every "as shown in FIG. N" against the numerals that
     sentence uses and the view that actually shows them.
  7. reconciliation.json, which is a machine reading of the same sheets. Agree or disagree with
     it, and say which.
  8. pack-text/03-Application-Data-Sheet.txt and pack-text/04-Declaration.txt against each other
     and against the specification: title, inventor names, sheet count.

Report through the structured output. Every finding needs evidence you actually read. In
`checked`, name each of the eight steps and what it established.
"""

_CHECK_TOOL = '''#!/usr/bin/env python3
"""Re-run the deterministic filing audit over the files in pack/.

    python3 tools/check_pack.py

Prints the same findings AUDIT.txt was built from, so you can confirm nothing has changed and see
the exact rule behind each one.
"""
import json
import os
import sys

#  RE-EXEC UNDER THE SERVER'S OWN INTERPRETER. `python3` on this box is the system one and does
#  not have this application's dependencies, so the import below failed on ModuleNotFoundError
#  and the reviewer reported, correctly, that it could not re-run the audit at all.
_PYTHON = __PYTHON__
if os.path.abspath(sys.executable) != os.path.abspath(_PYTHON) and os.path.exists(_PYTHON):
    os.execv(_PYTHON, [_PYTHON, os.path.abspath(__file__)] + sys.argv[1:])

sys.path.insert(0, __SRC_DIR__)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    import filing_pack
    import filing_rules
    pack = os.path.join(HERE, "pack")
    findings = []

    def read(name):
        path = os.path.join(pack, name)
        with open(path, "rb") as handle:
            return handle.read()

    names = sorted(os.listdir(pack))
    for name in names:
        if name.endswith(".docx"):
            findings += filing_rules.audit_specification_docx(read(name), where=name)
        elif name.endswith(".pdf"):
            findings += filing_rules.audit_pdf(read(name), where=name)
            text = filing_pack._pdf_text(read(name))
            if "declaration" in name.lower():
                findings += filing_rules.audit_declaration_text(text, where=name)
            if "data-sheet" in name.lower():
                findings += filing_rules.audit_ads_text(text, where=name)

    stored = os.path.join(HERE, "reconciliation.json")
    if os.path.exists(stored):
        with open(stored, "r", encoding="utf-8") as handle:
            findings += json.load(handle)

    print(filing_pack.audit_text(findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# =============================================================================================
# The workspace the reviewer reads
# =============================================================================================
def root() -> Path:
    """Its own tree, one level below the config directory, so the reviewer's Claude configuration
    is separate from the drafting agent's and starts empty."""
    if QA_ROOT:
        base = Path(QA_ROOT)
    else:
        import draft_workspace
        base = draft_workspace.root() / "filing-qa"
    base.mkdir(parents=True, exist_ok=True)
    return base


def workspace_for(project_id: int) -> Path:
    return root() / f"p{int(project_id)}"


def prepare(*, project_id: int, built: Mapping[str, Any],
            sections: Mapping[str, str],
            numerals: Sequence[Mapping[str, str]] = (),
            figures: Sequence[Mapping[str, Any]] = (),
            reconciliation: Sequence[Mapping[str, Any]] = (),
            src_dir: str | Path | None = None) -> Path:
    """Lay the package out for a reader who has never seen this application."""
    workspace = workspace_for(project_id)
    shutil.rmtree(workspace, ignore_errors=True)
    (workspace / "pack").mkdir(parents=True, exist_ok=True)
    (workspace / "pack-text").mkdir(parents=True, exist_ok=True)
    (workspace / "pack" / "figures").mkdir(parents=True, exist_ok=True)
    (workspace / "draft").mkdir(parents=True, exist_ok=True)
    (workspace / "tools").mkdir(parents=True, exist_ok=True)

    for name, blob in dict(built.get("files") or {}).items():
        (workspace / "pack" / name).write_bytes(blob)
        if name.endswith(".docx"):
            (workspace / "pack-text" / (name[:-5] + ".txt")).write_text(
                _docx_text(blob), encoding="utf-8")
        elif name.endswith(".pdf"):
            (workspace / "pack-text" / (name[:-4] + ".txt")).write_text(
                filing_pack._pdf_text(blob), encoding="utf-8")

    for index, figure in enumerate(figures, 1):
        png = bytes(figure.get("png") or b"")
        if not png:
            continue
        label = str(figure.get("label") or f"sheet {index}")
        safe = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-") or "sheet"
        (workspace / "pack" / "figures" / f"uploaded-{index:02d}-{safe}.png").write_bytes(png)

    import draft_workspace
    for key, name, _heading in draft_workspace.SECTION_FILES:
        (workspace / "draft" / name).write_text(
            str(sections.get(key) or "").strip() + "\n", encoding="utf-8")
    (workspace / "draft" / "numerals.md").write_text(
        "# Reference numerals\n\n| Numeral | Part |\n| --- | --- |\n" + "\n".join(
            f"| {row.get('numeral')} | {row.get('part')} |" for row in numerals) + "\n",
        encoding="utf-8")

    (workspace / "reconciliation.json").write_text(
        json.dumps([dict(item) for item in reconciliation], ensure_ascii=False, indent=1),
        encoding="utf-8")
    (workspace / "AUDIT.txt").write_text(
        filing_pack.audit_text(list(built.get("findings") or [])), encoding="utf-8")
    (workspace / "README.txt").write_text(_readme(built, figures), encoding="utf-8")

    src = Path(src_dir) if src_dir else Path(__file__).resolve().parent
    (workspace / "tools" / "check_pack.py").write_text(
        _CHECK_TOOL.replace("__SRC_DIR__", json.dumps(str(src)))
                   .replace("__PYTHON__", json.dumps(sys.executable)), encoding="utf-8")
    #  WRITTEN LAST, and it matters. The first run of this reviewer reported the drawings as
    #  missing from an application that had them: it tried to list a directory, the command was
    #  not on its allow-list, the refusal read as an empty directory, and it filed a blocker
    #  against the applicant for a fault in the harness. An agent must never have to guess what
    #  exists.
    (workspace / "MANIFEST.txt").write_text(_manifest(workspace), encoding="utf-8")
    return workspace


def _manifest(workspace: Path) -> str:
    lines = ["EVERY FILE IN THIS DIRECTORY", "",
             "If it is on this list it is present and readable. A file you cannot open is a",
             "tooling fault to report as such, never a missing document.", ""]
    for path in sorted(Path(workspace).rglob("*")):
        if path.is_dir() or path.name == "MANIFEST.txt":
            continue
        lines.append(f"  {str(path.relative_to(workspace)):<52} {path.stat().st_size:>10} bytes")
    return "\n".join(lines) + "\n"


def _docx_text(blob: bytes) -> str:
    import io
    try:
        from docx import Document
        return "\n".join(paragraph.text for paragraph in Document(io.BytesIO(blob)).paragraphs)
    except Exception:                                              # noqa: BLE001
        return ""


def _readme(built: Mapping[str, Any], figures: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "WHAT IS IN THIS DIRECTORY", "",
        "pack/          the files that would be uploaded to Patent Center, exactly as built.",
        "pack-text/     the text of each of them, extracted, so you can grep and quote it.",
        "pack/figures/  the drawing sheets AS THE USER UPLOADED THEM. Open these and look.",
        "               pack/02-Drawings.pdf is what actually gets filed, and it is not always",
        "               the same thing: where one upload carried several views, each view is cut",
        "               onto its own sheet. Read both.",
        "draft/         the specification section by section, and the reference numeral table.",
        "AUDIT.txt      what the deterministic audit found. Read it before you start.",
        "reconciliation.json  what a machine inspection found ON THE UPLOADED ARTWORK, not on",
        "               the filing sheets. Treat it as a reading, not as proof: it is a model",
        "               looking at a picture. Where you disagree with it, say so and say what",
        "               you saw.",
        "tools/check_pack.py  re-runs the deterministic audit.",
        "",
        f"Sheets uploaded: {len(figures)}.",
        "Sheets in the filing package: " + ", ".join(
            f"{item.get('label') or 'unnumbered'}" for item in built.get("sheets") or []) or
        "none",
        "",
        "Reference character height measured on each filing sheet:",
    ]
    for item in built.get("measurements") or []:
        lines.append(f"  {item.get('sheet_number')}  {item.get('label') or 'unnumbered'}  "
                     f"{item.get('character_cm', 0):.2f} cm  "
                     f"(floor is 0.32 cm under 37 CFR 1.84(p)(3))")
    return "\n".join(lines) + "\n"


# =============================================================================================
# Running it
# =============================================================================================
def available() -> dict[str, Any]:
    """Whether a review can be attempted here, against the credential it would actually use."""
    state = dict(draft_agent.availability())
    path = token_file()
    if path:
        state["auth"] = bool(draft_agent._oauth_token(path))
        state["ok"] = bool(state.get("binary")) and state["auth"]
        if not state["auth"]:
            state["reason"] = f"The drafting subscription token at {path} is unreadable."
        state["token_file"] = path
    return state


def running(project_id: int) -> bool:
    with _LOCK:
        return int(project_id) in _RUNNING


def report_path(project_id: int) -> Path:
    return workspace_for(project_id) / "report.json"


def latest(project_id: int) -> dict[str, Any] | None:
    try:
        return json.loads(report_path(project_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def token_file() -> str:
    """The credential this review spends, or '' meaning the host default.

    Resolved through ``draft_terminal.TOKEN_FILES``, which is the same list the interactive
    drafting agent uses and which carries an absolute path as its last candidate. Trusting
    ``Path.home()`` alone is not enough: this host has two homes, and a process started with the
    wrong one silently fell through to the metered API key that was out of quota.
    """
    if TOKEN_FILE and Path(TOKEN_FILE).exists():
        return TOKEN_FILE
    try:
        import draft_terminal
        for candidate in draft_terminal.TOKEN_FILES:
            if candidate and Path(candidate).exists():
                return candidate
    except Exception:                                              # noqa: BLE001
        pass
    return ""


def review(workspace: Path, *, model: str = "", timeout: int = 0) -> dict[str, Any]:
    """One review pass. Blocking; callers that serve a request run it on a thread."""
    started = time.time()
    run = draft_agent.run(
        workspace=Path(workspace), prompt=PROMPT, system_prompt=SYSTEM_PROMPT,
        schema=REPORT_SCHEMA, model=draft_agent.normalize_model(model) or MODEL,
        tools="Read,Glob,Grep,Bash", timeout=timeout or TIMEOUT,
        allowed_bash=(CHECK_COMMAND, LIST_COMMAND), max_budget_usd=MAX_USD,
        transcript=Path(workspace) / "transcript.jsonl",
        #  NO VERTEX FALLBACK. That path is a hand-built tool layer that reads text under six
        #  named drafting directories and cannot open an image at all. Run this review on it and
        #  it sees none of the package and none of the sheets, and returns "do not file" about an
        #  application it never read. Measured, once, exactly that way.
        vertex_fallback=False, oauth_token_file=token_file())
    if not run.ok:
        return {"status": "failed", "error": run.error or "The reviewer returned no report.",
                "duration_ms": int((time.time() - started) * 1000)}
    result = dict(run.result or {})
    findings = [dict(item) for item in (result.get("findings") or [])]
    return {
        "status": "complete",
        "verdict": str(result.get("verdict") or "fix_first"),
        "summary": str(result.get("summary") or ""),
        "findings": findings,
        "checked": [str(item) for item in (result.get("checked") or [])][:60],
        "counts": {
            "blocker": sum(1 for item in findings if item.get("severity") == "blocker"),
            "formality": sum(1 for item in findings if item.get("severity") == "formality"),
            "note": sum(1 for item in findings if item.get("severity") == "note"),
        },
        "model": run.model,
        "cost_usd": round(float(run.cost_usd or 0.0), 4),
        "duration_ms": int((time.time() - started) * 1000),
        "finished_at": time.time(),
    }


def start(*, project_id: int, built: Mapping[str, Any], sections: Mapping[str, str],
          numerals: Sequence[Mapping[str, str]] = (),
          figures: Sequence[Mapping[str, Any]] = (),
          reconciliation: Sequence[Mapping[str, Any]] = (),
          model: str = "", on_done=None) -> dict[str, Any]:
    """Prepare the workspace and review it on a background thread.

    In the background because a careful read of a whole package is minutes of model time, and
    holding a request thread open for it is a request some proxy eventually gives up on.
    """
    project_id = int(project_id)
    with _LOCK:
        if project_id in _RUNNING:
            raise RuntimeError("A filing review of this draft is already running.")
        _RUNNING[project_id] = time.time()
    workspace = prepare(project_id=project_id, built=built, sections=sections,
                        numerals=numerals, figures=figures, reconciliation=reconciliation)
    report_path(project_id).write_text(json.dumps({
        "status": "running", "started_at": time.time(),
        "audit": [dict(item) for item in (built.get("findings") or [])],
        "audit_verdict": built.get("verdict", ""),
    }), encoding="utf-8")

    def _run() -> None:
        try:
            report = review(workspace, model=model)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            report = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        report["audit"] = [dict(item) for item in (built.get("findings") or [])]
        report["audit_verdict"] = built.get("verdict", "")
        try:
            report_path(project_id).write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8")
        except OSError:
            traceback.print_exc()
        with _LOCK:
            _RUNNING.pop(project_id, None)
        if on_done:
            try:
                on_done(report)
            except Exception:                                      # noqa: BLE001
                traceback.print_exc()

    threading.Thread(target=_run, name=f"filing-qa-{project_id}", daemon=True).start()
    return {"queued": True, "workspace": str(workspace)}


def combined_verdict(built: Mapping[str, Any], report: Mapping[str, Any] | None) -> str:
    """One answer from the two readers, and the stricter one wins."""
    mechanical = filing_rules.verdict(list(built.get("findings") or []))
    if not report or report.get("status") != "complete":
        return mechanical
    if report.get("verdict") == "do_not_file" or (report.get("counts") or {}).get("blocker"):
        return "not ready"
    if mechanical == "ready" and report.get("verdict") == "file_it":
        return "ready"
    return mechanical if mechanical != "ready" else \
        "ready, with formalities the Office may object to"
