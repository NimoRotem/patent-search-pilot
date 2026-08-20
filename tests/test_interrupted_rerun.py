"""A search killed by a restart must be restartable BY THE USER, from its own page.

Reported 2026-08-20: a report sat on its first phase for 76 minutes. Everything behind the page
already knew what had happened — /status returned status "interrupted" with the message "Use
Re-run to start it again" — and there was no Re-run that worked:

  * `restart_partial` was dispatcher-only, so a POST to /run hit `if p.exists() and not regen:
    return "ready", rep` and handed back the same dead partial. Re-posting the identical inputs
    landed on the identical slug and changed nothing.
  * the page's progress handler treated only `done` and `error` as terminal, so an interrupted
    run kept a spinner turning for ever, which reads as a slow search rather than a dead one.
"""
import re


def _webapp():
    import webapp
    return webapp


def test_run_asks_for_a_restart_of_an_interrupted_partial():
    """The one call that must be allowed to drop a stale partial is the one that means "run it"."""
    src = open(_webapp().__file__.replace(".pyc", ".py")).read()
    m = re.search(r"def run\(\):.*?\n@app\.route", src, re.S)
    assert m, "the /run view moved"
    body = m.group(0)
    call = re.search(r"ensure_report\(slug,.*?\)", body, re.S)
    assert call, "no ensure_report call in /run"
    assert "restart_partial=True" in call.group(0), (
        "POST /run hands back the interrupted partial instead of restarting the search")


def test_viewing_a_report_still_renders_the_partial():
    """A GET must NOT restart anything: a partial page still has to render, with its banner."""
    src = open(_webapp().__file__.replace(".pyc", ".py")).read()
    m = re.search(r"def report\(slug\):.*?\n    view = _build_view_cached", src, re.S)
    assert m, "the report view moved"
    call = re.search(r"ensure_report\(slug,.*?\)", m.group(0), re.S)
    assert call and "restart_partial" not in call.group(0), (
        "opening a report now restarts it, which is not what viewing means")


def test_ensure_report_drops_the_stale_partial_only_when_asked(monkeypatch, tmp_path):
    """The behaviour itself, not the call site: same inputs, one flag, two answers."""
    webapp = _webapp()
    slug = "adhoc-test0000dead"
    p = tmp_path / ("%s.json" % slug)
    p.write_text('{"partial": true, "query": "q"}')
    monkeypatch.setattr(webapp, "report_path", lambda s: tmp_path / ("%s.json" % s))
    dropped = []
    monkeypatch.setattr(webapp, "_drop_partial_report", lambda s: dropped.append(s))
    with webapp._JOB_LOCK:
        webapp._JOBS.pop(slug, None)

    st, rep = webapp.ensure_report(slug)                       # a viewer
    assert st == "ready" and rep["partial"] is True and dropped == []

    #  and with the flag: the partial is dropped and the run is (re)claimed
    st2, _ = webapp.ensure_report(slug, query="q", restart_partial=True)
    assert dropped == [slug], "the stale partial survived an explicit restart"
    assert st2 != "ready", st2
    with webapp._JOB_LOCK:
        webapp._JOBS.pop(slug, None)


def test_a_finished_report_is_never_restarted(monkeypatch, tmp_path):
    """`partial` is the whole test. A finished report must be served, flag or no flag."""
    webapp = _webapp()
    slug = "adhoc-test0000done"
    (tmp_path / ("%s.json" % slug)).write_text('{"partial": false, "query": "q"}')
    monkeypatch.setattr(webapp, "report_path", lambda s: tmp_path / ("%s.json" % s))
    dropped = []
    monkeypatch.setattr(webapp, "_drop_partial_report", lambda s: dropped.append(s))
    st, rep = webapp.ensure_report(slug, query="q", restart_partial=True)
    assert st == "ready" and dropped == []


def test_the_page_stops_and_offers_a_restart_when_a_run_is_interrupted():
    """The spinner is the symptom. Terminal state, plain sentence, working control."""
    html = open("templates/report.html").read()
    assert "ev.status === 'interrupted'" in html, "the page never treats interrupted as terminal"
    assert "refiningRestart" in html, "no restart control on an interrupted run"
    #  the control has to post every input the slug hash is built from, or it mints a new slug
    m = re.search(r'<form[^>]*id="refiningRestart".*?</form>', html, re.S)
    assert m, "the restart form moved"
    form = m.group(0)
    for field in ("query", "mode", "depth", "search_focus", "doc_token", "csrf_token"):
        assert 'name="%s"' % field in form, "restart form drops %s, so it mints a new slug" % field


def test_the_view_carries_what_the_restart_form_needs():
    src = open(_webapp().__file__.replace(".pyc", ".py")).read()
    for field in ('view["depth"]', 'view["doc_token"]', 'view["search_focus"]'):
        assert field in src, "%s is not on the view, so the restart form posts an empty one" % field
