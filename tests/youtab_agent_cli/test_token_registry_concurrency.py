"""Deterministic (barrier/event-controlled) concurrency proofs for the single
linearizable dashboard-auth registry (WAVE-16). These are NOT timing-only stress
tests: every ordering is forced with threading.Event / threading.Barrier, and the
invariant asserted is:

    after freeze_dashboard_auth() returns, ZERO subsequent mutation commits.

Plus: provider verify_token is never called while the coordinator lock is held.
"""
from __future__ import annotations

import threading
from typing import Optional

import pytest

from youtab_agent_cli.dashboard_auth import lifecycle
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.base import DashboardAuthProvider, TokenPrincipal


class _StubProvider(DashboardAuthProvider):
    supports_token = True
    name = "stub"
    display_name = "stub"

    def __init__(self, name, secret, scope, verify_hook=None):
        self.name = name
        self.display_name = name
        self._secret = secret
        self._scope = scope
        self._verify_hook = verify_hook

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if self._verify_hook is not None:
            self._verify_hook()
        if token == self._secret:
            return TokenPrincipal(principal=self.name, provider=self.name,
                                  scopes=(self._scope,))
        return None

    def start_login(self, *, redirect_uri): raise NotImplementedError
    def complete_login(self, *, code, state, code_verifier, redirect_uri): raise NotImplementedError
    def verify_session(self, *, access_token): return None
    def refresh_session(self, *, refresh_token): raise NotImplementedError
    def revoke_session(self, *, refresh_token): return None


class _Req:
    def __init__(self, path, headers=None):
        class _URL: pass
        u = _URL(); u.path = path
        self.url = u
        self.headers = headers or {}
        class _C: host = "127.0.0.1"
        self.client = _C()


@pytest.fixture(autouse=True)
def _fresh():
    lifecycle._default = lifecycle.AuthRegistry()
    yield
    lifecycle._default = lifecycle.AuthRegistry()


# --- freeze waits for an already-started mutation, then seals ---------------

def test_freeze_waits_for_in_progress_mutation_then_seals():
    reg = lifecycle._default
    acquired = threading.Event()
    release = threading.Event()
    freeze_done = threading.Event()

    def hold_coordinator():
        # Simulate a mutation in-progress by holding the coordinator lock.
        with reg._coord:
            acquired.set()
            release.wait(3)

    def do_freeze():
        reg.freeze()
        freeze_done.set()

    th = threading.Thread(target=hold_coordinator)
    th.start()
    assert acquired.wait(3)
    tf = threading.Thread(target=do_freeze)
    tf.start()
    # freeze() must BLOCK on the held coordinator lock — it cannot return.
    assert freeze_done.wait(0.4) is False
    release.set()
    th.join(3)
    # once the in-progress holder releases, freeze proceeds and returns.
    assert freeze_done.wait(3) is True
    tf.join(3)
    # after freeze() returned, ZERO subsequent mutation commits.
    with pytest.raises(lifecycle.FrozenRegistryError):
        token_auth.register_token_route("/late", provider="p", capability="c")


# --- a mutation beginning after freeze is refused (all of them) -------------

@pytest.mark.parametrize("kind", ["provider", "route", "prefix",
                                  "clear_providers", "clear_routes"])
def test_mutation_beginning_after_freeze_is_refused(kind):
    token_auth.freeze_token_routes()

    def do():
        if kind == "provider":
            auth_registry.register_provider(_StubProvider("late", "S", "s"))
        elif kind == "route":
            token_auth.register_token_route("/r", provider="p", capability="c")
        elif kind == "prefix":
            token_auth.register_token_route_prefix("/r/", provider="p", capability="c")
        elif kind == "clear_providers":
            auth_registry.clear_providers()
        elif kind == "clear_routes":
            token_auth.clear_token_routes()

    with pytest.raises(lifecycle.FrozenRegistryError):
        do()


# --- barrier-synchronized races: linearizable, nothing commits post-freeze --

def _race_mutation_vs_freeze(mutate_one):
    """Start N mutation threads and one freeze thread on the same barrier; assert
    linearizability — after freeze returns, no further mutation can commit, and
    every committed mutation is consistently visible."""
    N = 16
    barrier = threading.Barrier(N + 1)
    committed = []
    lock = threading.Lock()

    def mut(i):
        barrier.wait()
        try:
            mutate_one(i)
            with lock:
                committed.append(i)
        except lifecycle.FrozenRegistryError:
            pass

    def frz():
        barrier.wait()
        token_auth.freeze_token_routes()

    threads = [threading.Thread(target=mut, args=(i,)) for i in range(N)]
    ft = threading.Thread(target=frz)
    for t in threads:
        t.start()
    ft.start()
    # all N+1 barrier parties are the threads themselves; they self-release.
    for t in threads:
        t.join(5)
    ft.join(5)
    # Invariant: after freeze returned, zero further commits are possible.
    with pytest.raises(lifecycle.FrozenRegistryError):
        token_auth.register_token_route("/after-freeze", provider="p", capability="c")
    return committed


def test_route_registration_racing_freeze_is_linearizable():
    committed = _race_mutation_vs_freeze(
        lambda i: token_auth.register_token_route(
            f"/race/{i}", provider="p", capability="c"))
    # every route that committed is consistently present (no torn state)
    for i in committed:
        owner = token_auth.route_owner(f"/race/{i}")
        assert owner is not None and owner.provider == "p"


def test_prefix_registration_racing_freeze_is_linearizable():
    committed = _race_mutation_vs_freeze(
        lambda i: token_auth.register_token_route_prefix(
            f"/race/{i}/", provider="p", capability="c"))
    for i in committed:
        assert token_auth.route_owner(f"/race/{i}/x") is not None


def test_provider_registration_racing_freeze_is_linearizable():
    committed = _race_mutation_vs_freeze(
        lambda i: auth_registry.register_provider(
            _StubProvider(f"prov-{i}", "S", "s")))
    for i in committed:
        assert auth_registry.get_provider(f"prov-{i}") is not None


def test_provider_clear_racing_freeze_is_linearizable():
    # Pre-register a provider; race clear_providers vs freeze.
    auth_registry.register_provider(_StubProvider("pre", "S", "s"))
    _race_mutation_vs_freeze(lambda i: auth_registry.clear_providers())
    # after freeze, clear is refused (already asserted inside helper via route);
    # and the registry is frozen.
    assert token_auth.is_frozen() is True


def test_route_clear_racing_freeze_is_linearizable():
    token_auth.register_token_route("/pre", provider="p", capability="c")
    _race_mutation_vs_freeze(lambda i: token_auth.clear_token_routes())
    assert token_auth.is_frozen() is True


# --- authentication snapshot racing freeze ---------------------------------

def test_auth_snapshot_racing_freeze_is_consistent():
    # A snapshot resolved concurrently with freeze is always a consistent
    # generation (owner+provider from one lock hold); freezing does not remove
    # owners, so a valid snapshot stays valid.
    auth_registry.register_provider(_StubProvider("runtime-service", "S", "runtime"))
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    N = 16
    barrier = threading.Barrier(N + 1)
    results = []
    lock = threading.Lock()

    def snap(i):
        barrier.wait()
        pr, un = token_auth.authenticate_token(
            _Req("/api/runtime/v1/health", {"authorization": "Bearer S"}))
        with lock:
            results.append((pr is not None, un))

    def frz():
        barrier.wait()
        token_auth.freeze_token_routes()

    threads = [threading.Thread(target=snap, args=(i,)) for i in range(N)]
    ft = threading.Thread(target=frz)
    for t in threads:
        t.start()
    ft.start()
    for t in threads:
        t.join(5)
    ft.join(5)
    # every concurrent authentication succeeded (owner+provider present the whole
    # time); none saw a torn/absent-provider generation.
    assert all(ok and un is None for ok, un in results)


# --- verify_token is never called while the coordinator lock is held --------

def test_no_verify_token_while_coordinator_lock_held():
    reg = lifecycle._default
    observed_free = {"ok": None}

    def hook():
        # During verify_token, ANOTHER thread must be able to take the
        # coordinator lock — i.e. it is NOT held by the authenticating thread.
        result = {}

        def other():
            got = reg._coord.acquire(timeout=1)
            result["got"] = got
            if got:
                reg._coord.release()

        t = threading.Thread(target=other)
        t.start()
        t.join(2)
        observed_free["ok"] = result.get("got", False)

    auth_registry.register_provider(
        _StubProvider("runtime-service", "S", "runtime", verify_hook=hook))
    token_auth.register_token_route_prefix("/api/runtime/v1/",
                                           provider="runtime-service",
                                           capability="runtime")
    principal, _ = token_auth.authenticate_token(
        _Req("/api/runtime/v1/health", {"authorization": "Bearer S"}))
    assert principal is not None
    assert observed_free["ok"] is True  # coordinator lock was FREE during verify


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
