"""End-to-end service-token isolation through the REAL app middleware stack.

Drives ``youtab_agent_cli.web_server.app`` via ``TestClient`` on a GATED bind
(``auth_required=True``) with the REAL ``RuntimeServiceProvider`` and
``DrainSecretProvider`` registered and their real token routes bound to their
owning provider. Every principal here is a real ``dashboard_auth.base.
TokenPrincipal`` minted by a real ``verify_token`` — never a hand-forged
fixture (Owner invariant #8). This is the regression that would have caught the
historical ``_authorization_gate`` 500 AND the cross-provider drain escalation.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from youtab_agent_cli import web_server
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth

from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider
from plugins.dashboard_auth.drain import DrainSecretProvider

# Strong, DISTINCT per-surface secrets (>= 256-bit url-safe).
RUNTIME_SECRET = "RT-" + "u9Qk3Zk1cJ7pR2vN8mB4tL6wX0sY5aH1dG3fE2iK7oP"
DRAIN_SECRET = "DR-" + "z2Wm5Xb8nQ4rT6yU1oI3pA7sD9fG0hJ2kL4vC6xB8nM"

RUNTIME_PREFIX = "/api/runtime/v1/"
DRAIN_PATH = "/api/gateway/drain"
HEALTH = "/api/runtime/v1/health"

IDENT = {
    "X-Youtab-Tenant-Id": "tenantA",
    "X-Youtab-User-Id": "userA",
    "X-Youtab-Roles": "service",
    "X-Youtab-Correlation-Id": "cid-iso",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", RUNTIME_SECRET)
    monkeypatch.setenv("YOUTAB_AGENT_DASHBOARD_DRAIN_SECRET", DRAIN_SECRET)

    # Gated (hosted) bind: this is what makes _principal_for_request take the
    # token-principal branch — the exact production condition the pinned code
    # crashed on.
    web_server.app.state.auth_required = True

    auth_registry.clear_providers()
    token_auth.clear_token_routes()
    auth_registry.register_provider(
        RuntimeServiceProvider(secret=RUNTIME_SECRET, scope="runtime"))
    auth_registry.register_provider(
        DrainSecretProvider(secret=DRAIN_SECRET, scope="drain"))
    # Bind each route to its owning provider + required capability.
    token_auth.register_token_route_prefix(
        RUNTIME_PREFIX, provider="runtime-service", capability="runtime")
    token_auth.register_token_route(
        DRAIN_PATH, provider="drain-secret", capability="drain")

    c = TestClient(web_server.app)
    try:
        yield c
    finally:
        auth_registry.clear_providers()
        token_auth.clear_token_routes()
        web_server.app.state.auth_required = False


def _bearer(secret, extra=None):
    h = {"Authorization": f"Bearer {secret}"}
    if extra:
        h.update(extra)
    return h


# --- the ladder on the owner's own surface --------------------------------

def test_runtime_unauth_401(client):
    r = client.get(HEALTH)
    assert r.status_code == 401
    assert "login_url" not in r.text  # token-seam 401, not the cookie gate


def test_runtime_malformed_bearer_401(client):
    r = client.get(HEALTH, headers=_bearer("not-the-secret-xxxxxxxxxxxxxxxxxxxx"))
    assert r.status_code == 401


def test_runtime_correct_bearer_without_identity_403(client):
    r = client.get(HEALTH, headers=_bearer(RUNTIME_SECRET))
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "identity_unverified"


def test_runtime_correct_bearer_with_identity_200(client):
    r = client.get(HEALTH, headers=_bearer(RUNTIME_SECRET, IDENT))
    assert r.status_code == 200
    assert r.json().get("ok") is True
    # no secret leakage into the response
    assert RUNTIME_SECRET not in r.text


def test_drain_correct_bearer_succeeds(client):
    r = client.post(DRAIN_PATH, headers=_bearer(DRAIN_SECRET), json={"action": "cancel"})
    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert DRAIN_SECRET not in r.text


# --- cross-provider isolation (the escalation the fix closes) --------------

def test_runtime_token_cannot_reach_drain(client):
    # Historically (blanket exemption) this returned 200 and drained. The seam
    # now consults only the drain owner, which does not recognise the runtime
    # secret -> 401. Never 200.
    r = client.post(DRAIN_PATH, headers=_bearer(RUNTIME_SECRET), json={"action": "drain"})
    assert r.status_code in (401, 403)
    assert r.status_code != 200


def test_drain_token_cannot_reach_runtime(client):
    r = client.get(HEALTH, headers=_bearer(DRAIN_SECRET, IDENT))
    assert r.status_code in (401, 403)
    assert r.status_code != 200


# --- neither service token reaches interactive/admin routes ----------------

@pytest.mark.parametrize("secret", [RUNTIME_SECRET, DRAIN_SECRET])
def test_service_token_cannot_reach_interactive_route(client, secret):
    # /api/config is not a token route for either provider -> the token seam
    # passes through, token_authenticated stays False, the interactive cookie
    # gate denies (401). Never 200.
    r = client.get("/api/config", headers=_bearer(secret))
    assert r.status_code in (401, 403)
    assert r.status_code != 200


# --- no AttributeError / 500 on an accepted service bearer -----------------

def test_accepted_service_bearer_never_500(client):
    # The whole point: an accepted bearer must reach the contract (200/403),
    # never the historical 500 from _authorization_gate calling .has() on a
    # TokenPrincipal.
    for headers, expected in [
        (_bearer(RUNTIME_SECRET, IDENT), 200),
        (_bearer(RUNTIME_SECRET), 403),
    ]:
        r = client.get(HEALTH, headers=headers)
        assert r.status_code == expected
        assert r.status_code != 500


# --- the plugins declare provider ownership (not just direct registration) --

def test_plugins_register_provider_bound_routes():
    """The real plugin register(ctx) must bind its route to ITS provider."""
    captured = {}

    class _Ctx:
        def register_dashboard_auth_provider(self, provider):
            captured["provider"] = provider

    import os as _os

    token_auth.clear_token_routes()
    auth_registry.clear_providers()
    _os.environ["YOUTAB_AGENT_RUNTIME_SERVICE_SECRET"] = RUNTIME_SECRET
    _os.environ["YOUTAB_AGENT_DASHBOARD_DRAIN_SECRET"] = DRAIN_SECRET
    try:
        import plugins.dashboard_auth.runtime_service as rs
        import plugins.dashboard_auth.drain as dr
        rs.register(_Ctx())
        dr.register(_Ctx())
        rt_owner = token_auth.route_owner("/api/runtime/v1/agents")
        dr_owner = token_auth.route_owner("/api/gateway/drain")
        assert rt_owner is not None and rt_owner.provider == "runtime-service"
        assert rt_owner.capability == "runtime"
        assert dr_owner is not None and dr_owner.provider == "drain-secret"
        assert dr_owner.capability == "drain"
    finally:
        token_auth.clear_token_routes()
        auth_registry.clear_providers()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
