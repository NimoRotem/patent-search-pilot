"""The public link is minted with the report, under one password the owner sets once.

Asked for 2026-08-23: "Public link create by default with default password set by the user once in
his profile area." Two properties carry the whole safety of that, and both are tested here.

**A link is never published without a password.** The link IS the access control, and an
unguessable slug is not a password. Publishing automatically with none would quietly turn every
finished search into a document anyone holding the URL can read, which is not a default anybody
chose. `autopublish` does nothing until a share password exists, and the account page says so
rather than leaving somebody to find out.

**The password is stored hashed and never read back.** It is set once, copied onto each report at
publish time, and can be replaced but not displayed. `public_user` must not leak the hash to a
template, which is the same rule the login password already follows.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import accounts                                                          # noqa: E402
import public_report                                                     # noqa: E402


def test_the_hash_never_reaches_a_template():
    got = accounts.public_user({"id": 1, "email": "a@b.c", "password_hash": "x",
                                "share_password_hash": "scrypt:32768:8:1$abc"})
    assert "share_password_hash" not in got
    assert "password_hash" not in got
    assert got["has_share_password"] is True
    assert accounts.public_user({"id": 1, "share_password_hash": ""})["has_share_password"] is False


def test_autopublish_does_nothing_without_a_share_password(monkeypatch):
    """THE SAFETY PROPERTY. Defect-injected by the assertion below: if `publish` is reached at all,
    a report was about to go public with no password on it."""
    monkeypatch.setattr(accounts, "share_defaults",
                        lambda uid: {"autopublish": True, "password_hash": ""})
    monkeypatch.setattr(public_report, "publish",
                        lambda *a, **k: pytest.fail("published with no share password"))
    assert public_report.autopublish(1, "adhoc-x", title="t") == {}


def test_autopublish_does_nothing_when_the_owner_turned_it_off(monkeypatch):
    monkeypatch.setattr(accounts, "share_defaults",
                        lambda uid: {"autopublish": False, "password_hash": "scrypt:x"})
    monkeypatch.setattr(public_report, "publish",
                        lambda *a, **k: pytest.fail("published against the owner's setting"))
    assert public_report.autopublish(1, "adhoc-x") == {}


def test_autopublish_passes_the_owners_hash_through_unchanged(monkeypatch):
    """The point of carrying a HASH rather than a password: the plaintext is never stored, never
    passed around and never re-hashed into something the owner's password would not open."""
    seen = {}
    monkeypatch.setattr(accounts, "share_defaults",
                        lambda uid: {"autopublish": True, "password_hash": "scrypt:32768:8:1$abc"})
    monkeypatch.setattr(public_report, "publish",
                        lambda user_id, slug, **k: seen.update(k, user_id=user_id, slug=slug) or {"ok": 1})
    public_report.autopublish(7, "adhoc-y", title="a title")
    assert seen["password_hash"] == "scrypt:32768:8:1$abc"
    assert seen["user_id"] == 7 and seen["slug"] == "adhoc-y" and seen["title"] == "a title"
    assert "password" not in seen, "a plaintext password must never travel this path"


def test_autopublish_never_raises(monkeypatch):
    """It runs inside the search's completion path. A share link is not worth failing a search."""
    def boom(_uid):
        raise RuntimeError("store is down")
    monkeypatch.setattr(accounts, "share_defaults", boom)
    assert public_report.autopublish(1, "adhoc-z") == {}


def test_publish_accepts_a_ready_made_hash():
    import inspect
    sig = inspect.signature(public_report.publish)
    assert "password_hash" in sig.parameters
    src = inspect.getsource(public_report.publish)
    assert "pw = password_hash or None" in src, (
        "publish no longer accepts an already-hashed password, so the owner's one share password "
        "cannot reach a report without storing the plaintext")


def test_a_short_share_password_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(accounts, "ensure_schema", lambda: None)
    monkeypatch.setattr(accounts, "db", type("D", (), {"cursor": lambda *a, **k: calls.append(1)}))
    with pytest.raises(ValueError):
        accounts.set_share_password(1, "abc")
    assert not calls, "a refused password still opened a transaction"


def test_the_account_page_offers_it_once_and_says_what_happens():
    body = open(os.path.join(ROOT, "templates", "account.html"), encoding="utf-8").read()
    assert 'name="action" value="share"' in body
    assert "share_password" in body and "autopublish" in body
    #  and it must say, in the no-password state, that nothing is being published
    assert "nothing is being" in body.replace("\n", " ")


def test_the_search_completion_path_mints_the_link():
    """Anchored on the call, so deleting it fails here rather than silently going back to a manual
    Publish click on every report."""
    body = open(os.path.join(ROOT, "src", "webapp.py"), encoding="utf-8").read()
    assert "public_report.autopublish(" in body
    assert "accounts.search_owner(slug)" in body, (
        "the owner is resolved from the report dict alone, which does not record who ran it")
