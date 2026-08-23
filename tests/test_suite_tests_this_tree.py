"""A test run must import the tree it is testing, and nothing else.

FOUND 2026-08-23, and it had been silently true for as long as the file existed.
`eval/citation_recall.py` began with

    sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")

pytest imports every test module during collection, `tests/test_citation_recall.py` imports that
module, and from that moment the DEPLOYED checkout sat at the front of `sys.path` for the whole
process. Every module not already cached was then imported from production rather than from the
worktree under test.

Nothing failed, because the two trees usually hold the same code. It surfaced only when a signature
changed on one side: `concise_description.build` gained a parameter in the worktree, three tests
called it through the deployed copy that lacked it, and the failure looked like flakiness because
it depended on which test imported the module first.

The cost is worse than three red tests. A worktree suite reporting green was partly measuring
production, which is the one thing a test run must never do.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#  Named as a literal rather than derived: deriving it from ROOT would make this assertion vacuous
#  the moment the suite is run from the deployed checkout itself.
DEPLOYED = "/home/nimrod_rotem/patent-search-pilot"


def _py_files():
    for sub in ("src", "eval", "ops", "tests"):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                yield os.path.join(sub, name), os.path.join(d, name)


def test_no_module_puts_another_checkout_on_the_path():
    """The guard. An absolute path to a checkout, on `sys.path`, in anything importable."""
    bad = []
    #  A BARE STRING LITERAL as the whole argument, which is the only shape that can name another
    #  checkout. `HERE + "/src"` is relative to the file and is fine, and this file quotes the old
    #  line in its own docstring, so both have to be allowed through.
    pattern = re.compile(r"""sys\.path\.(?:insert|append)\s*\(\s*(?:\d+\s*,\s*)?['"](/[^'"]*)['"]\s*\)""")
    for rel, path in _py_files():
        if os.path.basename(path) == os.path.basename(__file__):
            continue
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            m = pattern.search(line)
            if m and not m.group(1).startswith(os.path.join(ROOT, "")):
                bad.append("%s:%d: %s" % (rel, n, line.strip()[:100]))
    assert not bad, (
        "these put an absolute path on sys.path, so a test run can import a different checkout "
        "than the one under test:\n" + "\n".join(bad))


def test_the_modules_under_test_come_from_this_tree():
    """The property itself, checked against what is actually loaded right now. Cheap, and it
    catches a route the source scan above cannot: a path added by an installed package or an
    environment variable rather than by a line of our own code."""
    strays = []
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if f.startswith(DEPLOYED + os.sep) and not f.startswith(os.path.join(DEPLOYED, ".venv")):
            strays.append("%s <- %s" % (name, f))
    assert not strays, (
        "these are loaded from the deployed checkout instead of this worktree, so the suite is "
        "testing production:\n" + "\n".join(sorted(strays)[:20]))


def test_the_eval_helpers_resolve_their_root_relatively():
    """Both offenders, by name, so deleting the constant above does not delete the assertion."""
    for rel in ("eval/citation_recall.py", "eval/gold_probe.py"):
        body = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
        assert '"%s/src"' % DEPLOYED not in code, rel
        assert "os.path.dirname(os.path.abspath(__file__))" in code, rel
