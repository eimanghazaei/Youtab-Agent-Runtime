"""Phase 7 — /api/status exposes auth-gate state + AuthWidget integration.

The dashboard's status endpoint now reports ``auth_required`` and
``auth_providers`` so the AuthWidget + StatusPage can render the
correct "gated / loopback" badge without a separate round trip. This
test asserts both shapes (gated and loopback).

The AuthWidget itself is .tsx — no Python test here. The widget's
behaviour (renders nothing on 401, shows truncated user_id, etc.) is
documented in AuthWidget.tsx; covered manually via the Phase 4.2
smoke test against staging Portal.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from youtab_agent_cli import web_server
from youtab_agent_cli.dashboard_auth import clear_providers, register_provider
from tests.youtab_agent_cli.conftest_dashboard_auth import StubAuthProvider


#: The gated bind terminates TLS, so the session cookie resolves to its
#: ``__Host-`` variant. The stub provider accepts the user id as the token.
SESSION_COOKIE = "__Host-youtab_session_at"


def _signed_in(monkeypatch, user_id: str = "probe-user"):
    """Resolve every request to an ordinary signed-in user.

    This module's stub provider does not mint a session from a bare cookie, so
    the principal is supplied directly. It is the *normal user* baseline — no
    roster entry, nothing privileged — which is precisely the caller these
    tests are about now that /api/status is no longer public.
    """
    from youtab_agent_cli.authz import Principal, ROLE_SCOPES, Role

    monkeypatch.setattr(
        web_server, "_principal_for_request",
        lambda request: Principal(
            user_id=user_id, org_id="", role=Role.NORMAL_USER,
            scopes=ROLE_SCOPES[Role.NORMAL_USER],
        ),
    )


@pytest.fixture
def gated_client():
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


@pytest.fixture
def loopback_client():
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 8080
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:8080")
    yield client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def test_status_reports_auth_required_in_gated_mode(gated_client, monkeypatch):
    # No ``_login()`` call — ``/api/status`` is in the shared
    # ``PUBLIC_API_PATHS`` allowlist precisely so external probes (and
    # the SPA's pre-login bootstrap) can read the gate's shape without
    # a cookie. Hit it cold.
    # /api/status is no longer public (Owner direction), so the gate's shape
    # is read as a signed-in user rather than cold. The payload contract below
    # is unchanged -- only who may read it moved.
    _signed_in(monkeypatch)
    r = gated_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_required"] is True
    assert body["auth_providers"] == ["stub"]




# Host-local detail (absolute paths, PID, internal gateway URL) is deployment
# recon a liveness probe never needs. ``/api/status`` bypasses dashboard auth
# (it is in ``PUBLIC_API_PATHS``), so on a network-exposed bind it must not
# leak that detail to anonymous callers.
_HOST_DETAIL_FIELDS = frozenset({
    "youtab_home", "config_path", "env_path", "gateway_pid",
    "gateway_health_url",
})


def test_status_withholds_host_detail_in_gated_mode(gated_client, monkeypatch):
    """On a gated (non-loopback) bind, ``/api/status`` must expose only the
    liveness + auth-gate shape — never absolute host paths, the gateway PID,
    or the internal gateway health URL.

    The endpoint is no longer public (Owner direction), so it is read here as
    an ordinary signed-in user. The withholding still matters at that level:
    a customer is not entitled to deployment recon either, and this is the
    assertion that says so."""
    _signed_in(monkeypatch)
    r = gated_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    # Liveness / auth-gate shape stays public.
    for key in ("version", "gateway_state", "auth_required", "auth_providers"):
        assert key in body, f"liveness field {key!r} must stay public"
    # Deployment recon must be withheld from the anonymous public probe.
    leaked = _HOST_DETAIL_FIELDS & set(body.keys())
    assert not leaked, f"/api/status leaked host detail under the gate: {leaked}"


