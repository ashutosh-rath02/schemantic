"""Admin-login matching -- the one pure, side-effect-free piece of the
GitHub OAuth gate. The actual network exchange (authorize_url,
exchange_code_for_login) is verified manually against the live site, per
this repo's existing convention for external-API code (see
tests/test_datasheets.py): no mocking of the network layer anywhere.
"""

from schemantic import auth


def test_matching_login_is_admin(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_GITHUB_LOGIN", "ashutosh-rath02")
    assert auth.is_admin_login("ashutosh-rath02") is True


def test_matching_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_GITHUB_LOGIN", "Ashutosh-Rath02")
    assert auth.is_admin_login("ashutosh-rath02") is True
    assert auth.is_admin_login("ASHUTOSH-RATH02") is True


def test_different_login_is_not_admin(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_GITHUB_LOGIN", "ashutosh-rath02")
    assert auth.is_admin_login("someone-else") is False


def test_none_login_is_never_admin(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_GITHUB_LOGIN", "ashutosh-rath02")
    assert auth.is_admin_login(None) is False


def test_unconfigured_admin_login_matches_nothing(monkeypatch):
    # no "everyone is admin" fallback if the env var is unset/blank -- an
    # empty ADMIN_GITHUB_LOGIN must never accidentally admit an empty login
    monkeypatch.setattr(auth, "ADMIN_GITHUB_LOGIN", "")
    assert auth.is_admin_login("") is False
    assert auth.is_admin_login("anyone") is False
    assert auth.is_admin_login(None) is False
