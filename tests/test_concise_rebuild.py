"""A rebuild replaces the package; it does not merge with the last one.

Measured 2026-08-20 on adhoc-8dcf2436929a: a build at 19:06 wrote Doc7_GB2200615A and
Doc8_US8616602B2, a build at 19:15 wrote Doc7_US1675003A and Doc8_US6109885A, and all four
survived. Twelve files for a ten-document package, two of them called Document 7, and the zip
would have carried both into an envelope.

The packet grew past the descriptions on 2026-08-23: it now also holds the document list, the
statements, the audit, a legible copy of each non-U.S. item and a translation of each non-English
one. Every one of those is per-build too. A copy left behind for an item the new build does not
list is worse than a duplicate description, because it would be FILED as part of a submission it
does not belong to. So these tests assert the property, "each of these is cleared before anything
is written", rather than the exact shape of the loop that does it.
"""
import re

import webapp

#  Named as literals. Deriving them from the source under test would make the assertion vacuous.
MUST_BE_CLEARED = (
    "ConciseDescription_*",     # the descriptions themselves
    "*.model.json",             # what the preview and the re-render read
    "00_*",                     # the audit and the read-me
    "01_*",                     # the document list and statements
    "40_Copy_*",                # 1.290(d)(3) copies
    "50_Translation_*",         # 1.290(d)(4) translations
    "MANIFEST.csv",
)


def _source():
    return open(webapp.__file__.replace(".pyc", ".py")).read()


def _clearing_block():
    src = _source()
    m = re.search(r"out = CONCISE_DIR / slug\n(.*?)for k, d in enumerate\(docs, 1\)", src, re.S)
    assert m, "the concise build body moved"
    return m.group(1)


def test_a_build_clears_every_kind_of_artefact_the_last_one_left():
    head = _clearing_block()
    missing = [p for p in MUST_BE_CLEARED if p not in head]
    assert not missing, (
        "a rebuild leaves these behind, so the packet can carry a paper belonging to a document "
        "this submission does not list: %s" % ", ".join(missing))
    assert "unlink()" in head


def test_it_clears_before_it_writes():
    """After the write it would delete what it just made; the order is the whole fix."""
    src = _source()
    i_clear = src.index('"ConciseDescription_*"')
    i_write = src.index("concise_render.filename(d, fmt)")
    assert i_clear < i_write


def test_the_model_files_go_too():
    """`.model.json` is what the preview and the re-render read. A stale one renders a document
    that is not in this package."""
    assert '"*.model.json"' in _clearing_block()


def test_the_working_files_never_reach_the_zip():
    """The archive is what gets filed. `.md` and `.model.json` are working files: a model.json in
    an envelope to the Office is a leak of the raw cells behind the paper."""
    src = _source()
    m = re.search(r"zipfile\.ZipFile\(buf, \"w\".*?\n(.*?)\n    if not n:", src, re.S)
    assert m, "the zip body moved"
    body = m.group(1)
    assert '".model.json"' in body and '".md"' in body, (
        "the zip no longer excludes the working files")
    assert "continue" in body
