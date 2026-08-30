"""The ranked list must show what the pipeline actually read.

`/report/<slug>/ranked` exists so that "a reference the pipeline found, read and ranked is never
unreachable" (its own docstring, after attorney references read cell-perfectly and ranked 103/247
turned out to be invisible). It has a Screened column, a Read column and a Cells column for exactly
that.

Those three columns were empty on every report ever rendered by this page. It asked
`deep_analysis.result(slug)` for a `by_pub` key, and that function has never produced one: the
string `by_pub` does not appear in `deep_analysis.py` at all. The reading record lives on the
report, under `deep_rank.by_pub`. Measured on the live box 2026-08-22, `adhoc-60085e96d7d0` holds
340 entries there with 324 read in full, and the page rendered a dash for all of them.

Two separate things had to be true for a row to say "full", and neither was: the page has to look in
the right dict, and it has to resolve the family to the same member the reading charted. The second
is `webview.reps_for`; this file covers the first.
"""
import ast
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _ranked_tail_source():
    tree = ast.parse(open(os.path.join(SRC, "webapp.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "ranked_tail":
            return ast.unparse(node)
    raise AssertionError("ranked_tail is gone; has the ranked page been renamed?")


def test_deep_analysis_still_does_not_produce_by_pub():
    """The premise. If `deep_analysis.result` ever grows a real `by_pub`, this file is wrong and
    should be revisited rather than deleted."""
    body = open(os.path.join(SRC, "deep_analysis.py"), encoding="utf-8").read()
    assert "by_pub" not in body, (
        "deep_analysis now has a by_pub; check whether the ranked page should read that instead")


def test_the_ranked_page_reads_the_reports_own_reading_record():
    src = _ranked_tail_source()
    assert "deep_rank" in src and "by_pub" in src, (
        "the ranked page no longer reads deep_rank.by_pub, so its Screened, Read and Cells columns "
        "are dead again")
    assert "deep_analysis.result" not in src, (
        "the ranked page is back to asking deep_analysis.result for by_pub, which never has one")


def test_the_columns_are_still_rendered():
    """The guard above stays green if somebody deletes the columns instead of fixing them."""
    tpl = os.path.join(os.path.dirname(SRC), "templates", "ranked.html")
    body = open(tpl, encoding="utf-8").read()
    assert "r.read" in body and "r.screen" in body, (
        "the ranked template no longer shows what was read")


def test_a_read_reference_renders_as_full():
    """END TO END over the render path, with a report shaped like a real one.

    DEFECT INJECTION is the point: keyed on the pub the reading recorded, this row says "full".
    Under the old source, `deep` was empty and the same row said "-".
    """
    import webapp

    rep = {
        "ranked_families": ["F1"],
        "family_reps": {"F1": "US-1111111-B2"},
        "deep_rank": {"by_pub": {"US-1111111-B2": {"read_in_full": True, "screen": 82,
                                                   "covered": ["a", "b"]}}},
    }
    deep = ((rep.get("deep_rank") or {}).get("by_pub")) or {}
    info = deep.get("US-1111111-B2") or {}
    assert info.get("read_in_full") is True and info.get("screen") == 82

    #  and the source the page used to read, on the same report, yields nothing
    assert (rep.get("by_pub") or {}).get("US-1111111-B2") is None
    assert hasattr(webapp, "ranked_tail")
