"""The drafting agent's panel can be folded away. It must always be possible to unfold it.

WHY THIS EXISTS. On 2026-08-31 this was reported as "the claude session window is missing on both
nimo and nimo2". Nothing was missing. The panel was folded, and the folded panel was unusable:

``.studioterm`` is ``align-self: start`` with ``overflow: hidden``, so it is exactly as tall as its
contents, and folding hides every one of them. The collapsed box measured 34 by 2 pixels, and the
26px chevron that reopens it, being inside a 2px box with hidden overflow, was clipped out of
existence. There was nothing on the screen to press and nothing to say where the panel had gone.
The fold is remembered in localStorage and both hostnames resolve to one origin, so it looked the
same everywhere, on every visit, for ever.

None of that was a JavaScript error, so the bundle test passed. None of it was a missing element,
so the template test passed. It was a box with no height, and the only thing that catches a box
with no height is checking that something gives it one.

These are text assertions on the shipped files rather than a rendering test, because the layout
that produced the bug is one declaration and a browser is not needed to see whether it is there.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "draft_studio.css").read_text(encoding="utf-8")
JS = (ROOT / "static" / "draft_studio.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "draft_studio.html").read_text(encoding="utf-8")

#  Comments hold prose about the very selectors being asserted, so they have to go first or every
#  search finds its own explanation.
CSS_CODE = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

def _lift_media(css: str) -> tuple[str, str]:
    """Split the sheet into (rules that always apply, rules inside an ``@media`` block).

    Every question here is about one state or the other, and a conditional rule that overrides an
    unconditional one is a LATER rule, so a search over the raw text answers a desktop question
    with the phone's answer. There are four ``max-width: 760px`` blocks and they are scattered
    through the file rather than gathered at the end, so this walks braces instead of slicing.
    """
    wide, inside, i = [], [], 0
    while True:
        at = css.find("@media", i)
        if at < 0:
            wide.append(css[i:])
            return "".join(wide), "\n".join(inside)
        wide.append(css[i:at])
        brace = css.index("{", at)
        depth, j = 0, brace
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inside.append(css[brace + 1:j])
        i = j + 1


WIDE, NARROW = _lift_media(CSS_CODE)


def _block(selector: str, css: str = WIDE) -> str:
    """The declarations of the LAST rule with exactly this selector, or "" if there is none."""
    found = ""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if match.group(1).strip() == selector:
            found = match.group(2)
    return found


def _px(block: str, prop: str) -> float | None:
    match = re.search(rf"(?<![-\w]){re.escape(prop)}\s*:\s*(-?[\d.]+)px", block)
    return float(match.group(1)) if match else None


def test_the_panel_is_as_tall_as_its_contents_which_is_why_the_rest_of_this_file_exists():
    """The premise. If this changes, the assertions below are about the wrong thing."""
    base = _block(".studioterm")
    assert "align-self: start" in base
    assert "overflow: hidden" in base


def test_a_folded_panel_is_given_a_height_of_its_own():
    """Its contents are hidden, so nothing else will give it one, and 2px cannot be pressed."""
    folded = _block(".studio.chathidden .studioterm")
    assert folded, "no rule sizes the collapsed panel at all"
    floor = _px(folded, "min-height")
    assert floor is not None and floor >= 120, (
        "the collapsed panel needs a min-height it can hold its own reopen control inside; "
        f"found {floor!r}")
    assert "align-self: stretch" in folded, (
        "the rail should be the height of the draft beside it, not just its floor")


def test_a_folded_panel_on_a_narrow_screen_is_given_one_too():
    """One column there, so the collapsed panel is a row: same bug, different axis."""
    folded = _block(".studio.chathidden .studioterm", NARROW)
    floor = _px(folded, "min-height")
    assert floor is not None and floor >= 30, (
        f"a collapsed row with every child hidden is two pixels of border; found {floor!r}")


def test_folding_does_not_hide_the_way_back():
    """Whatever the fold hides, it may not hide the control that undoes it."""
    hide = [match.group(0) for match in re.finditer(
        r"\.studio\.chathidden \.studioterm > \*[^{]*\{[^}]*display:\s*none[^}]*\}", CSS_CODE)]
    #  Over the whole file, not just the wide part: a rule added under any media query that hides
    #  the way back is the same bug.
    assert hide, "expected a rule that hides the folded panel's children"
    for rule in hide:
        assert ":not(.chatfold)" in rule, f"the reopen button is hidden by: {rule}"
        assert ":not(.chatspine)" in rule, f"the rail's own label is hidden by: {rule}"


def test_the_folded_rail_says_what_it_is():
    """A bare strip reads as the feature having gone, not as a panel being shut."""
    assert 'class="chatspine"' in HTML
    section = HTML[HTML.index('<section class="studioterm"'):]
    assert "chatspine" in section[:section.index("</section>")], (
        "the label has to be inside the panel, or the fold rules never reach it")
    label = re.search(r'<span class="chatspine"[^>]*>([^<]+)</span>', HTML)
    assert label and label.group(1).strip(), "the rail carries no words"
    shown = _block(".studio.chathidden .chatspine")
    assert "display: block" in shown, "the label only appears once the panel is folded"
    assert "display: none" in _block(".chatspine"), "the label must not show while it is open"


def test_the_whole_rail_reopens_it_not_only_the_button_on_it():
    assert re.search(r"querySelector\('\.studioterm'\)[\s\S]{0,400}?foldChat\(false\)", JS), (
        "expected a click handler on the collapsed panel itself")


def test_the_fold_control_sits_in_the_same_place_in_both_states():
    """Pressing it must not move the control that undoes it across the width of the panel."""
    open_side = _px(_block(".chatfold"), "left")
    shut_side = _px(_block(".studio.chathidden .chatfold"), "left")
    assert open_side is not None and shut_side is not None, (
        "both states should place the fold button on the same edge")
    assert abs(open_side - shut_side) <= 12


@pytest.mark.parametrize("key", ["iptorch.chatfold2"])
def test_the_fold_that_could_not_be_undone_is_forgotten_once(key):
    """Nobody sitting in the old unrecoverable fold chose it knowing what it did."""
    assert key in JS, "the remembered fold should read a key no trapped browser can hold"
    assert re.search(r"removeItem\('iptorch\.chatfold'\)", JS), (
        "the old key should be cleared rather than left to rot in every browser")
