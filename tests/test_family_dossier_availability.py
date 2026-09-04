"""The dossier's USPTO client, and the difference between empty and unread.

This module walks up to twelve relatives with two calls each, and several dossiers run at once
under the search workers, so it meets the Open Data Portal's rate limit routinely. It used to
return an empty dict for every answer below 500, which turned a 429 into "this relative has no
office actions" and quietly removed the examiner's own rejection from a drafted patent's
evidence. That is the one document the module exists to find.
"""
import email.message
import urllib.error

import family_dossier as fd


class _HttpError(urllib.error.HTTPError):
    def __init__(self, code, retry_after=None):
        super().__init__("https://api.uspto.gov/x", code, "boom", email.message.Message(), None)
        if retry_after:
            self.headers["Retry-After"] = str(retry_after)


def _responder(*outcomes):
    """Answer each call with the next outcome: an exception class or a body."""
    seq = list(outcomes)

    def call(req, timeout=None):
        item = seq.pop(0) if seq else b'{"ok": true}'
        if isinstance(item, Exception):
            raise item
        class R:
            def read(self): return item
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    return call


def test_a_rate_limit_is_retried_rather_than_read_as_an_empty_wrapper(monkeypatch):
    monkeypatch.setattr(fd, "KEY", "x")
    monkeypatch.setattr(fd.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fd.urllib.request, "urlopen",
                        _responder(_HttpError(429), _HttpError(429), b'{"documentBag": [1]}'))
    assert fd._call("patent/applications/1/documents", log=lambda *_: None) == {"documentBag": [1]}


def test_retry_after_is_honoured(monkeypatch):
    waits = []
    monkeypatch.setattr(fd, "KEY", "x")
    monkeypatch.setattr(fd.time, "sleep", lambda n: waits.append(n))
    monkeypatch.setattr(fd.urllib.request, "urlopen",
                        _responder(_HttpError(429, retry_after=7), b'{}'))
    fd._call("patent/applications/1", log=lambda *_: None)
    assert waits and max(waits) >= 7


def test_giving_up_is_recorded_so_a_caller_can_tell_it_from_an_empty_file(monkeypatch):
    monkeypatch.setattr(fd, "KEY", "x")
    monkeypatch.setattr(fd, "TRIES", 2)
    monkeypatch.setattr(fd.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fd.urllib.request, "urlopen",
                        _responder(_HttpError(429), _HttpError(429), _HttpError(429)))
    del fd.UNAVAILABLE[:]
    said = []
    assert fd._call("patent/applications/1/documents", log=said.append) == {}
    assert fd.UNAVAILABLE and fd.UNAVAILABLE[-1]["why"] == "HTTP 429"
    assert any("UNAVAILABLE" in line for line in said)


def test_a_real_404_is_an_answer_and_is_not_recorded_as_a_failure(monkeypatch):
    monkeypatch.setattr(fd, "KEY", "x")
    monkeypatch.setattr(fd.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fd.urllib.request, "urlopen", _responder(_HttpError(404)))
    del fd.UNAVAILABLE[:]
    assert fd._call("patent/applications/1", log=lambda *_: None) == {}
    assert fd.UNAVAILABLE == []


def test_the_dossier_reports_what_it_could_not_read(monkeypatch):
    monkeypatch.setattr(fd, "KEY", "x")
    monkeypatch.setattr(fd, "TRIES", 1)
    monkeypatch.setattr(fd.time, "sleep", lambda *_: None)

    def flaky(path, body=None, log=print):
        if path.endswith("/documents"):
            fd._note_unavailable(path, "HTTP 429")
            return {}
        if path.endswith("/continuity"):
            return {}
        return {"patentFileWrapperDataBag": [
            {"applicationNumberText": "19315746",
             "applicationMetaData": {"applicationStatusDescriptionText": "Docketed",
                                     "inventionTitle": "t", "earliestPublicationNumber": "US1A1"}}]}

    monkeypatch.setattr(fd, "_call", flaky)
    del fd.UNAVAILABLE[:]
    said = []
    out = fd.dossier(publication="US1A1", log=said.append)
    assert out["unavailable"], "the dossier must carry out what it could not read"
    assert any("INCOMPLETE" in line for line in said)
