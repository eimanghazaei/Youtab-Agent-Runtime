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

    #: Present only when the frontend has been built. A runner that skipped
    #: the build must not go red for it, and a runner that did the build must
    #: not be allowed to smuggle in an extra public route under cover of it.
    BUILD_CONDITIONAL = {"/assets/{}.css"}

    #: Bypass the *cookie* gate, not authentication. Each carries its own
    #: credential that its handler verifies, and holding them to a session
    #: scope refuses them before that verification ever runs.
    CREDENTIAL_BEARING = {"/api/cron/fire", "/api/mcp/oauth/callback/{}"}

    AUTHORISED_PUBLIC = {
        "/api/auth/csrf", "/api/auth/providers", "/auth/login",
        "/auth/callback", "/auth/password-login", "/auth/logout",
        "/login", "/auth/native/authorize", "/auth/native/token",
        "/auth/native/refresh", "/api/health", "/{}", "/assets/{}.css",
        "/dashboard-plugins/{}/{}",
        # The probe contract and the SPA's pre-login bootstrap.
        "/api/status", "/api/config/defaults", "/api/config/schema",
        "/api/dashboard/themes", "/api/dashboard/plugins",
    } | CREDENTIAL_BEARING

    def test_nothing_else_became_public(self, inventory):
        """The public set is what was authorised, and never larger.

        Checked as a subset rather than an equality because the built and
        unbuilt routers differ by exactly ``/assets/{}.css``. Equality would
        make this fail on a CI runner that did not build the frontend -- red
        for a reason that has nothing to do with authorization, which is the
        kind of failure that teaches people to ignore the check.
        """
        actual = {
            p for p, m in (tuple(x) for x in inventory["http"])
            if required_scope(probe(p), m) == PUBLIC
        }
        assert actual <= self.AUTHORISED_PUBLIC, (
            f"unauthorised public routes: {sorted(actual - self.AUTHORISED_PUBLIC)}"
        )
        missing = self.AUTHORISED_PUBLIC - actual
        assert missing <= self.BUILD_CONDITIONAL, (
            f"authorised public routes absent from the router: {sorted(missing)}"
        )

    def test_the_build_conditional_route_is_public_when_it_exists(self, inventory):
        """And is genuinely conditional, not silently missing in both states."""
        paths = {p for p, _ in (tuple(x) for x in inventory["http"])}
        for path in self.BUILD_CONDITIONAL:
            if path in paths:
                assert required_scope(probe(path), "GET") == PUBLIC

    def test_status_is_public_again(self):
        """Superseded: the withdrawal broke the portal probe and was reverted.

        Kept as an assertion rather than deleted, so the reversal is recorded
        where the withdrawal was. See TestStatusKeepsItsProbeContract.
        """
        assert required_scope("/api/status", "GET") == PUBLIC


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


# --- the findings this slice was opened to resolve ---------------------------


class TestStatusKeepsItsProbeContract:
    """Restored after being withdrawn. The withdrawal broke a real consumer.

    NAS ``fly-provider.ts getInstanceRuntimeStatus`` fetches ``/api/status``
    without a cookie as its sole signal that a wildcard-subdomain agent is
    alive. Holding it to ``ui:read`` returned 403 and surfaced every healthy
    agent as STARTING/down. It is public again, and the reason it is safe to
    be public is a property of its *payload*, which the next test pins.
    """

    def test_it_is_public(self):
        assert required_scope("/api/status", "GET") == PUBLIC
        assert authorize(Principal(user_id="", org_id=""), "/api/status", "GET")

    def test_it_is_not_shadowed_by_the_cluster_grant(self):
        """An exact entry has to beat the prefix, or the fix is cosmetic."""
        from youtab_agent_cli.authz import UI_READ

        assert required_scope("/api/status", "GET") != UI_READ

    def test_the_allowlist_and_the_policy_now_agree(self):
        """They disagreed while /api/status was withdrawn. That is resolved."""
        from youtab_agent_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

        for path in PUBLIC_API_PATHS:
            assert required_scope(path, "GET") in (PUBLIC, None) or path == "/api/cron/fire", (
                f"{path} is on the middleware allowlist but the policy guards it"
            )
        assert "/api/status" in PUBLIC_API_PATHS


class TestProfileListMasksThePrivateFields:
    """The disclosure was in the payload, so the fix is in the payload.

    Holding ``GET /api/profiles`` at ``provider:read`` would have closed it by
    removing the profile picker from every ordinary user. The route stays a
    user capability; four of its fields do not.
    """

    @pytest.fixture
    def records(self):
        return [{
            "name": "default", "description": "d", "skill_count": 3,
            "model": "gpt-x", "provider": "acme",
            "path": "/root/.youtab-agent-runtime", "has_env": True,
        }]

    def test_a_normal_user_sees_none_of_them(self, records):
        from youtab_agent_cli.web_server import (
            PRIVATE_PROFILE_FIELDS, mask_private_profile_fields,
        )

        user = resolve_principal(user_id="someone", org_id="acme", roster={})
        [masked] = mask_private_profile_fields(records, user)
        for field in PRIVATE_PROFILE_FIELDS:
            assert field not in masked, f"{field} still disclosed to a normal user"
        assert masked["restricted"] is True

    def test_the_useful_fields_survive(self, records):
        """Masking that removed the picker's own data would be a regression."""
        from youtab_agent_cli.web_server import mask_private_profile_fields

        user = resolve_principal(user_id="someone", org_id="acme", roster={})
        [masked] = mask_private_profile_fields(records, user)
        assert masked["name"] == "default"
        assert masked["description"] == "d"
        assert masked["skill_count"] == 3

    def test_a_provider_reader_still_sees_them(self, records):
        from youtab_agent_cli.web_server import mask_private_profile_fields

        owner = Principal(user_id="o", org_id="", role=Role.YOUTAB_OWNER,
                          scopes=ROLE_SCOPES[Role.YOUTAB_OWNER])
        [full] = mask_private_profile_fields(records, owner)
        assert full["provider"] == "acme"
        assert full["model"] == "gpt-x"

    def test_an_absent_principal_is_masked_not_trusted(self, records):
        from youtab_agent_cli.web_server import mask_private_profile_fields

        assert "provider" not in mask_private_profile_fields(records, None)[0]

    def test_the_route_itself_stays_a_user_capability(self):
        user = resolve_principal(user_id="someone", org_id="acme", roster={})
        assert authorize(user, "/api/profiles", "GET")


class TestCredentialBearingRoutesVerifyTheirCredential:
    """Public at the gate is only correct if the handler is the real boundary.

    Both were read rather than assumed.
    """

    def test_the_cron_fire_verifier_refuses_without_a_key(self):
        """No JWKS configured must mean refuse, never unsigned decode."""
        from plugins.cron_providers.chronos.verify import verify_nas_fire_token

        assert verify_nas_fire_token(
            token="x.y.z", expected_audience="agent:1", jwks_or_key=None) is None

    def test_the_cron_fire_verifier_refuses_without_an_audience(self):
        from plugins.cron_providers.chronos.verify import verify_nas_fire_token

        assert verify_nas_fire_token(
            token="x.y.z", expected_audience="", jwks_or_key="https://n/jwks") is None

    def test_the_cron_fire_verifier_refuses_an_empty_token(self):
        from plugins.cron_providers.chronos.verify import verify_nas_fire_token

        assert verify_nas_fire_token(
            token="", expected_audience="agent:1", jwks_or_key="https://n/jwks") is None

    def test_the_cron_fire_verifier_rejects_symmetric_algorithms(self):
        """NAS signs asymmetrically; an HS256 token must not verify."""
        import jwt as pyjwt
        from plugins.cron_providers.chronos.verify import verify_nas_fire_token

        forged = pyjwt.encode(
            {"aud": "agent:1", "exp": 9999999999, "purpose": "cron_fire"},
            "secret", algorithm="HS256")
        assert verify_nas_fire_token(
            token=forged, expected_audience="agent:1", jwks_or_key="secret") is None

    def test_the_mcp_callback_compares_state_in_constant_time(self):
        """The OAuth state is the boundary, so how it is compared matters."""
        import inspect

        from youtab_agent_cli.web_routers import mcp

        source = inspect.getsource(mcp.mcp_oauth_callback)
        assert "compare_digest" in source
        assert "expected_state" in source

    def test_both_are_public_at_the_gate(self):
        assert required_scope("/api/cron/fire", "POST") == PUBLIC
        assert required_scope("/api/mcp/oauth/callback/notion", "GET") == PUBLIC

    def test_the_rest_of_their_clusters_is_not(self):
        """The exemption is the one route, not the prefix around it."""
        from youtab_agent_cli.authz import AUTOMATION_MANAGE, TOOL_MANAGE

        assert required_scope("/api/cron/jobs", "GET") == AUTOMATION_MANAGE
        assert required_scope("/api/mcp/servers", "GET") == TOOL_MANAGE


class TestPathRoutesAreMatchedAsPaths:
    """Two rules were written as fixed-segment patterns against ``:path`` routes.

    Every real request carried more segments than the pattern allowed, fell
    through to a refusal, and 403'd a route that was meant to be reachable.
    Caught only because the full suite was run rather than the selected files.
    """

    @pytest.mark.parametrize("path", [
        "/dashboard-plugins/bundledx/dist/index.js",
        "/dashboard-plugins/kanban/assets/deep/nested/main.css",
    ])
    def test_a_multi_segment_plugin_asset_resolves(self, path):
        assert required_scope(path, "GET") == PUBLIC

    @pytest.mark.parametrize("path", [
        "/api/mcp/oauth/callback/notion",
        "/api/mcp/oauth/callback/scoped/server/name",
    ])
    def test_a_multi_segment_callback_resolves(self, path):
        assert required_scope(path, "GET") == PUBLIC


class TestApiDocsProductionPolicy:
    """Decision: operator-only, not public, not removed.

    Removing them outright would need the routes never registered, which is an
    app-construction change rather than a policy one. Holding them at
    ``ops:manage`` gives the same externally-visible result -- an anonymous or
    ordinary caller cannot read the API surface -- and keeps the route
    inventory stable so the policy stays checkable.
    """

    PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]

    @pytest.mark.parametrize("path", PATHS)
    def test_an_anonymous_caller_is_refused(self, path):
        assert not authorize(Principal(user_id="", org_id=""), path, "GET")

    @pytest.mark.parametrize("path", PATHS)
    def test_a_normal_user_is_refused(self, path):
        user = resolve_principal(user_id="someone", org_id="acme", roster={})
        assert not authorize(user, path, "GET")

    @pytest.mark.parametrize("path", PATHS)
    def test_an_operator_holding_ops_manage_is_allowed(self, path):
        from youtab_agent_cli.authz import OPS_MANAGE

        operator = Principal(user_id="op", org_id="",
                             role=Role.YOUTAB_OPERATOR,
                             scopes=frozenset({OPS_MANAGE}))
        assert authorize(operator, path, "GET")

    def test_ops_manage_is_not_in_the_user_baseline(self):
        from youtab_agent_cli.authz import OPS_MANAGE, USER_CAPABILITIES

        assert OPS_MANAGE not in USER_CAPABILITIES


class TestTheUserBaselineWasValidatedRouteByRoute:
    """Three routes the cluster rule handed to every signed-in user wrongly.

    Each was found by reading the endpoint body rather than by looking at the
    prefix, which is the only way this class of error surfaces: all three sit
    in clusters whose other routes are genuinely user capability.
    """

    @pytest.fixture
    def user(self):
        return resolve_principal(user_id="someone", org_id="acme", roster={})

    def test_model_analytics_is_not_a_user_capability(self, user):
        """It selects `model, billing_provider` per session."""
        assert required_scope("/api/analytics/models", "GET") == authz.PROVIDER_READ
        assert not authorize(user, "/api/analytics/models", "GET")

    def test_but_the_caller_keeps_their_own_usage_totals(self, user):
        """Restricting the disclosure must not cost the neighbouring feature."""
        assert authorize(user, "/api/analytics/usage", "GET")

    def test_portal_status_is_not_a_user_capability(self, user):
        """It reports each feature's `current_provider` -- the binding."""
        assert required_scope("/api/portal", "GET") == authz.PROVIDER_READ
        assert not authorize(user, "/api/portal", "GET")

    def test_the_ssh_owner_nonce_is_not_a_user_capability(self, user):
        """It returns a live secret; its own docstring calls it sensitive."""
        assert required_scope("/api/ssh/ownership", "GET") == authz.OPS_MANAGE
        assert not authorize(user, "/api/ssh/ownership", "GET")

    def test_no_ops_route_reaches_the_user_baseline(self, inventory, user):
        """The whole /api/ops cluster, checked against the router not a list."""
        for pair in inventory["http"]:
            path, method = tuple(pair)
            if not path.startswith("/api/ops"):
                continue
            assert not authorize(user, probe(path), method), (
                f"{method} {path} is reachable by an ordinary user"
            )

    def test_no_baseline_scope_is_a_privileged_scope(self):
        """The grant is a set; this is the assertion that it stayed clean."""
        from youtab_agent_cli.authz import USER_CAPABILITIES

        privileged = {
            authz.PROVIDER_READ, authz.PROVIDER_WRITE, authz.CREDENTIAL_READ,
            authz.CREDENTIAL_WRITE, authz.ENGINE_SELECT, authz.TENANT_MANAGE_ANY,
            authz.DEPLOYMENT_MANAGE, authz.EVENTS_READ, authz.OPS_MANAGE,
        }
        assert not (USER_CAPABILITIES & privileged)
