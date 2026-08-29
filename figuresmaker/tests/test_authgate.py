"""The sign-in gate, and the one way it has actually failed.

This app trusts a cookie another app signed. The defect worth a test is not a forged cookie, which
the signature already refuses: it is a REAL cookie verified with one app's key while the user id
inside it is looked up in a different app's database. That is what happened when the root of
nimo.iptorch.com moved on 2026-08-27, and it locked the owner out of his own figures with an error
that looked like a wrong password. So: the key and the account database must come from the same
place, and a configuration that cannot supply the database must deny rather than guess.
"""
from __future__ import annotations

import importlib
import sys

import pytest


def _tree(tmp_path, name, secret, env_lines):
    root = tmp_path / name
    root.mkdir()
    (root / ".secret_key").write_text(secret + "\n", encoding="utf-8")
    (root / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return root


def _load(monkeypatch, **environ):
    for key in ("AUTH_ROOT", "PILOT_ROOT", "SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("authgate", None)
    return importlib.import_module("authgate")


PG = ["PGHOST=10.0.0.9", "PGPORT=5432", "PGDATABASE=roots", "PGUSER=rootuser", "PGPASSWORD=rootpw"]


def test_auth_root_moves_the_key_and_the_database_together(tmp_path, monkeypatch):
    pilot = _tree(tmp_path, "pilot", "old-key", ["PGHOST=10.0.0.1", "PGPORT=5433",
                                                 "PGDATABASE=patents", "PGUSER=patents",
                                                 "PGPASSWORD=old"])
    root = _tree(tmp_path, "root", "new-key", PG)
    gate = _load(monkeypatch, PILOT_ROOT=str(pilot), AUTH_ROOT=str(root))

    assert gate.SECRET_FILE == root / ".secret_key"
    assert gate.ENV_FILE == root / ".env"
    assert gate._secret_key() == "new-key"
    assert gate._pg_settings()["PGDATABASE"] == "roots"
    #  The retrieval side stays where it was: this is a sign-in change, not a data move.
    assert gate.PILOT_ROOT == pilot


def test_identity_defaults_to_the_pilot_when_one_app_does_both(tmp_path, monkeypatch):
    pilot = _tree(tmp_path, "pilot", "only-key", PG)
    gate = _load(monkeypatch, PILOT_ROOT=str(pilot))

    assert gate.SECRET_FILE == pilot / ".secret_key"
    assert gate.ENV_FILE == pilot / ".env"


def test_a_cookie_the_other_tree_signed_is_refused(tmp_path, monkeypatch):
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface

    pilot = _tree(tmp_path, "pilot", "old-key", PG)
    root = _tree(tmp_path, "root", "new-key", PG)
    signer = Flask("t")
    signer.secret_key = "old-key"
    stale = SecureCookieSessionInterface().get_signing_serializer(signer).dumps(
        {"user_id": 4, "session_version": 1})

    gate = _load(monkeypatch, PILOT_ROOT=str(pilot), AUTH_ROOT=str(root))
    assert gate.read_session(stale) is None


def test_an_unreadable_account_env_denies_instead_of_falling_back(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".secret_key").write_text("new-key\n", encoding="utf-8")
    gate = _load(monkeypatch, AUTH_ROOT=str(root))

    with pytest.raises(gate.AuthUnavailable):
        gate._pg_settings()
    #  And the caller turns that into a denial, never into a connection to some default database.
    assert gate._lookup(1) is None


def test_a_half_filled_account_env_denies(tmp_path, monkeypatch):
    root = _tree(tmp_path, "root", "new-key", ["PGHOST=10.0.0.9", "PGDATABASE=roots"])
    gate = _load(monkeypatch, AUTH_ROOT=str(root))

    with pytest.raises(gate.AuthUnavailable):
        gate._pg_settings()
