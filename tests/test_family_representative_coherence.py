"""The document that was READ has to be the document that is CITED, at every later stage.

Counsel, 2026-08-22, on the representative swap: "family members are not interchangeable
documents. If the system reads one member but now cites the earlier-published representative,
every quote has to be re-verified against the cited member. Continuations-in-part add matter,
claims differ between members, and translations differ. The existence check and the refuter pass
need to run against the document that will actually be filed, not the one that was read."

The swap itself was right. What made it dangerous is that SIX places resolved the representative
independently, and only three of them passed the subject's date:

    deep_rank (the screen)          date-aware
    deep_analysis (reading top-up)  date-aware
    claim_rescue (orphan rescue)    date-aware
    webview.view (the report page)  DATE-BLIND -> a different member
    webapp.ranked_tail (the list)   DATE-BLIND -> a different member
    webapp ranked API (paging)      DATE-BLIND -> a different member

So the reading read one member and the page displayed another, and `deep.get(pub)` on the display
side missed for exactly those families: a reference read in full rendered as unread. The same
divergence is what would have put a quote verified against member A under a citation to member B.

There is a second way to diverge that no amount of passing the date fixes: `_seed_families`
REPLACES the representative for a document the examiner applied by number, and that override lives
only in the screen's local dict. Any later re-resolution loses it and cites a sibling the Office
never used.

The fix is that the choice is made ONCE and then recorded on the report, and every later stage
pins to the record instead of re-deciding. These tests hold that shut from three directions: the
pin outranks the ordering, the sites all go through the one entry point, and the ordering still
decides a family nobody has decided yet.
"""
import ast
import datetime
import os

import pytest

import webview

#  The reported family, from test_family_representative: members on both sides of the cutoff.
FAM = "66624664"
EFD = datetime.date(2021, 4, 20)
REPORT = {"date_cutoff": "2021-04-20"}

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


@pytest.fixture()
def cur():
    db = pytest.importorskip("db")
    try:
        conn = db.connect()
    except Exception:
        pytest.skip("no corpus available")
    conn.autocommit = True
    c = conn.cursor()
    try:
        c.execute("SELECT 1 FROM publications WHERE simple_family_id=%s LIMIT 1", (FAM,))
        if not c.fetchone():
            pytest.skip("corpus does not hold the reference family")
        yield c
    finally:
        conn.close()


# --------------------------------------------------------------------- recording the choice

def test_recording_keeps_the_publication_number_per_family():
    rep = {}
    webview.record_family_reps(rep, {"F1": {"publication_number": "US-1-A"},
                                     "F2": {"publication_number": "US-2-A"}})
    assert rep["family_reps"] == {"F1": "US-1-A", "F2": "US-2-A"}


def test_a_family_already_decided_is_never_re_decided():
    """The whole point of recording. If a later stage could overwrite the entry, the record would
    track the last stage to run rather than the stage that did the reading."""
    rep = {"family_reps": {"F1": "US-READ-A"}}
    webview.record_family_reps(rep, {"F1": {"publication_number": "US-OTHER-B"}})
    assert rep["family_reps"]["F1"] == "US-READ-A"


def test_recording_survives_a_report_that_has_no_map_yet():
    rep = {"family_reps": None}                    # a report written before this existed
    webview.record_family_reps(rep, {"F1": {"publication_number": "US-1-A"}})
    assert rep["family_reps"] == {"F1": "US-1-A"}


def test_recording_ignores_rows_with_no_publication_number():
    rep = {}
    webview.record_family_reps(rep, {"F1": {}, "F2": None, "F3": {"publication_number": ""}})
    assert rep.get("family_reps") in ({}, None) or rep["family_reps"] == {}


def test_recording_nothing_does_not_invent_a_key():
    rep = {}
    webview.record_family_reps(rep, {})
    assert "family_reps" not in rep
    assert webview.record_family_reps(None, {"F1": {"publication_number": "US-1-A"}}) is None


# --------------------------------------------------------------------- the pin beats the ordering

def test_the_pin_outranks_the_date_rule(cur):
    """DEFECT INJECTION, both ways.

    `unpinned` is what the ordering picks for this family at this cutoff. Pinning the OTHER member
    has to return that other member instead, or the record is decorative and a later stage is free
    to cite something the reading never opened.
    """
    unpinned = webview.resolve_family_reps(cur, [FAM], subject_efd=EFD)[FAM]
    cur.execute(
        "SELECT publication_number FROM publications "
        "WHERE simple_family_id=%s AND publication_number <> %s "
        "ORDER BY publication_date DESC NULLS LAST LIMIT 1",
        (FAM, unpinned["publication_number"]))
    row = cur.fetchone()
    if not row:
        pytest.skip("family has only one member in this corpus")
    other = row["publication_number"]

    pinned = webview.resolve_family_reps(cur, [FAM], subject_efd=EFD, pinned=[other])[FAM]
    assert pinned["publication_number"] == other, (
        "the pin was accepted and ignored: asked for %s, got %s" % (other,
                                                                    pinned["publication_number"]))
    assert unpinned["publication_number"] != other, "the ordering already picked the pinned member"


def test_the_pin_outranks_readability_too(cur):
    """Readability is a hard gate on a family nobody has decided. It must NOT re-open one that has
    been decided: if the run read an abstract-only member and quoted it, the report cites that
    member, not a fuller sibling whose text those quotes are not in."""
    cur.execute(
        "SELECT p.publication_number FROM publications p "
        " WHERE p.simple_family_id=%s "
        "   AND (SELECT count(*) FROM claims c WHERE c.publication_id=p.id) = 0 "
        " LIMIT 1", (FAM,))
    row = cur.fetchone()
    if not row:
        pytest.skip("every member of this family has claims")
    thin = row["publication_number"]
    got = webview.resolve_family_reps(cur, [FAM], subject_efd=EFD, pinned=[thin])[FAM]
    assert got["publication_number"] == thin


def test_a_pin_for_another_family_does_not_disturb_this_one(cur):
    """The pin list is global to the query and the families are resolved in one pass, so a pin that
    belongs to family B must not change which member of family A is returned."""
    plain = webview.resolve_family_reps(cur, [FAM], subject_efd=EFD)[FAM]
    got = webview.resolve_family_reps(cur, [FAM], subject_efd=EFD,
                                      pinned=["XX-0000000-A9"])[FAM]
    assert got["publication_number"] == plain["publication_number"]


def test_reps_for_pins_from_the_report(cur):
    plain = webview.reps_for(cur, REPORT, [FAM])[FAM]
    report = {"date_cutoff": "2021-04-20", "family_reps": {}}
    cur.execute(
        "SELECT publication_number FROM publications "
        "WHERE simple_family_id=%s AND publication_number <> %s LIMIT 1",
        (FAM, plain["publication_number"]))
    row = cur.fetchone()
    if not row:
        pytest.skip("family has only one member in this corpus")
    report["family_reps"][FAM] = row["publication_number"]
    assert webview.reps_for(cur, report, [FAM])[FAM]["publication_number"] == \
        row["publication_number"]


def test_reps_for_falls_back_to_the_date_rule_not_the_blind_one(cur):
    """A family the report has not decided is decided by the date rule, because that is what every
    stage that HAS a report should have been doing all along. The blind ordering picks the newest
    member, which is systematically the weakest prior art."""
    blind = webview.resolve_family_reps(cur, [FAM])[FAM]
    got = webview.reps_for(cur, REPORT, [FAM])[FAM]
    assert got["publication_date"] < EFD
    assert got["publication_number"] != blind["publication_number"]


def test_reps_for_tolerates_a_report_with_no_cutoff(cur):
    """A report with no date is not an error: it is a search that was never cut against one."""
    assert webview.reps_for(cur, {}, [FAM])[FAM]["publication_number"] == \
        webview.resolve_family_reps(cur, [FAM])[FAM]["publication_number"]
    assert webview.reps_for(cur, None, [FAM])                     # must not raise


# --------------------------------------------------------------------- no stage may re-decide

#  `claim_rescue` keeps one guarded direct call for the case where it is handed no report at all,
#  and `reps_for` is the wrapper itself. Everything else must go through the wrapper.
ALLOWED_DIRECT_CALLERS = {"claim_rescue.py", "webview.py"}


def _modules_calling(name):
    out = {}
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        except SyntaxError:
            continue
        hits = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            called = (f.attr if isinstance(f, ast.Attribute) else
                      f.id if isinstance(f, ast.Name) else "")
            if called == name:
                hits += 1
        if hits:
            out[fn] = hits
    return out


def test_no_new_stage_resolves_a_representative_on_its_own():
    """A GUARD, not a style rule. Six independent resolutions is how the reading and the display
    came to name different documents; the seventh would do it again, silently, and the symptom
    reaches a lawyer rather than a log. A stage that genuinely has no report should say so by
    taking the same guarded shape claim_rescue does."""
    offenders = set(_modules_calling("resolve_family_reps")) - ALLOWED_DIRECT_CALLERS
    assert not offenders, (
        "these modules resolve a family representative without going through webview.reps_for, so "
        "they can name a different member from the one the run read: %s" % sorted(offenders))


def test_the_report_facing_stages_do_go_through_the_wrapper():
    """The other half: the guard above stays green if somebody deletes the call entirely, so name
    the stages that must be asking."""
    users = _modules_calling("reps_for")
    for mod in ("deep_rank.py", "deep_analysis.py", "webapp.py", "webview.py", "claim_rescue.py"):
        assert mod in users, "%s no longer resolves representatives through the report" % mod
    assert users["webapp.py"] >= 2, "the ranked page and the ranked API both resolve a window"


def test_the_screen_records_before_anything_else_can_resolve():
    """deep_rank must record AFTER `_seed_families`, or the examiner override is not in the record
    and every later stage silently swaps back to the ordering's pick."""
    src = open(os.path.join(SRC, "deep_rank.py"), encoding="utf-8").read()
    #  The CALL, not the `def`. Anchoring on "_seed_families(cur, report" matched the definition,
    #  which sits a thousand lines above either of these, so the comparison was true whatever the
    #  screen actually did and the guard asserted nothing.
    seed_at = src.index("families, seed_fams = _seed_families(")
    record_at = src.index("record_family_reps(report, reps)")
    assert seed_at < record_at, (
        "the representatives are recorded before the file-wrapper seeds override them, so a "
        "document the examiner applied would be recorded as the sibling the ordering preferred")
