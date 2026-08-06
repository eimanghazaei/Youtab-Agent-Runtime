"""The route authorization registry says what it means, and matches the router.

The registry is data, and data that nobody checks is a document rather than a
control. These tests hold three separate things:

* its own invariants — no duplicate decision, no unknown method, no
  unjustified public entry — enforced at construction so a malformed registry
  cannot be imported at all, let alone serve a request;
* its agreement with the router the application really builds, so an entry for
  a route that no longer exists is caught as staleness rather than read as
  coverage;
* its agreement with ``authz.ROUTE_SCOPES`` wherever both describe the same
  route, because two tables that disagree about one route is worse than either
  table alone.

Coverage is deliberately *not* asserted to be complete here. It is not
complete yet, and a test that pretended otherwise — by asserting a subset, or
by comparing against a frozen list rather than the router — would turn a
partial registry into a green check. The completeness assertion lands with the
default-deny flip, together with the Router-vs-policy gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.youtab.route_inventory import collect  # noqa: E402
from youtab_agent_cli import authz  # noqa: E402
from youtab_agent_cli.route_authz_registry import (  # noqa: E402
    INDEX,
    KNOWN_METHODS,
    REGISTRY,
    RegistryError,
    RouteClass,
    RouteEntry,
    classify,
    coverage,
)


@pytest.fixture(scope="module")
def inventory():
    from youtab_agent_cli.web_server import app

    return collect(app)


# --- the registry's own invariants ------------------------------------------


class TestRegistryIsWellFormed:
    def test_it_has_entries(self):
        """An empty registry would satisfy every other check in this file."""
        assert len(REGISTRY) > 50

    def test_every_entry_is_indexed_exactly_once(self):
        """Built at import; a duplicate raises there, so this pins the count."""
        assert len(INDEX) == len(REGISTRY)

    def test_every_method_is_known(self):
        assert {e.method for e in REGISTRY} <= KNOWN_METHODS

    def test_head_and_options_are_never_registered(self):
        """Starlette synthesises them; an entry for one is permanently stale."""
        assert not {e for e in REGISTRY if e.method in {"HEAD", "OPTIONS"}}

    def test_public_and_loopback_entries_carry_a_justification(self):
        needs_reason = [
            e for e in REGISTRY
            if e.route_class in {RouteClass.PUBLIC, RouteClass.LOOPBACK_HEALTH}
        ]
        assert needs_reason, "the public surface is not empty; it must be justified"
        for entry in needs_reason:
            assert entry.justification.strip(), f"{entry.method} {entry.path}"
            assert len(entry.justification) > 40, (
                f"{entry.method} {entry.path}: a justification has to state the "
                "reason, not restate the route"
            )


class TestMalformedEntriesAreRefused:
    """The invariants above are enforced, not merely described.

    Each of these is the registry's own mutation proof: the construction that
    would defeat the control raises instead of being accepted.
    """

    def test_an_unknown_method_is_refused(self):
        with pytest.raises(RegistryError, match="unknown method"):
            RouteEntry("/api/x", "TRACE", RouteClass.AUTHENTICATED_USER)

    def test_a_relative_path_is_refused(self):
        with pytest.raises(RegistryError, match="absolute"):
            RouteEntry("api/x", "GET", RouteClass.AUTHENTICATED_USER)

    def test_an_unjustified_public_entry_is_refused(self):
        with pytest.raises(RegistryError, match="justification"):
            RouteEntry("/api/x", "GET", RouteClass.PUBLIC)

    def test_an_unjustified_loopback_entry_is_refused(self):
        with pytest.raises(RegistryError, match="justification"):
            RouteEntry("/api/x", "GET", RouteClass.LOOPBACK_HEALTH)

    def test_a_duplicate_decision_is_refused(self):
        from youtab_agent_cli.route_authz_registry import _build_index

        with pytest.raises(RegistryError, match="duplicate"):
            _build_index([
                RouteEntry("/api/x", "GET", RouteClass.AUTHENTICATED_USER),
                RouteEntry("/api/x", "GET", RouteClass.OWNER_SUPERADMIN),
            ])


# --- agreement with the real router -----------------------------------------


class TestRegistryMatchesTheRouter:
    def test_no_entry_is_stale(self, inventory):
        """Every classified route still exists on the router.

        Staleness is the quiet failure: an entry for a deleted route keeps
        counting toward coverage while protecting nothing.
        """
        report = coverage(inventory["http"], inventory["websocket"],
                          inventory["mounts"])
        assert report["stale"] == []

    def test_classified_routes_are_a_real_subset_of_the_router(self, inventory):
        report = coverage(inventory["http"], inventory["websocket"],
                          inventory["mounts"])
        assert len(report["classified"]) == len(INDEX)

    def test_the_surface_is_not_yet_fully_classified(self, inventory):
        """States the truth this branch is at, so nobody reads green as done.

        This assertion is expected to be inverted — to "nothing unclassified"
        — in the same commit that flips ``authorize()`` to default-deny. Until
        then it exists so a passing suite cannot be mistaken for a finished
        slice.
        """
        report = coverage(inventory["http"], inventory["websocket"],
                          inventory["mounts"])
        assert report["unclassified"], (
            "coverage is complete — flip authorize() to default-deny and "
            "replace this assertion with its inverse"
        )


# --- agreement with the scope table -----------------------------------------


class TestRegistryAgreesWithAuthz:
    def test_scoped_entries_match_required_scope(self):
        """Where both tables cover a route, they must say the same thing."""
        for entry in REGISTRY:
            if entry.method in {"WEBSOCKET", "MOUNT"} or entry.scope is None:
                continue
            from_table = authz.required_scope(entry.path, entry.method)
            if from_table is None:
                continue
            assert from_table == entry.scope, (
                f"{entry.method} {entry.path}: registry says {entry.scope}, "
                f"ROUTE_SCOPES says {from_table}"
            )

    def test_every_currently_scoped_route_is_classified(self, inventory):
        """No route that ``ROUTE_SCOPES`` already guards may be left unmapped.

        The privileged surface is the part that was already reasoned about;
        losing one of those entries while adding others would be a regression
        the coverage number alone would hide.
        """
        for pair in inventory["http"]:
            path, method = tuple(pair)
            if authz.required_scope(path, method) is None:
                continue
            assert classify(path, method) is not None, f"{method} {path}"


# --- the findings this classification produced ------------------------------


class TestPrivilegeLeaksFoundByReadingBodies:
    """Two routes whose authority their path prefix actively misrepresents.

    Both sit under ``/api/plugins/kanban``, a cluster that is otherwise the
    caller's own board data, and both were reachable by anyone signed in.
    Classifying by prefix, route name or neighbouring routes would have marked
    them user-owned and shipped the disclosure with a test asserting it.
    """

    def test_kanban_model_options_is_owner_only(self):
        entry = classify("/api/plugins/kanban/model-options", "GET")
        assert entry is not None
        assert entry.route_class is RouteClass.OWNER_SUPERADMIN
        assert entry.scope == authz.PROVIDER_READ

    def test_kanban_profile_roster_is_owner_only(self):
        entry = classify("/api/plugins/kanban/profiles", "GET")
        assert entry is not None
        assert entry.route_class is RouteClass.OWNER_SUPERADMIN
        assert entry.scope == authz.PROVIDER_READ

    def test_the_profile_description_routes_stay_user_owned(self):
        """The fix restricts the roster read, not the capability beside it."""
        for path, method in (
            ("/api/plugins/kanban/profiles/{}", "PATCH"),
            ("/api/plugins/kanban/profiles/{}/describe-auto", "POST"),
        ):
            entry = classify(path, method)
            assert entry is not None
            assert entry.route_class is RouteClass.USER_OWNED_RESOURCE


class TestCapabilityIsPreserved:
    """Security here restricts identity, secrets and infrastructure.

    It must not restrict what the Agent and its user may do inside their own
    workspace, so the routes that carry that capability are pinned as
    user-owned rather than quietly promoted to an administrative class.
    """

    @pytest.mark.parametrize("path,method", [
        ("/api/plugins/kanban/tasks", "POST"),
        ("/api/plugins/kanban/tasks/{}/decompose", "POST"),
        ("/api/plugins/kanban/tasks/{}/specify", "POST"),
        ("/api/plugins/kanban/dispatch", "POST"),
        ("/api/plugins/kanban/runs/{}/terminate", "POST"),
        ("/api/plugins/kanban/tasks/{}/log", "GET"),
        ("/api/plugins/kanban/tasks/{}/attachments", "POST"),
        ("/api/plugins/kanban/events", "WEBSOCKET"),
    ])
    def test_agent_and_user_capability_stays_user_owned(self, path, method):
        entry = classify(path, method)
        assert entry is not None, f"{method} {path} lost its classification"
        assert entry.route_class is RouteClass.USER_OWNED_RESOURCE


# --- the public surface -----------------------------------------------------


class TestPublicSurfaceIsExactlyTheAllowlist:
    def test_registry_public_set_matches_public_paths(self):
        """The registry may not invent a public route the middlewares don't have.

        ``PUBLIC_API_PATHS`` is what the two auth middlewares actually consult.
        A route classified public here but absent there would be a document
        that disagrees with the code; the reverse would be an unreviewed
        unauthenticated route.
        """
        from youtab_agent_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

        registry_open = {
            e.path for e in REGISTRY
            if e.route_class in {RouteClass.PUBLIC, RouteClass.LOOPBACK_HEALTH,
                                 RouteClass.INTERNAL_SERVICE}
        }
        assert registry_open == set(PUBLIC_API_PATHS)

    def test_cron_fire_is_internal_service_not_public(self):
        """It bypasses the cookie gate but is not unauthenticated.

        Its own NAS-minted JWT is the boundary. Classifying it ``public``
        would record the bypass and lose the credential that makes it safe.
        """
        entry = classify("/api/cron/fire", "POST")
        assert entry is not None
        assert entry.route_class is RouteClass.INTERNAL_SERVICE
