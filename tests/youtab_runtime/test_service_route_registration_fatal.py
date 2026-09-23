"""WAVE-19: security-critical service-route registration is FAIL-CLOSED.

A failure to register the runtime-service or drain provider/route — INCLUDING a
failure the plugin loader swallows — must ABORT dashboard startup BEFORE the app
serves a single request, never leave the route reachable through the interactive
cookie gate.

These tests drive the REAL ``youtab_agent_cli.web_server.app`` lifespan (which
runs ``freeze_token_routes()`` then ``verify_service_route_ownership()``) and the
REAL plugin ``register()`` functions. The only scaffolding is a minimal loader
context (mirroring ``PluginContext.register_dashboard_auth_provider``, which
swallows ``TypeError``/``ValueError``) and a helper that swallows any exception
escaping ``register()`` EXACTLY as ``PluginManager._load_plugin`` does — so the
tests prove the abort happens *despite* the loader's swallow, not because of a
re-raise the real framework would not perform.
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from youtab_agent_cli import web_server
from youtab_agent_cli.dashboard_auth import lifecycle
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.lifecycle import ServiceRouteRegistrationError

import plugins.dashboard_auth.runtime_service as runtime_plugin
import plugins.dashboard_auth.drain as drain_plugin

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
    "X-Youtab-Correlation-Id": "cid-w19",
}


class _LoaderCtx:
    """Mirror of ``PluginContext.register_dashboard_auth_provider``: register the
    provider into the shared registry and SWALLOW ``(TypeError, ValueError)``
    exactly as the real loader wrapper does — so a provider-registration failure
    surfaces to the test the same way it would in production (silently)."""

    def register_dashboard_auth_provider(self, provider) -> None:
        try:
            auth_registry.register_provider(provider)
        except (TypeError, ValueError):
            pass


def _run_plugin_register_like_loader(plugin_module) -> None:
    """Invoke the REAL plugin ``register()`` and swallow anything it raises,
    EXACTLY as ``PluginManager._load_plugin`` does (``except Exception`` →
    log + continue). Proves the abort does not depend on the plugin's exception
    propagating past the loader."""
    try:
        plugin_module.register(_LoaderCtx())
    except Exception:  # noqa: BLE001 — this is precisely what the loader does
        pass


@pytest.fixture(autouse=True)
def _gated(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", RUNTIME_SECRET)
    monkeypatch.setenv("YOUTAB_AGENT_DASHBOARD_DRAIN_SECRET", DRAIN_SECRET)
    web_server.app.state.auth_required = True
    yield
    web_server.app.state.auth_required = False


def _bearer(secret, extra=None):
    h = {"Authorization": f"Bearer {secret}"}
    if extra:
        h.update(extra)
    return h


def _assert_startup_aborts():
    """Entering the app lifespan must raise ServiceRouteRegistrationError and
    therefore never reach ``yield`` — no request can be served."""
    with pytest.raises(ServiceRouteRegistrationError):
        with TestClient(web_server.app):
            pass  # pragma: no cover — lifespan raises before yield


# --- 1. runtime provider registration failure aborts startup ---------------

def test_runtime_provider_registration_failure_aborts_startup(monkeypatch):
    real = auth_registry.register_provider

    def _fail(provider):
        if getattr(provider, "name", None) == "runtime-service":
            raise ValueError("forced provider registration failure")
        return real(provider)

    monkeypatch.setattr(auth_registry, "register_provider", _fail)
    _run_plugin_register_like_loader(runtime_plugin)
    # requirement declared, provider absent (registration failed + swallowed)
    assert auth_registry.get_provider("runtime-service") is None
    _assert_startup_aborts()


# --- 2. runtime prefix ownership failure aborts startup --------------------

def test_runtime_prefix_ownership_failure_aborts_startup(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("forced token-route registration failure")

    monkeypatch.setattr(token_auth, "register_token_route_prefix", _boom)
    _run_plugin_register_like_loader(runtime_plugin)
    assert auth_registry.get_provider("runtime-service") is not None
    assert token_auth.route_owner("/api/runtime/v1/health") is None
    _assert_startup_aborts()


# --- 3. drain provider registration failure aborts startup -----------------

def test_drain_provider_registration_failure_aborts_startup(monkeypatch):
    real = auth_registry.register_provider

    def _fail(provider):
        if getattr(provider, "name", None) == "drain-secret":
            raise ValueError("forced provider registration failure")
        return real(provider)

    monkeypatch.setattr(auth_registry, "register_provider", _fail)
    _run_plugin_register_like_loader(drain_plugin)
    assert auth_registry.get_provider("drain-secret") is None
    _assert_startup_aborts()


# --- 4. drain exact-route ownership failure aborts startup -----------------

def test_drain_route_ownership_failure_aborts_startup(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("forced token-route registration failure")

    monkeypatch.setattr(token_auth, "register_token_route", _boom)
    _run_plugin_register_like_loader(drain_plugin)
    assert auth_registry.get_provider("drain-secret") is not None
    assert token_auth.route_owner(DRAIN_PATH) is None
    _assert_startup_aborts()


# --- 5. conflicting owner aborts startup -----------------------------------

def test_conflicting_owner_aborts_startup():
    # Someone else already owns the runtime prefix. The plugin's own
    # registration then raises TokenRouteOwnershipError (swallowed by the
    # loader), leaving the prefix owned by the wrong provider -> abort.
    token_auth.register_token_route_prefix(
        RUNTIME_PREFIX, provider="not-runtime-service", capability="x")
    _run_plugin_register_like_loader(runtime_plugin)
    owner = token_auth.route_owner("/api/runtime/v1/health")
    assert owner is not None and owner.provider == "not-runtime-service"
    _assert_startup_aborts()


# --- 6. freeze-before-registration aborts startup --------------------------

def test_freeze_before_registration_aborts_startup():
    # Ordering bug: the registry freezes BEFORE the plugin registers. The
    # provider/route registration is then refused (frozen), but the recorded
    # requirement stays unmet -> the lifespan verification aborts, rather than
    # silently disabling a surface the operator configured.
    token_auth.freeze_token_routes()
    _run_plugin_register_like_loader(runtime_plugin)
    assert auth_registry.get_provider("runtime-service") is None
    _assert_startup_aborts()


# --- 7. a plugin-discovery exception cannot be swallowed into a served route -

def test_loader_swallow_does_not_prevent_abort(monkeypatch):
    # Prove the two facts together: (a) the plugin's register() raises, (b) the
    # loader swallows it (no exception escapes), yet (c) startup still aborts.
    raised = {"n": 0}

    def _boom(*a, **k):
        raised["n"] += 1
        raise RuntimeError("forced token-route registration failure")

    monkeypatch.setattr(token_auth, "register_token_route_prefix", _boom)
    # (b) the loader-equivalent swallow does not propagate:
    _run_plugin_register_like_loader(runtime_plugin)  # must NOT raise
    assert raised["n"] == 1  # (a) register() really did hit the failure
    # (c) startup still aborts despite the swallow:
    _assert_startup_aborts()


# --- 8/9. after an abort, no request can reach the affected route ----------

def test_no_request_reaches_route_after_abort(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("forced token-route registration failure")

    monkeypatch.setattr(token_auth, "register_token_route_prefix", _boom)
    _run_plugin_register_like_loader(runtime_plugin)
    # The lifespan never yields, so no client can be entered and no request —
    # cookie, runtime token, drain token, or unauthenticated — can be served.
    with pytest.raises(ServiceRouteRegistrationError):
        with TestClient(web_server.app) as c:  # pragma: no cover
            c.get(HEALTH)
            c.get(HEALTH, headers=_bearer(RUNTIME_SECRET))
            c.post(DRAIN_PATH, headers=_bearer(DRAIN_SECRET), json={})
            c.get(HEALTH, cookies={"session": "forged"})


# --- 10. the fatal error/log carries no supplied token or secret -----------

def test_abort_error_and_logs_contain_no_secret(monkeypatch, caplog):
    def _boom(*a, **k):
        raise RuntimeError("forced token-route registration failure")

    monkeypatch.setattr(token_auth, "register_token_route_prefix", _boom)
    with caplog.at_level(logging.DEBUG):
        _run_plugin_register_like_loader(runtime_plugin)
        try:
            with TestClient(web_server.app):
                pass
        except ServiceRouteRegistrationError as exc:
            message = str(exc)
        else:  # pragma: no cover
            pytest.fail("startup did not abort")
    assert RUNTIME_SECRET not in message
    assert DRAIN_SECRET not in message
    assert RUNTIME_SECRET not in caplog.text
    assert DRAIN_SECRET not in caplog.text
    # the diagnostic still names the provider + route + reason
    assert "runtime-service" in message and RUNTIME_PREFIX in message


# --- 11. a clean startup still authorizes/denies correctly -----------------

@pytest.fixture()
def healthy_client():
    _run_plugin_register_like_loader(runtime_plugin)
    _run_plugin_register_like_loader(drain_plugin)
    # both requirements are now met; the lifespan verification passes
    with TestClient(web_server.app) as c:
        yield c


def test_healthy_runtime_token_reaches_runtime(healthy_client):
    r = healthy_client.get(HEALTH, headers=_bearer(RUNTIME_SECRET, IDENT))
    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert RUNTIME_SECRET not in r.text


def test_healthy_drain_token_reaches_drain(healthy_client):
    r = healthy_client.post(DRAIN_PATH, headers=_bearer(DRAIN_SECRET), json={"action": "drain"})
    assert r.status_code == 200
    assert DRAIN_SECRET not in r.text


def test_healthy_runtime_token_cannot_reach_drain(healthy_client):
    r = healthy_client.post(DRAIN_PATH, headers=_bearer(RUNTIME_SECRET), json={"action": "drain"})
    assert r.status_code in (401, 403)
    assert r.status_code != 200


def test_healthy_drain_token_cannot_reach_runtime(healthy_client):
    r = healthy_client.get(HEALTH, headers=_bearer(DRAIN_SECRET, IDENT))
    assert r.status_code in (401, 403)
    assert r.status_code != 200


# --- surface intentionally disabled (no secret) declares no requirement ----

def test_disabled_surface_declares_no_requirement_and_starts(monkeypatch):
    # No secrets -> the plugins skip (surface off), declare no requirement, and
    # the lifespan verification passes: a clean deployment is not made fatal.
    monkeypatch.delenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", raising=False)
    monkeypatch.delenv("YOUTAB_AGENT_DASHBOARD_DRAIN_SECRET", raising=False)
    _run_plugin_register_like_loader(runtime_plugin)
    _run_plugin_register_like_loader(drain_plugin)
    assert auth_registry.get_provider("runtime-service") is None
    assert lifecycle._default._required_ownerships == []
    with TestClient(web_server.app):  # must NOT raise
        pass


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
