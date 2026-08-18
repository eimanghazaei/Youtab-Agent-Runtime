"""The ``/api/plugins`` body-audit: its two corrections are enforced and load-bearing.

The plugins cluster is otherwise the caller's own Kanban board and achievement
state — genuine ``plugin:use`` capability that must stay reachable. Reading every
handler body found exactly two routes whose authority their ``/api/plugins``
prefix misrepresents, and one write-side finding that the route table cannot
express:

* ``GET /api/plugins/kanban/model-options`` and ``GET /api/plugins/kanban/profiles``
  return the private provider/engine catalogue (provider slugs, raw model ids,
  and the ``model``/``provider`` bound to every installed profile). Both are held
  at ``provider:read`` via :data:`authz.EXACT_ROUTE_SCOPES`, exact beating the
  ``/api/plugins`` → ``plugin:use`` prefix.
* ``POST /tasks``, ``POST /tasks/bulk`` and ``PATCH /tasks/{}`` accept an optional
  ``model_override``/``provider_override`` — a raw engine selection — on an
  otherwise user-owned mutation. That is engine-selection authority the route
  table cannot carve out of a mixed body; it is recorded KNOWN OPEN in the
  registry and pinned here so it cannot silently regress or be silently blessed.

A correction that is enforced but unproven is one refactor away from being
advisory. The mutation tests below revert each provider:read correction back to
the blanket ``plugin:use`` — in memory, via ``monkeypatch.setattr`` on the real
policy module, the parallel-safe pattern ``test_router_policy_gate.py`` uses
(rewriting the file on disk races the gate's workers) — and prove the normal
user is re-admitted, which is exactly the regression the correction prevents.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from youtab_agent_cli import authz  # noqa: E402
from youtab_agent_cli.authz import (  # noqa: E402
    PLUGIN_USE,
    PROVIDER_READ,
    USER_CAPABILITIES,
    Principal,
    Role,
    authorize,
    required_scope,
    resolve_principal,
)
from youtab_agent_cli.route_authz_registry import (  # noqa: E402
    RouteClass,
    classify,
)

#: The two ``/api/plugins`` routes the body-audit tightened to ``provider:read``.
CORRECTED_ROUTES = (
    "/api/plugins/kanban/model-options",
    "/api/plugins/kanban/profiles",
)

#: The task-write routes carrying the KNOWN OPEN engine-override sub-field.
TASK_WRITE_ROUTES = (
    ("/api/plugins/kanban/tasks", "POST"),
    ("/api/plugins/kanban/tasks/bulk", "POST"),
    ("/api/plugins/kanban/tasks/{}", "PATCH"),
)


def _normal_user() -> Principal:
    """The ordinary signed-in baseline: a verified identity, no roster grant."""
    return resolve_principal(user_id="someone", org_id="acme", roster={})


def _provider_reader() -> Principal:
    """A principal holding exactly ``provider:read`` and nothing else.

    Constructed directly rather than through a role so the positive assertion
    turns on the single scope under test, not on everything a superadmin holds.
    """
    return Principal(user_id="op", org_id="acme", scopes=frozenset({PROVIDER_READ}))


# --- the correction is real, on both sides ----------------------------------


class TestModelOptionsCorrectionIsLoadBearing:
    """``GET /api/plugins/kanban/model-options`` returns the engine catalogue."""

    def test_it_requires_provider_read(self):
        assert required_scope(
            "/api/plugins/kanban/model-options", "GET") == PROVIDER_READ

    def test_a_provider_reader_is_authorized(self):
        assert authorize(
            _provider_reader(), "/api/plugins/kanban/model-options", "GET")

    def test_normal_user_is_refused_model_options(self):
        # Named target of the mutation harness: reverting the correction back
        # to the blanket ``plugin:use`` (which the baseline holds) flips this.
        assert not authorize(
            _normal_user(), "/api/plugins/kanban/model-options", "GET")

    def test_the_registry_classifies_it_owner_only(self):
        entry = classify("/api/plugins/kanban/model-options", "GET")
        assert entry is not None
        assert entry.route_class is RouteClass.OWNER_SUPERADMIN
        assert entry.scope == PROVIDER_READ


class TestProfileRosterCorrectionIsLoadBearing:
    """``GET /api/plugins/kanban/profiles`` returns each profile's engine binding."""

    def test_it_requires_provider_read(self):
        assert required_scope(
            "/api/plugins/kanban/profiles", "GET") == PROVIDER_READ

    def test_a_provider_reader_is_authorized(self):
        assert authorize(
            _provider_reader(), "/api/plugins/kanban/profiles", "GET")

    def test_normal_user_is_refused_profiles(self):
        # Named target of the mutation harness for the profiles correction.
        assert not authorize(
            _normal_user(), "/api/plugins/kanban/profiles", "GET")

    def test_the_registry_classifies_it_owner_only(self):
        entry = classify("/api/plugins/kanban/profiles", "GET")
        assert entry is not None
        assert entry.route_class is RouteClass.OWNER_SUPERADMIN
        assert entry.scope == PROVIDER_READ


# --- fail-closed: the baseline holds none of the privileged scope -----------


class TestTheBaselineDoesNotHoldProviderRead:
    def test_user_capabilities_exclude_provider_read(self):
        assert PROVIDER_READ not in USER_CAPABILITIES

    def test_the_normal_role_intersects_none_of_the_privileged_scope(self):
        baseline = authz.ROLE_SCOPES[Role.NORMAL_USER]
        assert baseline & {PROVIDER_READ} == frozenset()

    @pytest.mark.parametrize("path", CORRECTED_ROUTES)
    def test_a_normal_user_is_refused_each_corrected_route(self, path):
        assert not authorize(_normal_user(), path, "GET")


# --- the rest of the cluster stays reachable capability ---------------------


class TestUserCapabilityInTheClusterIsPreserved:
    """Tightening the two disclosures must not cost the product beside them."""

    @pytest.mark.parametrize("path,method", [
        ("/api/plugins/kanban/board", "GET"),
        ("/api/plugins/kanban/tasks", "POST"),
        ("/api/plugins/kanban/tasks/x", "PATCH"),
        ("/api/plugins/kanban/tasks/bulk", "POST"),
        ("/api/plugins/kanban/dispatch", "POST"),
        ("/api/plugins/kanban/config", "GET"),
        ("/api/plugins/kanban/orchestration", "PUT"),
        ("/api/plugins/kanban/profiles/x", "PATCH"),
        ("/api/plugins/youtab-achievements/achievements", "GET"),
    ])
    def test_a_normal_user_still_reaches_it(self, path, method):
        assert authorize(_normal_user(), path, method), f"{method} {path} was taken away"


# --- the write-side finding is recorded, not silently blessed ---------------


class TestTaskWriteEngineOverrideIsGuardedInHandler:
    """``model_override``/``provider_override`` on the task-write routes.

    Persisting a raw provider+model is engine:select authority, but it is an
    optional sub-field of a user-owned mutation, so the route table cannot carve
    it out. The route stays ``plugin:use`` at the boundary (task create/edit is
    a product capability) and the elevated sub-field is refused in the handler,
    exactly like ``PUT /api/config``. These tests pin that the route-table half
    of that decision holds; the handler half is proven in
    ``tests/plugins/test_kanban_engine_override_authz.py``.
    """

    @pytest.mark.parametrize("path,method", TASK_WRITE_ROUTES)
    def test_the_route_table_still_admits_the_baseline(self, path, method):
        probe = path.replace("{}", "x")
        assert authorize(_normal_user(), probe, method)

    @pytest.mark.parametrize("path,method", TASK_WRITE_ROUTES)
    def test_it_is_classified_user_owned_and_cites_the_handler_guard(self, path, method):
        entry = classify(path, method)
        assert entry is not None
        assert entry.route_class is RouteClass.USER_OWNED_RESOURCE
        assert "CLOSED in the handler" in entry.justification
        assert "engine:select" in entry.justification


# --- mutation: revert a correction in memory, prove it reopens the route ----
#
# In-memory ``monkeypatch``, never a file write. The required gate runs its
# workers in parallel (pytest-xdist), and a mutation written to ``authz.py`` on
# disk is global: a second worker freshly importing the module mid-mutation
# would read a half-written policy and fail an unrelated consistency check.
# ``required_scope``/``authorize`` look ``authz.EXACT_ROUTE_SCOPES`` up by name
# at call time, so swapping the attribute on the module object reverts the
# correction for this worker alone — the pattern ``test_router_policy_gate``
# already uses.


@pytest.mark.parametrize("route", CORRECTED_ROUTES)
def test_reverting_the_correction_reopens_the_route(route, monkeypatch):
    """The ``provider:read`` correction is load-bearing.

    With the exact override in place the route needs ``provider:read`` and the
    normal-user baseline is refused. Drop that override and the route falls back
    to the ``/api/plugins`` → ``plugin:use`` prefix, which the baseline holds —
    the disclosure reopens. Proven by reverting the policy on the module in
    memory, so parallel workers never see each other's mutation.
    """
    # The correction is in force.
    assert required_scope(route, "GET") == PROVIDER_READ
    assert authorize(_normal_user(), route, "GET") is False

    reverted = {
        path: scope
        for path, scope in authz.EXACT_ROUTE_SCOPES.items()
        if path != route
    }
    monkeypatch.setattr(authz, "EXACT_ROUTE_SCOPES", reverted)

    # Reverted to the blanket prefix: the baseline is admitted again.
    assert required_scope(route, "GET") == PLUGIN_USE
    assert authorize(_normal_user(), route, "GET") is True
