"""A rebuild replaces the package; it does not merge with the last one.

Measured 2026-08-20 on adhoc-8dcf2436929a: a build at 19:06 wrote Doc7_GB2200615A and
Doc8_US8616602B2, a build at 19:15 wrote Doc7_US1675003A and Doc8_US6109885A, and all four
survived. Twelve files for a ten-document package, two of them called Document 7, and the zip
would have carried both into an envelope.
"""
import re

import webapp


def test_a_build_clears_the_previous_package():
    src = open(webapp.__file__.replace(".pyc", ".py")).read()
    m = re.search(r'out = CONCISE_DIR / slug\n(.*?)for k, d in enumerate\(docs, 1\)', src, re.S)
    assert m, "the concise build body moved"
    head = m.group(1)
    assert 'glob("ConciseDescription_*")' in head, (
        "a rebuild leaves the previous package's documents on disk, so the same document number "
        "can name two different references")
    assert "unlink()" in head


def test_it_clears_before_it_writes():
    """After the write it would delete what it just made; the order is the whole fix."""
    src = open(webapp.__file__.replace(".pyc", ".py")).read()
    i_clear = src.index('glob("ConciseDescription_*")')
    i_write = src.index("concise_render.filename(d, fmt)")
    assert i_clear < i_write


def test_the_model_files_go_too():
    """`.model.json` is what the preview and the re-render read. A stale one renders a document
    that is not in this package."""
    src = open(webapp.__file__.replace(".pyc", ".py")).read()
    assert 'glob("*.model.json")' in src
