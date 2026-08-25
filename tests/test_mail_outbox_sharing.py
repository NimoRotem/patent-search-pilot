"""An instance with no mail transport must not consume the shared outbox.

The outbox is one Postgres table, and more than one deployment of this app can point at the same
database (production plus a fix bench). `_claim_one` leases the oldest due row to whichever worker
asks first. A deployment configured deliberately without a transport still runs the worker, so
before this guard it won every race and failed each row as TransportUnavailable, which is
non-terminal by design and therefore retried for ever: on 2026-08-18 outbox row 16 reached 53
attempts and no search notification was delivered for hours while the instance that COULD send sat
idle.

These tests anchor on the ABORT, not on the happy path: the counter must not move, and the row
must still be there for someone else.
"""
import importlib

import pytest


@pytest.fixture()
def notif(monkeypatch):
    import notifications
    importlib.reload(notifications)
    monkeypatch.setattr(notifications, "_NO_TRANSPORT_LOGGED", False, raising=False)
    return notifications


def _fake_rows(notif, monkeypatch, claimed):
    """Record every claim attempt so the test can prove none happened."""
    def _claim_one():
        claimed.append(1)
        return None
    monkeypatch.setattr(notif, "_claim_one", _claim_one)


def test_no_transport_never_claims_a_row(notif, monkeypatch):
    claimed = []
    _fake_rows(notif, monkeypatch, claimed)
    monkeypatch.setattr(notif, "transport_status",
                        lambda: {"configured": False, "transport": "smtp",
                                 "detail": "SMTP_HOST is missing"})
    out = notif.deliver_pending()
    assert out["skipped"] == "no transport"
    assert out["sent"] == 0 and out["failed"] == 0
    assert claimed == [], "a transport-less instance claimed a row out of the shared outbox"


def test_a_configured_transport_still_claims(notif, monkeypatch):
    claimed = []
    _fake_rows(notif, monkeypatch, claimed)
    monkeypatch.setattr(notif, "transport_status",
                        lambda: {"configured": True, "transport": "smtp", "detail": "127.0.0.1"})
    out = notif.deliver_pending()
    assert "skipped" not in out
    assert claimed == [1], "the guard is not simply always-off; a capable instance must claim"


def test_the_guard_logs_once_not_every_poll(notif, monkeypatch, capsys):
    _fake_rows(notif, monkeypatch, [])
    monkeypatch.setattr(notif, "transport_status",
                        lambda: {"configured": False, "transport": "smtp", "detail": "none"})
    for _ in range(5):
        notif.deliver_pending()
    #  A worker polling every few seconds must not fill the journal with the same line.
    assert capsys.readouterr().out.count("no transport on this instance") == 1
