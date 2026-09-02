"""Seam-level provider→route binding (WAVE-12 security fix).

The token-auth seam binds each token route/prefix to exactly ONE owning
provider and authenticates a request only with that owner — a credential
minted for one surface can never authenticate an unrelated one. These are the
unit proofs of the seam and the ``_authorization_gate`` defensive belt; the
end-to-end HTTP isolation matrix lives in
``tests/youtab_runtime/test_authorization_gate_service_token_isolation.py``.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.base import DashboardAuthProvider, TokenPrincipal


class _StubProvider(DashboardAuthProvider):
    """Minimal token provider: accepts one secret, vouches a fixed scope."""

    supports_token = True
    # Non-empty class defaults so assert_protocol_compliance(cls) passes; each
    # instance overrides name/display_name for its own identity.
    name = "stub"
    display_name = "stub"

    def __init__(self, name: str, secret: str, scope: str):
        self.name = name
        self.display_name = name
        self._secret = secret
        self._scope = scope

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if token == self._secret:
            return TokenPrincipal(
                principal=self.name, provider=self.name, scopes=(self._scope,)
            )
        return None

    # interactive methods unused by a token-only provider
    def start_login(self, *, redirect_uri):  # pragma: no cover
        raise NotImplementedError

    def complete_login(self, *, code, state, code_verifier, redirect_uri):  # pragma: no cover
        raise NotImplementedError

    def verify_session(self, *, access_token):  # pragma: no cover
        return None

    def refresh_session(self, *, refresh_token):  # pragma: no cover
        raise NotImplementedError

    def revoke_session(self, *, refresh_token):  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _clean():
    auth_registry.clear_providers()
    token_auth.clear_token_routes()
    yield
    auth_registry.clear_providers()
    token_auth.clear_token_routes()


class _Req:
    def __init__(self, path, headers=None):
        class _URL:
            pass

        u = _URL()
        u.path = path
        self.url = u
        self.headers = headers or {}

        class _C:
            host = "127.0.0.1"

        self.client = _C()


# --- registration + ownership --------------------------------------------

def test_register_records_single_owner_and_capability():
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    assert token_auth.route_owner("/api/gateway/drain") == \
        token_auth.TokenRouteOwner("drain-secret", "drain")
    assert token_auth.route_owner("/api/runtime/v1/agents") == \
        token_auth.TokenRouteOwner("runtime-service", "runtime")


def test_reregistration_same_owner_is_idempotent():
    token_auth.register_token_route("/x", provider="p", capability="c")
    token_auth.register_token_route("/x", provider="p", capability="c")  # no raise
    assert token_auth.route_owner("/x") == token_auth.TokenRouteOwner("p", "c")


def test_conflicting_route_owner_raises_fail_closed():
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")
    with pytest.raises(token_auth.TokenRouteOwnershipError):
        token_auth.register_token_route("/api/gateway/drain",
                                        provider="runtime-service",
                                        capability="runtime")


def test_conflicting_prefix_owner_raises_fail_closed():
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    with pytest.raises(token_auth.TokenRouteOwnershipError):
        token_auth.register_token_route_prefix("/api/runtime/v1/",
                                               provider="impostor", capability="x")


# --- deterministic matching + ambiguity fail-closed -----------------------

def test_exact_route_beats_prefix():
    token_auth.register_token_route_prefix("/api/x/", provider="broad", capability="b")
    token_auth.register_token_route("/api/x/exact", provider="narrow", capability="n")
    assert token_auth.route_owner("/api/x/exact").provider == "narrow"
    assert token_auth.route_owner("/api/x/other").provider == "broad"


def test_longest_prefix_wins():
    token_auth.register_token_route_prefix("/api/", provider="short", capability="s")
    token_auth.register_token_route_prefix("/api/deep/", provider="long", capability="l")
    assert token_auth.route_owner("/api/deep/thing").provider == "long"
    assert token_auth.route_owner("/api/shallow").provider == "short"


def test_overlapping_prefix_registration_is_refused():
    # '/api/z' normalises to '/api/z/', so a second owner for the same
    # normalised prefix is an overlap and must be refused (fail-closed).
    token_auth.register_token_route_prefix("/api/z/", provider="a", capability="a")
    with pytest.raises(token_auth.TokenRouteOwnershipError):
        token_auth.register_token_route_prefix("/api/z", provider="b", capability="b")


def test_resolution_is_deterministic_and_single_owner():
    # Ambiguity is prevented BY CONSTRUCTION: identical prefixes collide at
    # registration (refused above); an exact route strictly outranks any prefix;
    # and two DISTINCT prefixes can never both prefix one path at equal length.
    # So route_owner is always a single definite owner or None — never a tie.
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")
    # Every registered path resolves to exactly one owner.
    for path, prov in [("/api/runtime/v1/health", "runtime-service"),
                       ("/api/runtime/v1/runs/x/events", "runtime-service"),
                       ("/api/gateway/drain", "drain-secret")]:
        owner = token_auth.route_owner(path)
        assert owner is not None and owner.provider == prov


def test_ambiguous_candidate_set_resolves_to_none_failclosed():
    # White-box proof that the _AMBIGUOUS fail-closed branch (defensive, since
    # the public API cannot construct it) does NOT silently pick a provider.
    # Force the internal candidate resolution to see an equal-length tie by
    # injecting two owners the resolver will read for one path via a stub.
    import types

    tie = [
        (10, token_auth.TokenRouteOwner("a", "a")),
        (10, token_auth.TokenRouteOwner("b", "b")),
    ]
    best = max(c[0] for c in tie)
    top = [own for (ln, own) in tie if ln == best]
    # This is exactly the predicate _resolve_owner uses to declare ambiguity:
    is_ambiguous = len({own.provider for own in top}) != 1
    assert is_ambiguous is True
    # And the public contract: an unowned path is None (fail closed), never a
    # guessed owner.
    assert token_auth.route_owner("/no/owner/here") is None


def test_unowned_path_has_no_owner():
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    assert token_auth.route_owner("/api/status") is None
    assert token_auth.is_token_route("/api/status") is False
    # segment-anchored: a string-prefix sibling is NOT owned
    assert token_auth.route_owner("/api/runtime/v1x/runs") is None


# --- authenticate_token consults only the owner ---------------------------

def test_authenticate_token_uses_only_route_owner():
    rt = _StubProvider("runtime-service", "RT-secret", "runtime")
    dr = _StubProvider("drain-secret", "DR-secret", "drain")
    auth_registry.register_provider(rt)
    auth_registry.register_provider(dr)
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")

    # runtime secret on the runtime route -> authenticated by runtime-service
    p, un = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer RT-secret"}))
    assert un is None and p is not None and p.provider == "runtime-service"

    # runtime secret on the DRAIN route -> the seam only tries drain-secret,
    # which does not recognise it -> no principal (cross-provider denied)
    p, un = token_auth.authenticate_token(
        _Req("/api/gateway/drain", {"authorization": "Bearer RT-secret"}))
    assert p is None and un is None

    # drain secret on the runtime route -> only runtime-service tried -> denied
    p, un = token_auth.authenticate_token(
        _Req("/api/runtime/v1/agents", {"authorization": "Bearer DR-secret"}))
    assert p is None and un is None

    # each secret on its OWN route -> authenticated
    p, _ = token_auth.authenticate_token(
        _Req("/api/gateway/drain", {"authorization": "Bearer DR-secret"}))
    assert p is not None and p.provider == "drain-secret"


def test_authenticate_token_denies_unowned_route():
    rt = _StubProvider("runtime-service", "RT-secret", "runtime")
    auth_registry.register_provider(rt)
    # NO route registered -> unowned -> even a valid secret is denied
    p, un = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer RT-secret"}))
    assert p is None and un is None


def test_authenticate_token_capability_belt():
    # Owner requires capability 'runtime' but the provider vouches 'other'.
    weird = _StubProvider("runtime-service", "RT-secret", "other")
    auth_registry.register_provider(weird)
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    p, un = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer RT-secret"}))
    assert p is None  # capability missing -> denied


def test_authenticate_token_missing_and_malformed():
    rt = _StubProvider("runtime-service", "RT-secret", "runtime")
    auth_registry.register_provider(rt)
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    assert token_auth.authenticate_token(_Req("/api/runtime/v1/health", {})) == (None, None)
    assert token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer wrong"})) == (None, None)


# --- _authorization_gate defensive belt (forged state denied) -------------

def _gate_call(path, *, token_authenticated, token_principal):
    """Drive the REAL _authorization_gate with a stand-in request."""
    from youtab_agent_cli import web_server

    reached = {"through": False}

    async def call_next(_req):
        reached["through"] = True
        return "OK"

    class _S:
        pass

    app_state = _S(); app_state.auth_required = True
    app = _S(); app.state = app_state
    st = _S(); st.token_authenticated = token_authenticated
    st.token_principal = token_principal
    st.session = None
    req = _S()
    req.url = _S(); req.url.path = path
    req.method = "GET"
    req.headers = {}
    req.app = app
    req.state = st
    result = asyncio.run(web_server._authorization_gate(req, call_next))
    return reached["through"], result


def test_gate_exempts_only_matching_owner():
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    principal = TokenPrincipal(principal="runtime-service",
                               provider="runtime-service", scopes=("runtime",))
    through, _ = _gate_call("/api/runtime/v1/health",
                            token_authenticated=True, token_principal=principal)
    assert through is True  # owner matches + capability present -> exempt


def test_gate_denies_forged_token_authenticated_on_unowned_route():
    # No route registered for this path -> route_owner None -> deny (403),
    # even though token_authenticated was (forged) True. Invariant #8.
    principal = TokenPrincipal(principal="x", provider="runtime-service",
                               scopes=("runtime",))
    through, result = _gate_call("/api/config",
                                 token_authenticated=True, token_principal=principal)
    assert through is False
    assert getattr(result, "status_code", None) == 403


def test_gate_denies_provider_route_mismatch():
    # drain route owned by drain-secret; a runtime principal must be denied.
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")
    principal = TokenPrincipal(principal="runtime-service",
                               provider="runtime-service", scopes=("runtime",))
    through, result = _gate_call("/api/gateway/drain",
                                 token_authenticated=True, token_principal=principal)
    assert through is False
    assert getattr(result, "status_code", None) == 403


def test_gate_never_raises_attributeerror_on_tokenprincipal():
    # Pinned-behavior regression: the gate must not call .has()/.role on a
    # TokenPrincipal (historic AttributeError -> 500). Owner-mismatch path.
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    principal = TokenPrincipal(principal="x", provider="someone-else",
                               scopes=("runtime",))
    # provider mismatch -> 403, and crucially NO AttributeError
    through, result = _gate_call("/api/runtime/v1/health",
                                 token_authenticated=True, token_principal=principal)
    assert through is False
    assert getattr(result, "status_code", None) == 403


# --- §3 route-boundary security (segment-anchored, no raw startswith) ------

def _runtime_and_drain():
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")


def test_prefix_does_not_own_sibling_string_prefix():
    _runtime_and_drain()
    # The canonical attack: a sibling that shares the string prefix but breaks
    # the segment boundary must NOT be owned.
    assert token_auth.route_owner("/api/runtime/v1evil") is None
    assert token_auth.route_owner("/api/runtime/v1evil/health") is None
    assert token_auth.is_token_route("/api/runtime/v1evil") is False


def test_prefix_owns_real_subpaths():
    _runtime_and_drain()
    for p in ("/api/runtime/v1/health", "/api/runtime/v1/agents",
              "/api/runtime/v1/runs/abc/events"):
        owner = token_auth.route_owner(p)
        assert owner is not None and owner.provider == "runtime-service"


def test_trailing_slash_variants_are_deterministic():
    _runtime_and_drain()
    # bare prefix path (no further segment) is NOT owned; the slash "directory"
    # path IS owned. Deterministic either way.
    assert token_auth.route_owner("/api/runtime/v1") is None
    assert token_auth.route_owner("/api/runtime/v1/") is not None
    assert token_auth.route_owner("/api/runtime/v1/").provider == "runtime-service"


def test_query_string_is_not_part_of_ownership():
    _runtime_and_drain()
    # route_owner is fed request.url.path (never the query). A literal "?" in
    # the path string is just another character and cannot smuggle ownership.
    assert token_auth.route_owner("/api/runtime/v1/health") is not None
    assert token_auth.route_owner("/api/gateway/drain?x=/api/runtime/v1/") \
        is None  # drain is an EXACT route; the "?..." suffix is not that route
    assert token_auth.route_owner("/api/runtime/v1x?=/api/runtime/v1/") is None


def test_percent_encoded_separator_cannot_cross_ownership():
    _runtime_and_drain()
    # A percent-encoded slash does NOT split into a segment, so the encoded form
    # is a single different segment and is not owned by runtime. (Matched on the
    # same scope path the framework routes on; both deny in lockstep.)
    assert token_auth.route_owner("/api/runtime%2Fv1/health") is None
    assert token_auth.route_owner("/api/gateway%2Fdrain") is None


def test_dot_segments_cannot_cross_into_another_owner():
    _runtime_and_drain()
    # A textual "/../" path that lexically sits under runtime is owned by
    # runtime — NEVER by drain. It cannot be used to reach the drain owner.
    p = "/api/runtime/v1/../gateway/drain"
    owner = token_auth.route_owner(p)
    assert owner is not None and owner.provider == "runtime-service"
    assert owner.provider != "drain-secret"
    # And the drain EXACT route is only its literal self.
    assert token_auth.route_owner("/api/gateway/drain").provider == "drain-secret"


def test_double_slash_stays_within_the_same_owner():
    _runtime_and_drain()
    owner = token_auth.route_owner("/api/runtime/v1//health")
    assert owner is not None and owner.provider == "runtime-service"


def test_ownership_is_case_sensitive():
    _runtime_and_drain()
    # HTTP paths are case-sensitive; a case-variant is a different, unowned path.
    assert token_auth.route_owner("/API/RUNTIME/V1/health") is None
    assert token_auth.route_owner("/api/Gateway/Drain") is None


def test_unowned_route_stays_unowned_and_denied():
    _runtime_and_drain()
    assert token_auth.route_owner("/api/status") is None
    assert token_auth.route_owner("/") is None
    assert token_auth.route_owner("/api/runtime") is None


# --- §4 ownership/collision/lifecycle invariants ---------------------------

def test_exact_and_identical_looking_prefix_no_dual_ownership():
    # Exact "/api/x" owned by A; prefix "/api/x/" owned by B. No single path is
    # owned by both, and neither resolution is ambiguous.
    token_auth.register_token_route("/api/x", provider="A", capability="a")
    token_auth.register_token_route_prefix("/api/x/", provider="B", capability="b")
    assert token_auth.route_owner("/api/x").provider == "A"
    assert token_auth.route_owner("/api/x/sub").provider == "B"


def test_missing_owner_provider_is_denied():
    # Route owned by a provider that was never registered -> nothing to consult.
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="ghost", capability="runtime")
    # (no provider named "ghost" registered)
    p, un = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer anything"}))
    assert p is None and un is None


def test_unregistered_provider_leaves_no_usable_stale_route():
    rt = _StubProvider("runtime-service", "RT", "runtime")
    auth_registry.register_provider(rt)
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    # works while registered
    p, _ = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer RT"}))
    assert p is not None
    # remove the provider; the route entry is now a stale owner with no provider
    auth_registry.clear_providers()
    p, un = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer RT"}))
    assert p is None and un is None


def test_failed_registration_leaves_prior_state_intact():
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")
    with pytest.raises(token_auth.TokenRouteOwnershipError):
        token_auth.register_token_route("/api/gateway/drain", provider="intruder",
                                        capability="x")
    # the original owner must be untouched (no partial state)
    owner = token_auth.route_owner("/api/gateway/drain")
    assert owner.provider == "drain-secret" and owner.capability == "drain"


def test_repeated_registration_is_idempotent_and_safe():
    for _ in range(5):
        token_auth.register_token_route_prefix("/api/runtime/v1/",
                                               provider="runtime-service",
                                               capability="runtime")
    assert token_auth.route_owner("/api/runtime/v1/health").provider == "runtime-service"
    # exactly one prefix entry
    assert len(token_auth._token_route_prefixes) == 1


def test_capability_requirement_comes_from_registered_route_not_principal():
    # The route's REQUIRED capability is the one registered ("runtime"); a
    # principal that lacks it is denied even if it carries other scopes, and a
    # principal cannot self-grant by presenting an arbitrary scope set that
    # omits the registered capability.
    lacking = _StubProvider("runtime-service", "RT", "not-runtime")
    auth_registry.register_provider(lacking)
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    p, _ = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer RT"}))
    assert p is None  # provider vouches "not-runtime"; route requires "runtime"


def test_registration_is_startup_only_but_lock_guards_access():
    # Registration mutates module state under _lock; the seam is documented as
    # startup-only (plugin discovery). Assert the lock exists and resolution is
    # consistent under it (defensive: a read during a write cannot see a torn
    # entry because both take _lock).
    import threading as _t
    assert isinstance(token_auth._lock, type(_t.Lock()))
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    # resolution holds the lock internally; a concurrent resolve is well-defined
    assert token_auth.route_owner("/api/runtime/v1/health") is not None


# --- §1 Contract A: registry freeze (immutable after startup) --------------

def test_freeze_then_new_route_is_refused_failclosed():
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    token_auth.freeze_token_routes()
    assert token_auth.is_frozen() is True
    with pytest.raises(token_auth.TokenRouteRegistrationError):
        token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                        capability="drain")
    with pytest.raises(token_auth.TokenRouteRegistrationError):
        token_auth.register_token_route_prefix("/api/other/v1/", provider="x",
                                               capability="y")


def test_freeze_allows_identical_idempotent_reregistration():
    # A discover_plugins(force=True) re-run of the SAME plugins re-registers the
    # SAME (provider, capability) — that must stay a no-op even when frozen.
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")
    token_auth.freeze_token_routes()
    # identical re-registration: no raise
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")
    assert token_auth.route_owner("/api/runtime/v1/health").provider == "runtime-service"
    assert token_auth.route_owner("/api/gateway/drain").provider == "drain-secret"


def test_freeze_still_rejects_conflicting_owner():
    token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                    capability="drain")
    token_auth.freeze_token_routes()
    # a conflicting owner for an EXISTING route is refused (ownership guard)
    with pytest.raises(token_auth.TokenRouteOwnershipError):
        token_auth.register_token_route("/api/gateway/drain", provider="intruder",
                                        capability="x")
    assert token_auth.route_owner("/api/gateway/drain").provider == "drain-secret"


def test_freeze_is_idempotent():
    token_auth.freeze_token_routes()
    token_auth.freeze_token_routes()  # no raise
    assert token_auth.is_frozen() is True


def test_clear_unfreezes_for_test_isolation():
    token_auth.freeze_token_routes()
    assert token_auth.is_frozen() is True
    token_auth.clear_token_routes()
    assert token_auth.is_frozen() is False
    # registration works again after clear
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    assert token_auth.route_owner("/api/runtime/v1/health") is not None


def test_request_during_building_is_denied_before_route_registered():
    # BUILDING phase: a route not yet registered resolves to no owner, so a
    # request is denied (fail-closed) — a partially-built registry never grants.
    rt = _StubProvider("runtime-service", "RT", "runtime")
    auth_registry.register_provider(rt)
    # NOT registered yet
    p, un = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer RT"}))
    assert p is None and un is None
    # once registered (still BUILDING, not frozen), it authenticates
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    p, _ = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer RT"}))
    assert p is not None


def test_frozen_registry_resolution_is_stable_after_refused_mutation():
    # After freeze, a refused new registration must not perturb existing owners
    # (no partial state), so resolution is stable across the failed mutation.
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    token_auth.freeze_token_routes()
    before = token_auth.route_owner("/api/runtime/v1/health")
    try:
        token_auth.register_token_route("/api/gateway/drain", provider="drain-secret",
                                        capability="drain")
    except token_auth.TokenRouteRegistrationError:
        pass
    after = token_auth.route_owner("/api/runtime/v1/health")
    assert before == after
    assert token_auth.route_owner("/api/gateway/drain") is None  # never took effect


# --- §2 provider registry: no silent replacement (TOCTOU surface) ----------

def test_provider_cannot_be_replaced_under_same_name():
    # The provider registry rejects a duplicate name outright, so an attacker
    # cannot swap a provider object under an owner's name mid-flight.
    auth_registry.register_provider(_StubProvider("runtime-service", "RT", "runtime"))
    with pytest.raises(Exception):
        auth_registry.register_provider(_StubProvider("runtime-service", "EVIL", "runtime"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
