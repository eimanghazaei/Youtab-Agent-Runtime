"""The Router-vs-policy gate fails closed on each of its nine conditions.

A gate that passes is worth nothing until each condition has been shown to
turn it red. So every check below breaks the thing it guards — by mutating the
policy tables in place, running the gate, and restoring — and asserts the gate
reported *that* condition, not merely that it failed.

The mutations run against the real router and the real policy module, not a
fixture. A gate proven against a mock is a gate proven against the mock.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.youtab.router_policy_gate import CONDITIONAL_ROUTES, run  # noqa: E402
from youtab_agent_cli import authz  # noqa: E402


def conditions(findings) -> set[str]:
    return {condition for condition, _ in findings}


@contextmanager
def patched(attr: str, value):
    """Swap a policy table for the duration of one gate run."""
    original = getattr(authz, attr)
    setattr(authz, attr, value)
    try:
        yield
    finally:
        setattr(authz, attr, original)


class TestTheGatePassesOnTheRealSurface:
    def test_router_and_policy_agree(self):
        findings = run()
        assert not findings, (
            "gate is failing on the shipped surface:\n" +
            "\n".join(f"  {c}: {d}" for c, d in findings)
        )


class TestEachConditionFailsClosed:
    """Nine mutations, nine conditions. Each names the condition it expects."""

    def test_missing_classification(self):
        """A router route no rule resolves."""
        stripped = tuple(
            (prefix, scope) for prefix, scope in authz.ROUTE_SCOPES
            if prefix != "/api/sessions"
        )
        with patched("ROUTE_SCOPES", stripped):
            assert "missing-classification" in conditions(run())

    def test_stale_entry(self):
        """A rule matching nothing on the router."""
        with patched("ROUTE_SCOPES",
                     authz.ROUTE_SCOPES + (("/api/deleted-cluster", authz.UI_READ),)):
            assert "stale-entry" in conditions(run())

    def test_duplicate_entry(self):
        """One prefix decided twice, conflictingly."""
        with patched("ROUTE_SCOPES",
                     authz.ROUTE_SCOPES + (("/api/sessions", authz.OPS_MANAGE),)):
            assert "duplicate-entry" in conditions(run())

    def test_unknown_method(self):
        """A rule naming a verb the router cannot serve."""
        with patched("WRITE_METHODS", authz.WRITE_METHODS | {"TRACE"}):
            assert "unknown-method" in conditions(run())

    def test_unknown_route_object(self, monkeypatch):
        """The collector could not classify a route object."""
        import scripts.youtab.router_policy_gate as gate

        real = gate._load

        def unrecognised():
            app, inventory = real()
            inventory = dict(inventory)
            inventory["unrecognised"] = ["<SomeWrapper object at 0x0>"]
            return app, inventory

        monkeypatch.setattr(gate, "_load", unrecognised)
        assert "unknown-route-object" in conditions(gate.run())

    def test_ambiguous_parameter(self, monkeypatch):
        """Two declared routes collapsing onto one normalised key."""
        import scripts.youtab.router_policy_gate as gate

        real = gate._load

        def collided():
            app, inventory = real()
            inventory = dict(inventory)
            inventory["http"] = list(inventory["http"]) + [["/api/sessions/{}", "GET"],
                                                           ["/api/sessions/{}", "GET"]]
            return app, inventory

        monkeypatch.setattr(gate, "_load", collided)
        assert "ambiguous-parameter" in conditions(gate.run())

    def test_missing_websocket(self, monkeypatch):
        """A socket that does not enforce a scope before accepting."""
        import scripts.youtab.router_policy_gate as gate

        real = gate._load

        def extra_socket():
            app, inventory = real()
            inventory = dict(inventory)
            inventory["websocket"] = list(inventory["websocket"]) + ["/api/unwatched"]
            return app, inventory

        monkeypatch.setattr(gate, "_load", extra_socket)
        assert "missing-websocket" in conditions(gate.run())

    def test_missing_mount(self, monkeypatch):
        """A mount with no rule covering it."""
        import scripts.youtab.router_policy_gate as gate

        real = gate._load

        def extra_mount():
            app, inventory = real()
            inventory = dict(inventory)
            inventory["mounts"] = list(inventory["mounts"]) + ["/api/unmapped-mount"]
            return app, inventory

        monkeypatch.setattr(gate, "_load", extra_mount)
        assert "missing-mount" in conditions(gate.run())

    def test_unjustified_public(self):
        """A public route with no written reason in the registry."""
        widened = dict(authz.EXACT_ROUTE_SCOPES)
        widened["/api/sessions"] = authz.PUBLIC
        with patched("EXACT_ROUTE_SCOPES", widened):
            assert "unjustified-public" in conditions(run())


class TestTheGateIsRestoredAfterEveryMutation:
    """Ordering guard: a mutation that leaked would make later runs meaningless."""

    def test_still_green(self):
        assert not run()


class TestInconclusiveIsFailure:
    def test_a_router_that_will_not_build_fails_the_gate(self, monkeypatch):
        """Never a pass. A gate that cannot see the surface has not checked it."""
        import scripts.youtab.router_policy_gate as gate

        def broken():
            raise RuntimeError("router import blew up")

        monkeypatch.setattr(gate, "_load", broken)
        assert "load-failure" in conditions(gate.run())


class TestBuildStateIsToleratedNotIgnored:
    def test_the_conditional_routes_are_named_not_wildcarded(self):
        """An open-ended exemption would hide a genuinely missing rule."""
        assert CONDITIONAL_ROUTES == frozenset({
            ("/assets/{}.css", "GET"),
            ("/assets", "MOUNT"),
        })

    def test_the_gate_passes_in_the_unbuilt_state(self):
        """This suite runs without a built frontend, which is the point."""
        assert not run()
