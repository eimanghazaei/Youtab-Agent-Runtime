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

    # Real startup ordering (WAVE-22): the seam serves only from a VERIFIED
    # generation. This fixture registers the providers/routes MANUALLY (so the
    # real lifespan cannot run here without double-registering), so it drives
    # the isolated registry through the real declare → freeze → verify →
    # VERIFIED transition itself — exactly what the dashboard lifespan does
    # before accepting traffic.
    token_auth.require_route_ownership(
        provider="runtime-service", path=RUNTIME_PREFIX, is_prefix=True,
        capability="runtime")
    token_auth.require_route_ownership(
        provider="drain-secret", path=DRAIN_PATH, is_prefix=False,
        capability="drain")
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()

    c = TestClient(web_server.app)
    try:
        yield c
    finally:
        # The registry is now VERIFIED (frozen); clear_* is correctly refused
        # after freeze. Per-test isolation is the autouse fresh-registry fixture
        # rebinding lifecycle._default, so no manual clear is needed here.
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


# --- §3 route-boundary attacks through the REAL app (HTTP), not helpers -----

@pytest.mark.parametrize("path", [
    "/api/runtime/v1evil",              # sibling string-prefix
    "/api/runtime/v1evil/health",       # sibling subpath
    "/api/runtime/v1/../gateway/drain",  # dot-segment cannot reach drain
])
def test_boundary_paths_never_authenticate_as_runtime(client, path):
    # A valid runtime bearer + identity on a path that is NOT the runtime
    # surface must never succeed (200). It is either not a token route (cookie
    # gate 401) or an unmatched route (404) — never an authenticated 200, and
    # never a 500.
    r = client.get(path, headers=_bearer(RUNTIME_SECRET, IDENT))
    assert r.status_code != 200
    assert r.status_code != 500


def test_percent_encoded_separator_resolves_consistently_no_crossing(client):
    # The ASGI stack decodes ``%2F`` to ``/`` BEFORE both the router and the
    # token seam see the path, so ``/api/runtime%2Fv1/health`` becomes the
    # runtime surface's own ``/api/runtime/v1/health`` for BOTH. Ownership and
    # routing therefore agree — the request is served as the runtime owner it
    # decodes to (200), never crossed to a different owner, never a 500. The
    # unit test proves the LITERAL encoded string is unowned; here we prove the
    # decoded scope path the framework routes on is what the seam uses too.
    r = client.get("/api/runtime%2Fv1/health", headers=_bearer(RUNTIME_SECRET, IDENT))
    assert r.status_code != 500
    # decodes to the runtime owner's own health route -> authorized 200; a
    # non-decoding stack would 401/404 instead. Either way it is never drain.
    assert r.status_code in (200, 401, 403, 404)
    # a DRAIN bearer on the same encoded runtime path must still be denied.
    assert client.get("/api/runtime%2Fv1/health",
                      headers=_bearer(DRAIN_SECRET, IDENT)).status_code != 200


def test_double_slash_stays_within_runtime_owner_no_crossing(client):
    # A double slash is still under the runtime prefix (owned by runtime), so it
    # can only ever reach the runtime surface — never drain — and must not 500.
    r = client.get("/api/runtime/v1//health", headers=_bearer(RUNTIME_SECRET, IDENT))
    assert r.status_code != 500
    # whatever it resolves to (200 if the route matches, 404 if not) it is the
    # runtime owner's decision, never drain's.
    assert r.status_code in (200, 404, 403, 401)


# --- WAVE-22: lifespan-disabled real app must fail closed (VERIFIED-only) ----

@pytest.fixture()
def _unverified_client(tmp_path, monkeypatch):
    """The REAL app with providers/routes registered but the registry left
    UNVERIFIED — i.e. the ASGI lifespan was disabled/bypassed/misconfigured so it
    never froze+verified. Bare ``TestClient`` (no ``with``) does NOT run the
    lifespan, so the shared registry stays BUILDING. This is exactly the
    configuration mistake WAVE-22 defends against."""
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", RUNTIME_SECRET)
    monkeypatch.setenv("YOUTAB_AGENT_DASHBOARD_DRAIN_SECRET", DRAIN_SECRET)
    web_server.app.state.auth_required = True
    auth_registry.clear_providers()
    token_auth.clear_token_routes()
    auth_registry.register_provider(
        RuntimeServiceProvider(secret=RUNTIME_SECRET, scope="runtime"))
    auth_registry.register_provider(
        DrainSecretProvider(secret=DRAIN_SECRET, scope="drain"))
    token_auth.register_token_route_prefix(
        RUNTIME_PREFIX, provider="runtime-service", capability="runtime")
    token_auth.register_token_route(
        DRAIN_PATH, provider="drain-secret", capability="drain")
    # DELIBERATELY no freeze / no verify: the registry stays BUILDING.
    from youtab_agent_cli.dashboard_auth.lifecycle import LifecycleState
    assert token_auth.lifecycle.registry_state() is LifecycleState.BUILDING
    c = TestClient(web_server.app)
    try:
        yield c
    finally:
        auth_registry.clear_providers()
        token_auth.clear_token_routes()
        web_server.app.state.auth_required = False


def test_lifespan_disabled_runtime_route_fails_closed(_unverified_client):
    # A VALID runtime bearer + identity, on a lifespan-disabled server, must be
    # refused with 503 service_unverified — never authenticated, never 500, never
    # a fall-through to the cookie gate.
    r = _unverified_client.get(HEALTH, headers=_bearer(RUNTIME_SECRET, IDENT))
    assert r.status_code == 503
    assert r.json().get("error") == "service_unverified"
    assert RUNTIME_SECRET not in r.text


def test_lifespan_disabled_drain_route_fails_closed(_unverified_client):
    # Likewise the drain surface: a valid drain bearer is refused 503 while
    # unverified, and no drain side effect can run (the seam denies before the
    # handler).
    r = _unverified_client.post(
        DRAIN_PATH, headers=_bearer(DRAIN_SECRET), json={"action": "cancel"})
    assert r.status_code == 503
    assert r.json().get("error") == "service_unverified"
    assert DRAIN_SECRET not in r.text


def test_no_env_var_bypasses_verification(_unverified_client, monkeypatch):
    # Behavioural: a battery of plausible "test/dev bypass" env vars must NOT
    # open the gate. The registry is unverified; the route stays 503 regardless.
    for name in (
        "YOUTAB_AGENT_SKIP_VERIFICATION", "YOUTAB_AGENT_TEST_MODE",
        "YOUTAB_AGENT_DEV", "YOUTAB_AGENT_INSECURE", "YOUTAB_AGENT_DEBUG",
        "TESTING", "CI", "PYTEST_CURRENT_TEST_BYPASS",
        "YOUTAB_AGENT_DASHBOARD_AUTH_VERIFIED", "YOUTAB_AGENT_ALLOW_UNVERIFIED",
    ):
        monkeypatch.setenv(name, "1")
    r = _unverified_client.get(HEALTH, headers=_bearer(RUNTIME_SECRET, IDENT))
    assert r.status_code == 503
    assert r.json().get("error") == "service_unverified"


def test_serving_gate_source_has_no_env_bypass():
    # Source-level: the serving gate and its fail-closed helper must not consult
    # any environment variable — there is no env/test bypass of VERIFIED-only
    # serving to regress.
    import inspect
    from youtab_agent_cli.dashboard_auth import token_auth as _ta
    for fn in (_ta.token_auth_middleware, _ta._registry_verified):
        src = inspect.getsource(fn)
        assert "environ" not in src and "getenv" not in src, (
            f"{fn.__name__} must not read environment variables")


def test_exact_drain_and_prefix_runtime_are_independent(client):
    # Exact drain route (owned by drain) and the runtime prefix (owned by
    # runtime) each authenticate ONLY their own bearer, end to end.
    ok_drain = client.post(DRAIN_PATH, headers=_bearer(DRAIN_SECRET),
                           json={"action": "cancel"})
    assert ok_drain.status_code == 200
    ok_runtime = client.get(HEALTH, headers=_bearer(RUNTIME_SECRET, IDENT))
    assert ok_runtime.status_code == 200
    # cross use denied (repeat of the isolation invariant at HTTP level)
    assert client.post(DRAIN_PATH, headers=_bearer(RUNTIME_SECRET),
                       json={"action": "drain"}).status_code != 200
    assert client.get(HEALTH, headers=_bearer(DRAIN_SECRET, IDENT)).status_code != 200


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
