"""Authorization refuses what it does not recognise, on both surfaces.

Before this, :func:`authz.authorize` returned ``True`` for any route no table
described. That meant adding an endpoint published it, and that forgetting to
classify one was indistinguishable from deciding it was open — with an
interactive shell, arbitrary file write and the endpoint that rewrites the
agent's system prompt among the routes nobody had classified.

Three things are held here:

* every route the real router builds resolves to a decision, and an invented
  one resolves to a refusal;
* the flip did not take the product away — the capabilities an ordinary
  signed-in user already had still resolve to scopes that user holds;
* every WebSocket enforces a scope at the upgrade, because Starlette's HTTP
  middleware never runs on one and the flip does not reach them.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.youtab.route_inventory import collect  # noqa: E402
from youtab_agent_cli import authz  # noqa: E402
from youtab_agent_cli.authz import (  # noqa: E402
    AUTHENTICATED,
    PUBLIC,
    Principal,
    ROLE_SCOPES,
    Role,
    authorize,
    required_scope,
    resolve_principal,
)


@pytest.fixture(scope="module")
def app():
    from youtab_agent_cli.web_server import app as application

    return application


@pytest.fixture(scope="module")
def inventory(app):
    return collect(app)


def probe(path: str) -> str:
    """A concrete request path for a normalised template."""
    return "/" if path == "/" else path.replace("{}", "x")


# --- the flip ---------------------------------------------------------------


class TestUnknownRoutesAreRefused:
    def test_an_unclassified_api_route_resolves_to_nothing(self):
        assert required_scope("/api/not-a-real-endpoint", "GET") is None
        assert required_scope("/api/not-a-real-endpoint", "POST") is None

    def test_and_is_therefore_refused_for_everyone(self):
        """Including the Owner. An unmapped route is refused, not escalated."""
        owner = Principal(user_id="o", org_id="", role=Role.YOUTAB_OWNER,
                          scopes=ROLE_SCOPES[Role.YOUTAB_OWNER])
        assert not authorize(owner, "/api/not-a-real-endpoint", "GET")
        assert not authorize(owner, "/api/not-a-real-endpoint", "DELETE")

    def test_an_unauthenticated_caller_holds_nothing(self):
        anonymous = Principal(user_id="", org_id="")
        assert not authorize(anonymous, "/api/sessions", "GET")
        assert not authorize(anonymous, "/api/env", "GET")

    def test_authenticated_class_needs_a_session_and_nothing_more(self):
        assert required_scope("/api/auth/me", "GET") == AUTHENTICATED
        assert not authorize(Principal(user_id="", org_id=""), "/api/auth/me", "GET")
        assert authorize(Principal(user_id="u", org_id=""), "/api/auth/me", "GET")


class TestEveryRealRouteResolves:
    def test_no_http_route_is_unclassified(self, inventory):
        """The whole router, not a list somebody maintains by hand."""
        unmapped = [
            f"{m} {p}" for p, m in (tuple(x) for x in inventory["http"])
            if required_scope(probe(p), m) is None
        ]
        assert unmapped == []

    def test_the_surface_is_the_size_it_should_be(self, inventory):
        """A collector that found nothing would satisfy the check above."""
        assert inventory["counts"]["http_route_methods"] > 250
        assert inventory["counts"]["websocket"] == 7


class TestTheSpaCatchAllCannotReopenTheSurface:
    """The one rule that could quietly undo the flip.

    ``GET /{full_path:path}`` matches everything no other route claimed. As a
    plain pattern it would also match a new ``/api`` endpoint nobody had
    classified, and the entire surface would be public again.
    """

    def test_a_client_side_route_is_public(self):
        assert required_scope("/settings", "GET") == PUBLIC
        assert required_scope("/chat/abc123", "GET") == PUBLIC

    @pytest.mark.parametrize("path", [
        "/api/not-a-real-endpoint",
        "/api/deeply/nested/invention",
        "/auth/not-a-real-flow",
    ])
    def test_but_an_application_root_never_falls_through_to_it(self, path):
        assert required_scope(path, "GET") is None
        assert not authorize(Principal(user_id="u", org_id=""), path, "GET")

    def test_the_root_itself_is_public_in_both_build_states(self):
        """One registry key, two endpoint bodies.

        ``serve_spa`` when the frontend is built, ``no_frontend`` when it is
        not. Public is right for both: the built form returns the login shell,
        the unbuilt form returns a 404 JSON. A runner that skipped the
        frontend build must not go red for the wrong reason.
        """
        assert required_scope("/", "GET") == PUBLIC


# --- the flip did not remove the product ------------------------------------


class TestOrdinaryUsersKeepTheirCapabilities:
    """Fail-closed must not mean fail-empty.

    Every route below was reachable by a signed-in user before the flip,
    because it was unguarded. If the flip left them refused, the change would
    have removed the product rather than secured it.
    """

    @pytest.fixture
    def user(self):
        # Absent from the roster, which is the ordinary case: the roster
        # grants elevation, it is not what makes someone a user.
        return resolve_principal(user_id="someone", org_id="acme", roster={})

    @pytest.mark.parametrize("path,method", [
        ("/api/sessions", "GET"),
        ("/api/sessions/abc", "DELETE"),
        ("/api/profiles", "GET"),
        ("/api/profiles/abc/soul", "GET"),
        ("/api/profiles/abc/soul", "PUT"),
        ("/api/git/status", "GET"),
        ("/api/skills", "GET"),
        ("/api/fs/read", "GET"),
        ("/api/files/list", "GET"),
        ("/api/memory", "GET"),
        ("/api/memory/reset", "POST"),
        ("/api/tools", "GET"),
        ("/api/mcp/servers", "GET"),
        ("/api/cron/jobs", "GET"),
        ("/api/messaging/channels", "GET"),
        ("/api/plugins/kanban/board", "GET"),
        ("/api/plugins/kanban/tasks", "POST"),
        ("/api/dashboard/capabilities", "GET"),
        ("/api/config/defaults", "GET"),
    ])
    def test_a_normal_user_still_reaches_it(self, user, path, method):
        assert authorize(user, path, method), f"{method} {path} was taken away"


class TestPrivilegedSurfaceStaysPrivileged:
    @pytest.fixture
    def user(self):
        return resolve_principal(user_id="someone", org_id="acme", roster={})

    @pytest.mark.parametrize("path,method", [
        ("/api/env", "GET"),
        ("/api/env", "PUT"),
        ("/api/env/reveal", "POST"),
        ("/api/credentials/pool", "GET"),
        ("/api/model/set", "POST"),
        ("/api/model/options", "GET"),
        ("/api/providers/oauth", "GET"),
        ("/api/gateway/stop", "POST"),
        ("/api/ops/anything", "GET"),
        ("/docs", "GET"),
        ("/openapi.json", "GET"),
        # Read from the endpoint body before the cluster rule was applied.
        ("/api/plugins/kanban/model-options", "GET"),
        ("/api/plugins/kanban/profiles", "GET"),
        ("/api/profiles/abc/model", "PUT"),
    ])
    def test_a_normal_user_is_refused(self, user, path, method):
        assert not authorize(user, path, method), f"{method} {path} was opened"

    def test_the_engine_bypass_is_closed(self):
        """Both writes of the same binding are held to the same scope."""
        assert (required_scope("/api/profiles/abc/model", "PUT")
                == required_scope("/api/model/set", "POST")
                == authz.ENGINE_SELECT)

    def test_restricting_the_roster_read_did_not_cost_the_editor(self):
        user = resolve_principal(user_id="someone", org_id="acme", roster={})
        assert authorize(user, "/api/plugins/kanban/profiles/abc", "PATCH")
        assert authorize(
            user, "/api/plugins/kanban/profiles/abc/describe-auto", "POST")


class TestTheLoginFlowStaysReachable:
    """Every one of these has to answer before a session can exist."""

    @pytest.mark.parametrize("path,method", [
        ("/api/auth/csrf", "GET"),
        ("/api/auth/providers", "GET"),
        ("/auth/login", "GET"),
        ("/auth/callback", "GET"),
        ("/auth/password-login", "POST"),
        ("/auth/logout", "POST"),
        ("/login", "GET"),
        ("/auth/native/authorize", "GET"),
        ("/auth/native/token", "POST"),
        ("/auth/native/refresh", "POST"),
        ("/api/health", "GET"),
        ("/assets/index-a1b2.css", "GET"),
        ("/dashboard-plugins/kanban/main.js", "GET"),
    ])
    def test_it_is_public(self, path, method):
        assert required_scope(path, method) == PUBLIC
        assert authorize(Principal(user_id="", org_id=""), path, method)

    def test_nothing_else_became_public(self, inventory):
        """The public set is exactly what was authorised, and no larger."""
        expected = {
            "/api/auth/csrf", "/api/auth/providers", "/auth/login",
            "/auth/callback", "/auth/password-login", "/auth/logout",
            "/login", "/auth/native/authorize", "/auth/native/token",
            "/auth/native/refresh", "/api/health", "/{}", "/assets/{}.css",
            "/dashboard-plugins/{}/{}",
        }
        actual = {
            p for p, m in (tuple(x) for x in inventory["http"])
            if required_scope(probe(p), m) == PUBLIC
        }
        assert actual == expected

    def test_status_is_not_public(self):
        """Explicitly withdrawn. See the report note about the portal probe."""
        assert required_scope("/api/status", "GET") != PUBLIC


# --- WebSockets -------------------------------------------------------------


class TestEveryWebSocketEnforcesAScope:
    """HTTP middleware never runs on an upgrade, so the flip does not reach here.

    Asserted against the handler source rather than by driving seven sockets:
    what has to be true is that each one calls the scope gate before it
    accepts, and that is a property of the handler, not of one connection.
    """

    SOCKETS = {
        "/api/pty": "PTY_SCOPE",
        "/api/console": "CONSOLE_SCOPE",
        "/api/ws": "WS_SCOPE",
        "/api/pub": "EVENTS_READ",
        "/api/audio/speak-stream": "AUDIO_STREAM_SCOPE",
        "/api/events": "EVENTS_READ",
    }

    def _handler_source(self, app, path):
        for route in app.routes:
            if getattr(route, "path", None) == path and not getattr(
                route, "methods", None
            ):
                return inspect.getsource(inspect.unwrap(route.endpoint))
        raise AssertionError(f"no WebSocket route at {path}")

    @pytest.mark.parametrize("path", sorted(SOCKETS))
    def test_it_calls_the_scope_gate(self, app, path):
        source = self._handler_source(app, path)
        assert "_ws_scope_ok" in source, f"{path} accepts without a scope check"
        if "ws.accept()" in source:
            assert source.index("_ws_scope_ok") < source.index("ws.accept()"), (
                f"{path} checks the scope after accepting the upgrade"
            )
        else:
            # Hands the socket to a delegate (tui_gateway) that accepts it, so
            # the gate only has to precede the handoff.
            assert source.index("_ws_scope_ok") < source.index("handle_ws(ws)")

    @pytest.mark.parametrize("path,const", sorted(SOCKETS.items()))
    def test_it_enforces_the_scope_named_for_it(self, app, path, const):
        assert const in self._handler_source(app, path)

    def test_the_plugin_socket_enforces_through_its_own_gate(self):
        """It delegates, so the check lives in the helper it delegates to."""
        from plugins.kanban.dashboard import plugin_api

        source = inspect.getsource(plugin_api._ws_upgrade_authorized)
        assert "_ws_scope_ok" in source
        assert "PLUGIN_USE" in source

    def test_the_shell_socket_is_not_held_to_a_read_scope(self):
        """An interactive shell is a write, whatever else it is."""
        assert authz.PTY_SCOPE == authz.SESSION_WRITE
        reader = Principal(user_id="r", org_id="",
                           scopes=frozenset({authz.SESSION_READ}))
        assert not reader.has(authz.PTY_SCOPE)
