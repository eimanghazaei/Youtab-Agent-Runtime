"""WAVE-27 egress boundary wiring: ambient context, observe/enforce modes,
non-httpx audited adapters, and the SSRF-factory routing."""

from __future__ import annotations

import pytest

from youtab_runtime import egress_audit as ea
from youtab_runtime.egress_audit import EgressDenied, EgressPolicy
from youtab_runtime.egress_context import (
    SYSTEM_RUN_ID,
    current_context,
    egress_run_context,
    in_run_context,
    system_principal,
)
from youtab_runtime.run_journal import Principal, list_events

P = Principal("tenantA", "userA")


@pytest.fixture
def db(tmp_path):
    return tmp_path / "run_journal.db"


@pytest.fixture(autouse=True)
def _deny_posture():
    # Deterministic, offline: deny everything except the one allowlisted host.
    ea.set_policy_provider(
        lambda: EgressPolicy(
            network_deny=True, allowlist=frozenset({"provider.internal.bench"})
        )
    )
    yield
    ea.reset_policy_provider()


# --------------------------------------------------------------------------- #
# Ambient context
# --------------------------------------------------------------------------- #
def test_system_fallback_when_no_run():
    assert not in_run_context()
    ctx = current_context()
    assert ctx.run_id == SYSTEM_RUN_ID
    assert ctx.principal == system_principal()


def test_run_context_sets_and_restores():
    with egress_run_context("run-1", P):
        assert in_run_context()
        assert current_context().run_id == "run-1"
        assert current_context().principal == P
    assert not in_run_context()


def test_run_context_rejects_bad_principal():
    with pytest.raises(TypeError):
        with egress_run_context("run-1", "not-a-principal"):  # type: ignore[arg-type]
            pass


# --------------------------------------------------------------------------- #
# Non-httpx audited adapters (requests): enforce vs observe
# --------------------------------------------------------------------------- #
def _egress(db):
    return [e for e in list_events(SYSTEM_RUN_ID, system_principal(), db_path=db,
                                   category="egress")]


def test_requests_enforce_denies_before_call(monkeypatch, db):
    from youtab_runtime import egress_adapters as adp

    called = {"n": 0}

    def _fake_request(method, url, **kw):
        called["n"] += 1
        raise AssertionError("must not reach the network on a denied destination")

    monkeypatch.setattr("requests.request", _fake_request)
    # Route audit to the isolated db by binding a run context whose db is db.
    # (adapters read ambient principal/run; the journal db is resolved via env in
    # prod, but here we assert the deny happens BEFORE any network call.)
    with pytest.raises(EgressDenied):
        adp.audited_requests_request(
            "GET", "https://blocked.example/x", adapter="test", enforce=True
        )
    assert called["n"] == 0


def test_requests_observe_proceeds_on_denied(monkeypatch, db):
    from youtab_runtime import egress_adapters as adp

    class _Resp:
        status_code = 200

    called = {"n": 0}

    def _fake_request(method, url, **kw):
        called["n"] += 1
        return _Resp()

    monkeypatch.setattr("requests.request", _fake_request)
    resp = adp.audited_requests_request(
        "GET", "https://blocked.example/x", adapter="test", enforce=False
    )
    assert resp.status_code == 200
    assert called["n"] == 1  # observe mode does not block


def test_requests_enforce_allows_allowlisted(monkeypatch, db):
    from youtab_runtime import egress_adapters as adp

    class _Resp:
        status_code = 201

    monkeypatch.setattr("requests.request", lambda *a, **k: _Resp())
    resp = adp.audited_requests_request(
        "POST", "https://provider.internal.bench/v1", adapter="test", enforce=True
    )
    assert resp.status_code == 201


def test_urlopen_enforce_denies_before_call(monkeypatch):
    from youtab_runtime import egress_adapters as adp

    def _fake_urlopen(url, **kw):
        raise AssertionError("must not open a denied destination")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    with pytest.raises(EgressDenied):
        adp.audited_urlopen("http://169.254.169.254/latest/meta-data/",
                            adapter="test", enforce=True)


# --------------------------------------------------------------------------- #
# httpx audited client: enforce raises before connect on a denied destination
# --------------------------------------------------------------------------- #
def test_httpx_audited_enforce_denies_before_connect():
    from youtab_runtime.egress_guard_http import audited_client

    client = audited_client(run_id="run-x", principal=P, adapter="test",
                            enforce=True)
    try:
        request = client.build_request("GET", "https://blocked.example/")
        with pytest.raises(EgressDenied):
            client.send(request)
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# SSRF factory routing: audited inside a run, plain outside
# --------------------------------------------------------------------------- #
def test_ssrf_factory_audited_inside_run_plain_outside():
    from tools.url_safety import create_ssrf_safe_client

    outside = create_ssrf_safe_client()
    try:
        assert type(outside).__name__ == "Client"  # plain httpx.Client
    finally:
        outside.close()

    with egress_run_context("run-1", P):
        inside = create_ssrf_safe_client()
        try:
            # Observe-mode audited subclass built via the ambient context.
            assert type(inside).__name__ == "_AuditedClient"
        finally:
            inside.close()
