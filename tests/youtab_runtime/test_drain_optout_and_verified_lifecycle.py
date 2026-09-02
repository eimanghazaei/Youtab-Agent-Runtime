"""WAVE-21: drain opt-out is fail-closed, and the registry lifecycle is a sealed,
monotonic BUILDING → FROZEN_UNVERIFIED → VERIFIED state machine.

Two things are proven here against production code:

1. The real ``gateway_drain`` handler has an INDEPENDENT, unconditional guard:
   with no valid drain secret it returns ``503 drain_disabled`` before ANY side
   effect — an ``ops:manage`` cookie principal that reached the handler cannot
   drive a drain (no cookie fallback). The destructive marker writes are stubbed;
   a real drain is never executed.
2. The AuthRegistry lifecycle is monotonic and terminal: token auth serves only
   in VERIFIED; a frozen-but-unverified registry fails closed; a requirement
   declared after VERIFIED is refused without mutation; there is no reverse
   transition and no reset.
"""
from __future__ import annotations

import asyncio
import threading
import types
from typing import Optional

import pytest

from youtab_agent_cli import web_server
from youtab_agent_cli.dashboard_auth import lifecycle
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.base import DashboardAuthProvider, TokenPrincipal
from youtab_agent_cli.dashboard_auth.lifecycle import (
    LifecycleState,
    RegistryNotVerifiedError,
    ServiceRouteRegistrationError,
)

import plugins.dashboard_auth.drain as drain_plugin

DRAIN_SECRET = "DR-" + "z2Wm5Xb8nQ4rT6yU1oI3pA7sD9fG0hJ2kL4vC6xB8nM"
DRAIN_ENV = "YOUTAB_AGENT_DASHBOARD_DRAIN_SECRET"


# ----------------------------------------------------------------------------
# Drain handler independent guard (call the REAL handler; stub the side effect)
# ----------------------------------------------------------------------------

class _FakeState:
    def __init__(self, principal=None, authed=False):
        self.token_principal = principal
        self.token_authenticated = authed


class _FakeRequest:
    def __init__(self, body, principal=None, authed=False):
        self._body = body
        self.state = _FakeState(principal, authed)
        self.headers = {}
        self.client = types.SimpleNamespace(host="127.0.0.1")

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _no_real_drain(monkeypatch):
    """Stub the destructive marker writes so a real drain is NEVER executed and
    we can assert zero side effects."""
    import gateway.drain_control as dc
    calls = {"write": 0, "clear": 0}

    def _w(**k):
        calls["write"] += 1
        return {"requested_at": "NA", "suppress_notification": False}

    def _c():
        calls["clear"] += 1
        return False

    monkeypatch.setattr(dc, "write_drain_request", _w)
    monkeypatch.setattr(dc, "clear_drain_request", _c)
    monkeypatch.setattr(dc, "drain_requested", lambda: False, raising=False)
    return calls


def _call_drain(req):
    return asyncio.run(web_server.gateway_drain(req))


def _drain_principal():
    return TokenPrincipal(principal="drain-control", provider="drain-secret",
                          scopes=("drain",))


def _ops_cookie_principal():
    # A stand-in for an interactive ops:manage principal that (hypothetically)
    # reached the handler — it is NOT a drain token principal.
    return types.SimpleNamespace(provider=None, scopes=(), principal="opsadmin")


@pytest.mark.parametrize("secret", [None, "", "weak", "a" * 10])
def test_drain_disabled_503_without_valid_secret(monkeypatch, _no_real_drain, secret):
    if secret is None:
        monkeypatch.delenv(DRAIN_ENV, raising=False)
    else:
        monkeypatch.setenv(DRAIN_ENV, secret)
    # Even simulating an ops:manage cookie principal that reached the handler:
    with pytest.raises(Exception) as ei:
        _call_drain(_FakeRequest({"action": "drain"},
                                 principal=_ops_cookie_principal(), authed=True))
    exc = ei.value
    assert getattr(exc, "status_code", None) == 503
    assert exc.detail == {"error": "drain_disabled"}
    assert _no_real_drain == {"write": 0, "clear": 0}  # NO side effect


def test_drain_valid_secret_but_cookie_principal_forbidden(monkeypatch, _no_real_drain):
    monkeypatch.setenv(DRAIN_ENV, DRAIN_SECRET)  # surface enabled
    with pytest.raises(Exception) as ei:
        _call_drain(_FakeRequest({"action": "drain"},
                                 principal=_ops_cookie_principal(), authed=True))
    assert getattr(ei.value, "status_code", None) == 403
    assert ei.value.detail == {"error": "drain_forbidden"}
    assert _no_real_drain == {"write": 0, "clear": 0}


def test_drain_valid_secret_and_drain_principal_proceeds(monkeypatch, _no_real_drain):
    monkeypatch.setenv(DRAIN_ENV, DRAIN_SECRET)
    out = _call_drain(_FakeRequest({"action": "drain"},
                                   principal=_drain_principal(), authed=True))
    assert out["ok"] is True and out["action"] == "drain"
    assert _no_real_drain["write"] == 1  # intended side effect happened once


def test_drain_unauthenticated_denied_no_side_effect(monkeypatch, _no_real_drain):
    monkeypatch.setenv(DRAIN_ENV, DRAIN_SECRET)
    with pytest.raises(Exception) as ei:
        _call_drain(_FakeRequest({"action": "drain"}, principal=None, authed=False))
    assert getattr(ei.value, "status_code", None) == 403
    assert _no_real_drain == {"write": 0, "clear": 0}


def test_drain_guard_error_carries_no_secret(monkeypatch, _no_real_drain):
    monkeypatch.setenv(DRAIN_ENV, DRAIN_SECRET)
    with pytest.raises(Exception) as ei:
        _call_drain(_FakeRequest({"action": "drain"},
                                 principal=_ops_cookie_principal(), authed=True))
    assert DRAIN_SECRET not in repr(ei.value.detail)


def test_is_drain_enabled_matches_provider_contract(monkeypatch):
    monkeypatch.delenv(DRAIN_ENV, raising=False)
    assert drain_plugin.is_drain_enabled() is False
    monkeypatch.setenv(DRAIN_ENV, "weak")
    assert drain_plugin.is_drain_enabled() is False
    monkeypatch.setenv(DRAIN_ENV, DRAIN_SECRET)
    assert drain_plugin.is_drain_enabled() is True


# ----------------------------------------------------------------------------
# Verified lifecycle state machine
# ----------------------------------------------------------------------------

class _Stub(DashboardAuthProvider):
    supports_token = True
    name = "stub"
    display_name = "stub"

    def __init__(self, name, secret, scope):
        self.name = name
        self.display_name = name
        self._secret = secret
        self._scope = scope

    def verify_token(self, *, token):
        if token == self._secret:
            return TokenPrincipal(principal=self.name, provider=self.name,
                                  scopes=(self._scope,))
        return None

    def start_login(self, *, redirect_uri): raise NotImplementedError
    def complete_login(self, *, code, state, code_verifier, redirect_uri): raise NotImplementedError
    def verify_session(self, *, access_token): return None
    def refresh_session(self, *, refresh_token): raise NotImplementedError
    def revoke_session(self, *, refresh_token): return None


def _healthy_registry():
    r = lifecycle.AuthRegistry()
    r.require_route_ownership(provider="runtime-service", path="/api/runtime/v1/",
                             is_prefix=True, capability="runtime")
    r.register_provider(_Stub("runtime-service", "S", "runtime"))
    r.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service",
                                  capability="runtime")
    return r


def test_initial_state_is_building():
    assert lifecycle.AuthRegistry().state() is LifecycleState.BUILDING


def test_freeze_produces_frozen_unverified():
    r = _healthy_registry()
    r.freeze()
    assert r.state() is LifecycleState.FROZEN_UNVERIFIED
    assert r.is_frozen() is True and r.is_verified() is False


def test_valid_verification_produces_verified():
    r = _healthy_registry()
    r.freeze()
    r.verify_required_ownerships()
    assert r.state() is LifecycleState.VERIFIED and r.is_verified() is True


def test_failed_verification_never_reaches_verified():
    r = lifecycle.AuthRegistry()
    r.require_route_ownership(provider="runtime-service", path="/api/runtime/v1/",
                             is_prefix=True, capability="runtime")
    # provider/route intentionally NOT registered -> requirement unmet
    r.freeze()
    with pytest.raises(ServiceRouteRegistrationError):
        r.verify_required_ownerships()
    assert r.state() is LifecycleState.FROZEN_UNVERIFIED
    assert r.is_verified() is False


def test_verify_before_freeze_is_refused():
    r = _healthy_registry()
    with pytest.raises(RegistryNotVerifiedError):
        r.verify_required_ownerships()
    assert r.state() is LifecycleState.BUILDING


def test_mutations_after_freeze_refused():
    r = _healthy_registry()
    r.freeze()
    with pytest.raises(lifecycle.FrozenRegistryError):
        r.register_provider(_Stub("late", "S", "s"))
    with pytest.raises(lifecycle.TokenRouteRegistrationError):
        r.register_token_route("/late", provider="p", capability="c")


def test_requirement_after_verified_refused_without_mutation():
    r = _healthy_registry()
    r.freeze()
    r.verify_required_ownerships()
    before = list(r._required_ownerships)
    with pytest.raises(lifecycle.FrozenRegistryError):
        r.require_route_ownership(provider="p2", path="/late", is_prefix=False,
                                 capability="c")
    assert r._required_ownerships == before  # no mutation
    assert r.state() is LifecycleState.VERIFIED


def test_requirement_during_frozen_unverified_is_kept_and_aborts():
    # freeze-before-registration: requirement declared in FROZEN_UNVERIFIED is
    # NOT lost, and verification catches it as unmet.
    r = lifecycle.AuthRegistry()
    r.freeze()
    r.require_route_ownership(provider="runtime-service", path="/api/runtime/v1/",
                             is_prefix=True, capability="runtime")
    assert len(r._required_ownerships) == 1
    with pytest.raises(ServiceRouteRegistrationError):
        r.verify_required_ownerships()
    assert r.state() is LifecycleState.FROZEN_UNVERIFIED


def test_no_reverse_transition():
    r = _healthy_registry()
    r.freeze()
    r.verify_required_ownerships()
    r.freeze()  # monotonic no-op, must NOT drop back
    assert r.state() is LifecycleState.VERIFIED


def test_verify_is_idempotent_in_verified():
    r = _healthy_registry()
    r.freeze()
    r.verify_required_ownerships()
    r.verify_required_ownerships()  # idempotent no-op
    assert r.state() is LifecycleState.VERIFIED


def test_no_reset_or_unfreeze_callable_ships():
    for mod in (lifecycle, token_auth, auth_registry):
        for name in dir(mod):
            low = name.lower()
            assert not (("reset" in low or "unfreeze" in low)
                        and callable(getattr(mod, name))), f"{mod.__name__}.{name}"


# --- middleware fails closed on a frozen-but-unverified registry ------------

class _Resp:
    pass


async def _call_next(_req):
    r = _Resp(); r.status_code = 200; r.served = True
    return r


class _MwReq:
    def __init__(self, path, headers):
        u = types.SimpleNamespace(path=path)
        self.url = u
        self.headers = headers
        self.state = types.SimpleNamespace()
        self.client = types.SimpleNamespace(host="127.0.0.1")


@pytest.fixture()
def _fresh(monkeypatch):
    monkeypatch.setattr(lifecycle, "_default", lifecycle.AuthRegistry())
    yield


def test_serving_before_verified_fails_closed(_fresh):
    reg = lifecycle._default
    reg.register_provider(_Stub("runtime-service", "S", "runtime"))
    reg.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service",
                                    capability="runtime")
    reg.freeze()  # FROZEN_UNVERIFIED — NOT verified
    resp = asyncio.run(token_auth.token_auth_middleware(
        _MwReq("/api/runtime/v1/health", {"authorization": "Bearer S"}), _call_next))
    assert resp.status_code == 503
    import json
    assert json.loads(bytes(resp.body)).get("error") == "service_unverified"


def test_serving_after_verified_authenticates(_fresh):
    reg = lifecycle._default
    reg.register_provider(_Stub("runtime-service", "S", "runtime"))
    reg.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service",
                                    capability="runtime")
    reg.freeze()
    reg.verify_required_ownerships()  # -> VERIFIED
    resp = asyncio.run(token_auth.token_auth_middleware(
        _MwReq("/api/runtime/v1/health", {"authorization": "Bearer S"}), _call_next))
    assert getattr(resp, "served", False) is True  # passed through to call_next


def test_building_serves_without_verified(_fresh):
    # BUILDING is the pre-freeze construction state used by lifespan-less test
    # harnesses; it authenticates (production reaches serving only via the
    # lifespan, which freezes+verifies before yield).
    reg = lifecycle._default
    reg.register_provider(_Stub("runtime-service", "S", "runtime"))
    reg.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service",
                                    capability="runtime")
    resp = asyncio.run(token_auth.token_auth_middleware(
        _MwReq("/api/runtime/v1/health", {"authorization": "Bearer S"}), _call_next))
    assert getattr(resp, "served", False) is True


# --- concurrency: freeze/verify/late-declare linearizable -------------------

def test_freeze_verify_late_declare_linearizable(_fresh):
    reg = _healthy_registry()
    lifecycle._default = reg
    barrier = threading.Barrier(3)

    def do_freeze():
        barrier.wait()
        reg.freeze()

    def do_verify():
        barrier.wait()
        for _ in range(200):
            if reg.is_frozen():
                try:
                    reg.verify_required_ownerships()
                except ServiceRouteRegistrationError:
                    # A late "/late" requirement recorded before verify makes
                    # verification fail — correct fail-closed, not an error.
                    pass
                return

    def do_late():
        barrier.wait()
        for _ in range(200):
            try:
                reg.require_route_ownership(provider="x", path="/late",
                                           is_prefix=False, capability="c")
            except lifecycle.FrozenRegistryError:
                pass  # refused once VERIFIED — expected

    ts = [threading.Thread(target=f) for f in (do_freeze, do_verify, do_late)]
    for t in ts: t.start()
    for t in ts: t.join(5)
    st = reg.state()
    has_late = any(r.path == "/late" for r in reg._required_ownerships)
    # The linearizability invariant: a VERIFIED registry NEVER contains an
    # unverified late requirement (a late declare either landed before verify —
    # which then fails and stays FROZEN_UNVERIFIED — or was refused post-VERIFIED).
    assert not (st is LifecycleState.VERIFIED and has_late)
    assert st in (LifecycleState.VERIFIED, LifecycleState.FROZEN_UNVERIFIED)
    # No reverse transition ever occurred.
    assert st >= LifecycleState.FROZEN_UNVERIFIED


def test_verify_token_runs_without_coordinator_lock(_fresh):
    reg = lifecycle._default
    observed = {"free": None}

    class _HookStub(_Stub):
        def verify_token(self, *, token):
            got = {}
            def other():
                got["ok"] = reg._coord.acquire(timeout=1)
                if got.get("ok"):
                    reg._coord.release()
            t = threading.Thread(target=other); t.start(); t.join(2)
            observed["free"] = got.get("ok", False)
            return super().verify_token(token=token)

    reg.register_provider(_HookStub("runtime-service", "S", "runtime"))
    reg.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service",
                                    capability="runtime")
    reg.freeze(); reg.verify_required_ownerships()
    token_auth.authenticate_token(
        _MwReq("/api/runtime/v1/health", {"authorization": "Bearer S"}))
    assert observed["free"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
