"""Contract tests for the generic non-interactive (bearer-token) auth seam.

Covers Task 2.0a: the reusable token-auth capability in the dashboard auth
framework — NOT the drain plugin (that's 2.0b/2.1). Asserts the ABC capability
flag, the registry filter, bearer extraction, provider stacking (verify_token),
and the route-agnostic middleware seam's fail-closed / 503 / pass-through
behaviour.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from youtab_agent_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
    clear_providers,
    list_providers,
    list_session_providers,
    list_token_providers,
    register_provider,
)
from youtab_agent_cli.dashboard_auth.base import ProviderError
from youtab_agent_cli.dashboard_auth import token_auth


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _OAuthOnly(DashboardAuthProvider):
    """A pure interactive provider — never token-authable."""

    name = "oauth-only"
    display_name = "OAuth Only"

    def start_login(self, *, redirect_uri):
        return LoginStart(redirect_url="x", cookie_payload={})

    def complete_login(self, *, code, state, code_verifier, redirect_uri):
        return Session("u", "e", "n", "o", self.name, 0, "a", "r")

    def verify_session(self, *, access_token):
        return None

    def refresh_session(self, *, refresh_token):
        return Session("u", "e", "n", "o", self.name, 0, "a", "r")

    def revoke_session(self, *, refresh_token):
        return None


class _TokenProvider(_OAuthOnly):
    """A token provider that accepts exactly one secret."""

    name = "tok"
    display_name = "Token Provider"
    supports_token = True

    def __init__(self, *, secret: str = "good-secret", scopes=("drain",)):
        self._secret = secret
        self._scopes = tuple(scopes)

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if token == self._secret:
            return TokenPrincipal(
                principal=self.name, provider=self.name, scopes=self._scopes
            )
        return None


class _UnreachableTokenProvider(_OAuthOnly):
    name = "tok-down"
    display_name = "Unreachable Token Provider"
    supports_token = True

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        raise ProviderError("backing store down")


class _BuggyTokenProvider(_OAuthOnly):
    name = "tok-buggy"
    display_name = "Buggy Token Provider"
    supports_token = True

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        raise RuntimeError("kaboom")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state():
    clear_providers()
    token_auth.clear_token_routes()
    yield
    # A test may drive the registry to VERIFIED (frozen), after which clear_* is
    # correctly refused. Per-test isolation is the autouse fresh-registry fixture
    # (conftest rebinds lifecycle._default), so tolerate a frozen registry here.
    try:
        clear_providers()
        token_auth.clear_token_routes()
    except token_auth.FrozenRegistryError:
        pass


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeClient:
    host = "1.2.3.4"


class _FakeRequest:
    """Minimal Request stand-in for the seam (no real Starlette needed)."""

    def __init__(self, path="/api/gateway/drain", headers=None):
        self.url = _FakeURL(path)
        self.headers = headers or {}
        self.client = _FakeClient()

        class _State:
            pass

        self.state = _State()


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# ABC + registry
# --------------------------------------------------------------------------


def test_oauth_provider_defaults_supports_token_false():
    assert _OAuthOnly().supports_token is False




class _NonInteractiveProvider(_TokenProvider):
    """A token-only credential with no interactive session."""

    name = "svc-cred"
    display_name = "Service Credential"
    supports_session = False


# --------------------------------------------------------------------------
# Bearer extraction
# --------------------------------------------------------------------------




# --------------------------------------------------------------------------
# authenticate_token (provider stacking)
# --------------------------------------------------------------------------


def test_authenticate_token_accepts_valid():
    register_provider(_TokenProvider(secret="good-secret"))
    token_auth.register_token_route(
        "/api/gateway/drain", provider="tok", capability="drain")
    req = _FakeRequest(headers={"authorization": "Bearer good-secret"})
    principal, unreachable = token_auth.authenticate_token(req)
    assert unreachable is None
    assert principal is not None
    assert principal.provider == "tok"
    assert principal.scopes == ("drain",)


def test_authenticate_token_rejects_wrong_secret():
    register_provider(_TokenProvider(secret="good-secret"))
    token_auth.register_token_route(
        "/api/gateway/drain", provider="tok", capability="drain")
    req = _FakeRequest(headers={"authorization": "Bearer wrong"})
    principal, unreachable = token_auth.authenticate_token(req)
    assert principal is None
    assert unreachable is None


def test_authenticate_token_consults_only_route_owner():
    # Provider binding: only the provider that OWNS the route is consulted; a
    # token another registered provider would accept is rejected here.
    register_provider(_TokenProvider(secret="aaa"))            # name "tok"
    second = _TokenProvider(secret="bbb")
    second.name = "tok2"
    register_provider(second)
    token_auth.register_token_route(
        "/api/gateway/drain", provider="tok2", capability="drain")
    principal, _ = token_auth.authenticate_token(
        _FakeRequest(headers={"authorization": "Bearer bbb"}))
    assert principal is not None and principal.provider == "tok2"
    # "aaa" would be accepted by 'tok', but 'tok' does not own this route.
    principal, _ = token_auth.authenticate_token(
        _FakeRequest(headers={"authorization": "Bearer aaa"}))
    assert principal is None


def test_authenticate_token_unreachable_owner_reports_503():
    # If the ROUTE OWNER's backing store is unreachable, surface it (503),
    # independent of any other registered provider.
    register_provider(_UnreachableTokenProvider())            # name "tok-down"
    register_provider(_TokenProvider(secret="good"))          # name "tok" (not owner)
    token_auth.register_token_route(
        "/api/gateway/drain", provider="tok-down", capability="drain")
    principal, unreachable = token_auth.authenticate_token(
        _FakeRequest(headers={"authorization": "Bearer good"}))
    assert principal is None
    assert unreachable == "tok-down"


def test_authenticate_token_buggy_owner_does_not_crash():
    # A buggy OWNER that raises is caught (not surfaced as unreachable) and
    # never 500s the seam.
    buggy = _BuggyTokenProvider()                             # name "tok-buggy"
    register_provider(buggy)
    token_auth.register_token_route(
        "/api/gateway/drain", provider="tok-buggy", capability="drain")
    principal, unreachable = token_auth.authenticate_token(
        _FakeRequest(headers={"authorization": "Bearer good"}))
    assert principal is None and unreachable is None


# --------------------------------------------------------------------------
# Middleware seam (route-agnostic)
# --------------------------------------------------------------------------


async def _call_next_ok(request):
    from fastapi.responses import JSONResponse

    return JSONResponse({"ok": True}, status_code=200)






def test_seam_rejects_wrong_token_401():
    register_provider(_TokenProvider(secret="good"))
    token_auth.register_token_route("/api/gateway/drain", provider="tok", capability="drain")
    # WAVE-22: the middleware serves (and therefore reaches the wrong-token
    # rejection) only from a VERIFIED generation. Drive the isolated registry
    # through the real freeze → verify transition first.
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()
    req = _FakeRequest(
        path="/api/gateway/drain", headers={"authorization": "Bearer bad"}
    )
    resp = _run(token_auth.token_auth_middleware(req, _call_next_ok))
    assert resp.status_code == 401


