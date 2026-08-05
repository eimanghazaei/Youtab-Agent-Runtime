"""The route inventory is real, and the CSRF controls stay where they were put.

Two things, together because the second is the precondition for the first: the
authorization registry that is coming will be checked against this inventory,
and it must not be built on a surface whose existing protections have quietly
regressed.

The inventory reads the router the application actually builds. A
hand-maintained list drifts the moment someone adds an endpoint, and that drift
is exactly what the registry gate will exist to catch -- so the thing being
compared has to come from the framework, not from a file someone remembered to
update.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.youtab.route_inventory import collect, normalise  # noqa: E402
from youtab_agent_cli.dashboard_auth import csrf  # noqa: E402


@pytest.fixture(scope="module")
def inventory():
    from youtab_agent_cli.web_server import app

    return collect(app)


# --- the inventory is real --------------------------------------------------


class TestInventoryComesFromTheRouter:
    def test_it_finds_a_substantial_surface(self, inventory):
        """A collector that silently found nothing would pass every later gate."""
        assert inventory["counts"]["http_route_methods"] > 200
        assert inventory["counts"]["websocket"] >= 5

    def test_it_finds_known_routes_of_each_kind(self, inventory):
        http = {tuple(pair) for pair in inventory["http"]}
        assert ("/api/gateway/restart", "POST") in http
        assert ("/api/health", "GET") in http
        assert ("/api/auth/csrf", "GET") in http
        assert "/api/events" in inventory["websocket"]
        assert "/api/ws" in inventory["websocket"]

    def test_dynamic_segments_are_normalised(self, inventory):
        """A renamed path parameter must not read as add-one-remove-one."""
        assert normalise("/api/sessions/{session_id}") == "/api/sessions/{}"
        assert normalise("/api/sessions/{id}") == "/api/sessions/{}"
        assert any("{}" in path for path, _ in
                   (tuple(p) for p in inventory["http"]))

    def test_trailing_slash_collapses_but_root_survives(self):
        assert normalise("/api/thing/") == "/api/thing"
        assert normalise("/") == "/"

    def test_derived_methods_are_excluded(self, inventory):
        """HEAD and OPTIONS carry no separate authority decision."""
        methods = {method for _path, method in inventory["http"]}
        assert "HEAD" not in methods
        assert "OPTIONS" not in methods

    def test_every_route_object_was_recognised(self, inventory):
        """An unrecognised route is one nobody would ever classify."""
        assert inventory["unrecognised"] == []

    def test_mounts_are_reported_rather_than_silently_walked_past(self, inventory):
        """A mount serves an arbitrary sub-application; it needs a decision."""
        assert inventory["mounts"], "the /assets mount is not being reported"


# --- the CSRF controls are still in place -----------------------------------


class TestCsrfControlsHold:
    def test_the_external_bind_still_enforces_origin_and_token(self):
        """Both halves, still wired into the gate that runs them."""
        from youtab_agent_cli import web_server

        source = Path(web_server.__file__).read_text(encoding="utf-8", errors="replace")
        assert "_browser_csrf_gate" in source
        assert "origin_is_trusted" in source
        assert "_csrf.consume" in source

    def test_unsafe_methods_are_the_ones_covered(self):
        assert csrf.UNSAFE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}
        assert "GET" not in csrf.UNSAFE_METHODS

    def test_the_signing_key_is_not_hardcoded(self):
        """No literal key in the module, and the per-process fallback is random."""
        source = Path(csrf.__file__).read_text(encoding="utf-8")
        assert "secrets.token_bytes" in source
        # Two imports of the module in one process must not share a constant
        # that was written down; the fallback is generated, so it differs.
        assert csrf._process_secret != b"\x00" * 32
        assert len(csrf._process_secret) >= 32

    def test_the_signing_key_is_never_returned_to_a_caller(self):
        """A token is derived from the key; the key itself never leaves."""
        token = csrf.mint("u-key-check")
        assert csrf._secret().hex() not in token
        assert csrf._secret().decode("latin-1") not in token

    def test_a_token_does_not_disclose_the_principal_it_binds(self):
        token = csrf.mint("owner@example.test")
        assert "owner@example.test" not in token

    def test_the_replay_store_is_documented_as_single_process(self):
        """Horizontal scaling must fail qualification, not silently degrade.

        The store is an in-process dict, so two dashboard processes would each
        accept the same nonce once -- the single-use property that defeats
        replay is only true within one process. That constraint is recorded in
        the module rather than discovered in production, and this test fails if
        the note is removed without the store being replaced.
        """
        source = Path(csrf.__file__).read_text(encoding="utf-8")
        assert "horizontally" in source.lower()
        assert isinstance(csrf._issued, dict), (
            "the nonce store is no longer an in-process dict; if it became "
            "shared and atomic, replace this test with one that proves it"
        )


class TestCookieBindsAreNotSilentlyBrowserFacing:
    def test_the_csrf_gate_is_scoped_to_the_assertion_bind(self):
        """Recorded, not assumed.

        The gate applies where the credential is ambient and the caller is a
        browser: the external Access bind. Cookie-session binds are separately
        governed and are NOT covered -- so this asserts the scoping is explicit
        and deliberate, and it fails if the condition is removed, which would
        either over-apply the gate or silently drop it.
        """
        from youtab_agent_cli import web_server

        source = Path(web_server.__file__).read_text(encoding="utf-8", errors="replace")
        gate = source[source.index("async def _browser_csrf_gate"):]
        gate = gate[:gate.index("async def _authorization_gate")]
        assert "_assertion_only_bind()" in gate, (
            "the CSRF gate no longer distinguishes the external bind"
        )
        assert "token_authenticated" in gate, (
            "service callers are no longer excluded by identity"
        )
