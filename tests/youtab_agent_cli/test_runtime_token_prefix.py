"""Unit tests for the token-seam prefix support (AR-PROD-01, Milestone 1).

The runtime surface registers a whole ``/api/runtime/v1/`` prefix as
token-authable because its parametric sub-paths can't be enumerated up front.
"""
from __future__ import annotations

from youtab_agent_cli.dashboard_auth import token_auth


def teardown_function():
    token_auth.clear_token_routes()


def test_prefix_matches_subpaths_but_is_segment_anchored():
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix("/api/runtime/v1", provider="runtime-service", capability="runtime")
    # Sub-paths (including parametric ones) match.
    assert token_auth.is_token_route("/api/runtime/v1/health")
    assert token_auth.is_token_route("/api/runtime/v1/runs/abc/events")
    # A sibling that merely shares the string prefix must NOT match.
    assert not token_auth.is_token_route("/api/runtime/v1x/runs")
    # An unrelated path is untouched.
    assert not token_auth.is_token_route("/api/status")


def test_exact_and_prefix_coexist():
    token_auth.clear_token_routes()
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret", capability="drain")
    token_auth.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service", capability="runtime")
    assert token_auth.is_token_route("/api/gateway/drain")
    assert token_auth.is_token_route("/api/runtime/v1/agents")


def test_clear_drops_prefixes():
    token_auth.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service", capability="runtime")
    assert token_auth.is_token_route("/api/runtime/v1/agents")
    token_auth.clear_token_routes()
    assert not token_auth.is_token_route("/api/runtime/v1/agents")
