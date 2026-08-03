"""Admin-only GitHub login -- a gate, not a user-accounts system.

There is exactly one valid identity: whatever GitHub username is configured
in SCHEMANTIC_ADMIN_GITHUB_LOGIN. Anyone else who completes GitHub OAuth
gets a clean rejection, not an account. No password storage, no signup
flow, no users database -- the only question this module ever answers is
"is the person who just authenticated with GitHub that one configured
account, yes or no."

is_admin_login() is deliberately the only pure, side-effect-free piece --
everything else here makes real network calls to GitHub and is verified
manually against the live site, per this project's existing convention for
external-API code (see schemantic/datasheets.py's docstring): unit-test the
deterministic logic, don't mock the network.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode

import httpx

GITHUB_CLIENT_ID = os.getenv("SCHEMANTIC_GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("SCHEMANTIC_GITHUB_CLIENT_SECRET", "")
ADMIN_GITHUB_LOGIN = os.getenv("SCHEMANTIC_ADMIN_GITHUB_LOGIN", "")

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_TIMEOUT_S = 10.0


def authorize_url(redirect_uri: str, state: str) -> str:
    params = urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": "read:user",
            "state": state,
        }
    )
    return f"{_AUTHORIZE_URL}?{params}"


def exchange_code_for_login(code: str, redirect_uri: str) -> str | None:
    """The OAuth authorization-code exchange, then a lookup of who that
    token belongs to. Returns the GitHub username, or None on any failure
    (wrong/expired code, GitHub outage, network error) -- callers treat
    None the same as "not the admin," never a crash."""
    try:
        with httpx.Client(headers={"Accept": "application/json"}) as client:
            token_response = client.post(
                _TOKEN_URL,
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                timeout=_TIMEOUT_S,
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not access_token:
                return None
            user_response = client.get(
                _USER_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=_TIMEOUT_S,
            )
            user_response.raise_for_status()
            return user_response.json().get("login")
    except httpx.HTTPError:
        return None


def is_admin_login(login: str | None) -> bool:
    """Case-insensitive on purpose -- GitHub usernames are already
    case-insensitive for sign-in, so a config typo in casing shouldn't
    lock the admin out. Empty/unconfigured ADMIN_GITHUB_LOGIN never
    matches anything, including an empty login -- there is no "everyone is
    admin" fallback state."""
    return bool(login) and bool(ADMIN_GITHUB_LOGIN) and login.lower() == ADMIN_GITHUB_LOGIN.lower()
