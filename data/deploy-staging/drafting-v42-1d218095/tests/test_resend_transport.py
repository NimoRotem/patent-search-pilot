"""Outbound mail without a host-local gateway.

On instance-3 this app handed notifications to a per-domain SMTP listener on 127.0.0.1:2530 which
relayed to Resend. Moving the stack to its own machine on 2026-08-21 left that behind: the gateway
is a whole webmail tenant with its own database, inbound forwarding and hold rules, and forking it
onto a second box so an outbound notification can leave would recreate exactly the coupling the
move was meant to end.

The port being wrong on that gateway is also how every search notification was silently lost once
before: the local handoff succeeded, the row was recorded sent, and Resend dropped the message
because the From domain was not verified for that tenant's key. One less hop is one less place for
that to happen.
"""
import notifications as n


def test_resend_is_a_transport(monkeypatch):
    monkeypatch.setattr(n, "MAIL_TRANSPORT", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    st = n.transport_status()
    assert st["transport"] == "resend" and st["configured"] is True


def test_auto_prefers_resend_when_a_key_is_present(monkeypatch):
    monkeypatch.setattr(n, "MAIL_TRANSPORT", "auto")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    assert n.transport_status()["transport"] == "resend"


def test_auto_still_falls_back_to_smtp_without_a_key(monkeypatch):
    """instance-3 keeps working exactly as it did."""
    monkeypatch.setattr(n, "MAIL_TRANSPORT", "auto")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    assert n.transport_status()["transport"] == "smtp"


def test_a_missing_key_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(n, "MAIL_TRANSPORT", "resend")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    st = n.transport_status()
    assert st["configured"] is False and "missing" in st["detail"]


def test_it_sends_a_real_user_agent(monkeypatch):
    """Resend 403s the default urllib User-Agent. This has cost a turn before."""
    monkeypatch.setattr(n, "MAIL_TRANSPORT", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    seen = {}

    class _R:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = req.data
        return _R()

    monkeypatch.setattr(n.urllib.request, "urlopen", _open)
    n._deliver({"to_email": "a@b.c", "subject": "s", "body_text": "t"})
    assert seen["ua"] and "Mozilla" in seen["ua"]
    assert seen["auth"] == "Bearer re_test"
    assert b'"to": ["a@b.c"]' in seen["body"]


def test_a_refused_send_raises_rather_than_recording_it_sent(monkeypatch):
    """The failure mode that lost sixteen notifications: a handoff that looks fine and is not."""
    import urllib.error
    monkeypatch.setattr(n, "MAIL_TRANSPORT", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")

    import io

    def _boom(req, timeout=None):
        #  A real file object: HTTPError delegates .read() to it, and passing None makes the
        #  error itself blow up in tempfile rather than surfacing the 403.
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"domain not verified"))

    monkeypatch.setattr(n.urllib.request, "urlopen", _boom)
    try:
        n._deliver({"to_email": "a@b.c", "subject": "s", "body_text": "t"})
    except RuntimeError as e:
        assert "403" in str(e)
    else:
        raise AssertionError("a refused send was treated as delivered")
