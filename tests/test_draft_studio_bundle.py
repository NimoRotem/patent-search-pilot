"""Boot the studio's JavaScript in node against a stub DOM and see whether it survives.

WHY THIS EXISTS. The studio bundle is 2,600 lines with no build step and no module system: every
function shares one closure, so deleting a block takes its constants with it and nothing says so
until a browser hits the line. That is not hypothetical. Replacing the conversation panel with the
terminal removed the `VERDICT` table three functions used, and every Python test still passed, the
page still rendered its shell, and the whole thing died on `renderChrome` with an empty draft pane
and a terminal stuck on "Starting the drafting agent…". A screenshot found it; nothing else could.

So this loads the real file, runs its IIFE, and fails on the first ReferenceError. It is not a
rendering test - there is no layout here and no real DOM - it is the one cheap check that every
name the boot path reaches actually exists.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "static" / "draft_studio.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed on this host")

#  Enough of a studio payload for every render function to have something to walk. Shapes, not
#  contents: a section list, one version, one figure, one reference.
PAYLOAD = {
    "ok": True,
    "project": {"id": 7, "title": "A clamp", "status": "ready", "revision": 3,
                "latest_version_no": 2, "search_slug": "", "input_kind": "description",
                "applicant": "", "inventors": "", "draft_model": "claude-sonnet-5",
                "disclosure_excerpt": "a clamp", "disclosure_chars": 7},
    "messages": [], "turns": [], "active_turn": None,
    "version": {"version_no": 2, "sections": {"title": "A clamp", "claims": "1. A clamp."},
                "citations": [], "change_note": "first", "created_at": "2026-08-30T09:00:00"},
    "versions": [{"version_no": 2, "status": "ready", "created_at": "2026-08-30T09:00:00",
                  "change_note": "first", "origin": "agent", "verdict": "warn"}],
    "qa": {"id": 4, "verdict": "warn", "summary": "checked", "version_no": 2,
           "model_name": "the deterministic checks", "counts": {"checks_failed": 1},
           "checks": [{"name": "Every section is written", "status": "fail",
                       "detail": "The Abstract is empty.", "items": ["Abstract"]}],
           "findings": []},
    "references": [{"publication_number": "US-9108319-B2", "title": "Prior", "origin": "report",
                    "url": "https://example.invalid", "rank": 1}],
    "documents": [{"id": 3, "filename": "brief.pdf", "kind": "prior_art", "title": "Brief",
                   "note": "", "publication_number": None, "chars": 900}],
    "sections": [{"key": "title", "heading": "Title"}, {"key": "claims", "heading": "Claims"}],
    "figures": [{"label": "FIG. 1", "caption": "side view", "numerals": ["10"],
                 "expected_numerals": ["10"], "uploaded": True, "figure_id": 9,
                 "active_version": 2, "n_versions": 2,
                 "versions": [{"version_no": 1, "created_at": ""},
                              {"version_no": 2, "created_at": ""}]},
                {"label": "FIG. 2", "caption": "end view", "numerals": [],
                 "expected_numerals": [], "uploaded": False, "figure_id": None,
                 "active_version": None, "n_versions": 0, "versions": []}],
    "searches": [],
    "agent": {"available": True, "reason": "", "status": "idle", "detail": "", "running": True,
              "exists": True, "pane_width": 132, "pane_total": 40,
              "models": [{"id": "claude-sonnet-5", "label": "Sonnet 5"}],
              "efforts": [{"id": "high", "label": "High"}],
              "default_model": "claude-opus-5", "default_effort": "high"},
}

HARNESS = r"""
// A DOM that answers every question with the same shape rather than a real tree. The studio only
// needs elements it can set textContent/innerHTML on and hang listeners off; what it renders is
// not what this is checking.
const PAYLOAD = __PAYLOAD__;
const failures = [];
const seen = new Set();

function makeEl(id) {
  const el = {
    id, textContent: '', innerHTML: '', value: '', title: '', hidden: false, disabled: false,
    scrollTop: 0, scrollHeight: 100, clientHeight: 50, clientWidth: 900, offsetTop: 0,
    dataset: { project: '7', pane: 'draft', key: 'title', kind: 'revise' },
    style: {}, children: [], files: [], parentNode: null, nextSibling: null,
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    addEventListener(){}, removeEventListener(){}, appendChild(){}, insertBefore(){},
    removeChild(){}, remove(){}, focus(){}, click(){}, scrollIntoView(){}, setAttribute(){},
    getAttribute(){ return ''; }, closest(){ return makeEl('closest'); },
    getBoundingClientRect(){ return { top: 0, left: 0, width: 900, height: 400, bottom: 400 }; },
    querySelector(){ return makeEl('q'); },
    querySelectorAll(){ return []; },
    insertAdjacentHTML(){},
  };
  el.parentNode = { insertBefore(){}, removeChild(){} };
  return el;
}

const stateEl = makeEl('studioState');
stateEl.textContent = JSON.stringify(PAYLOAD);

global.window = {
  APP_BASE: '', CSRF_TOKEN: 'x',
  addEventListener(){}, removeEventListener(){},
  localStorage: { getItem(){ return null; }, setItem(){}, removeItem(){} },
  confirm(){ return false; }, alert(){}, performance: { now: () => 0 },
  getComputedStyle(){ return { paddingLeft: '0px', paddingRight: '0px' }; },
  SpeechRecognition: null, webkitSpeechRecognition: null,
};
global.localStorage = window.localStorage;
global.getComputedStyle = window.getComputedStyle;
global.location = { hash: '#/draft', origin: 'http://x' };
window.location = global.location;
global.document = {
  documentElement: { lang: 'en' },
  body: { classList: { add(){}, remove(){}, toggle(){} }, contains(){ return false; } },
  getElementById(id) { seen.add(id); return id === 'studioState' ? stateEl : makeEl(id); },
  querySelector(sel) { return sel === '.studio' ? makeEl('studio') : makeEl(sel); },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(tag); },
  createDocumentFragment() { return makeEl('frag'); },
  addEventListener(){}, removeEventListener(){},
  getSelection() { return { rangeCount: 0 }; },
};
global.fetch = () => new Promise(() => {});          // never settles: no async paths run
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.clearInterval = () => {};
global.clearTimeout = () => {};

process.on('uncaughtException', (e) => failures.push(String(e && e.stack || e)));
try {
  require('__BUNDLE__');
} catch (e) {
  failures.push(String(e && e.stack || e));
}
console.log(JSON.stringify({ failures, seen: [...seen] }));
"""


def _boot() -> dict:
    harness = HARNESS.replace("__PAYLOAD__", json.dumps(PAYLOAD)).replace(
        "__BUNDLE__", str(BUNDLE))
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_bundle_boots_without_a_reference_error():
    """Every name the boot path reaches exists.

    `VERDICT` did not, for one deploy: it was declared at the top of the conversation panel and
    went out with it, and renderChrome threw on the first paint. The page still showed its shell,
    so the failure looked like "the terminal is slow to load" rather than "the script is dead".
    """
    out = _boot()
    assert out["failures"] == [], out["failures"][0][:1500]


def test_the_boot_path_touches_the_terminal_and_the_draft():
    """A harness that silently no-ops proves nothing, so check it reached both halves of the page."""
    out = _boot()
    reached = set(out["seen"])
    for element in ("termOut", "termStatus", "termModel", "termEffort", "termInput",
                    "draftBody", "reviewBody", "figuresBody", "historyBody", "stVerdict"):
        assert element in reached, f"{element} was never looked up: the harness is not exercising it"
