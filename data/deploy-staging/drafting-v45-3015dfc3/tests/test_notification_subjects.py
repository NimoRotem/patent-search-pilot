"""Every notification used the same subject, so Gmail made them one conversation.

Sixteen messages between 1 and 19 August all read "Your patent prior-art search is ready", from
the same sender. Gmail threads on subject plus participants, and a conversation that has ever been
muted or archived swallows every later message — not in the inbox, not in spam. A distinct subject
per search is the fix and is also the more useful line to read.
"""
import accounts


def test_two_different_searches_do_not_share_a_subject():
    a = {"subject": "US-20250033224-A1", "title": "Portable vacuum gripper", "slug": "adhoc-a"}
    b = {"subject": "US-11413727-B2", "title": "Vacuum gripper", "slug": "adhoc-b"}
    assert accounts._search_label(a) != accounts._search_label(b)


def test_the_label_names_the_application_when_there_is_one():
    row = {"subject": "US-20250033224-A1", "title": "Portable vacuum gripper", "slug": "s"}
    assert accounts._search_label(row) == "US-20250033224-A1 — Portable vacuum gripper"


def test_it_falls_back_to_the_query_then_to_the_slug():
    assert accounts._search_label(
        {"query": "a vacuum handling apparatus", "slug": "s"}) == "a vacuum handling apparatus"
    assert accounts._search_label({"slug": "adhoc-abc123"}) == "abc123"


def test_the_label_is_one_line_and_bounded():
    row = {"query": "x " * 400, "slug": "s"}
    out = accounts._search_label(row)
    assert "\n" not in out and "\r" not in out
    assert len(out) <= 66, len(out)


def test_a_long_label_is_elided_rather_than_cut_mid_stream():
    out = accounts._search_label({"query": "A vacuum handling apparatus " * 10, "slug": "s"})
    assert out.endswith("…")


def test_no_search_notification_ships_a_constant_subject():
    """Guards the regression directly: the literal that made them one thread must not come back."""
    src = open(accounts.__file__.replace(".pyc", ".py")).read()
    assert 'subject="Your patent prior-art search is ready"' not in src
    assert 'subject="Your patent prior-art search did not finish"' not in src
    assert "_search_label(row)" in src
