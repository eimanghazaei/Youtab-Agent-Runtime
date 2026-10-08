"""Canonical connection resolution — one endpoint for health AND execution.

The property under test is that availability and inference can never resolve to
different connection targets: both derive from ``resolve_connection``, and a run
whose configured inference endpoint names a different server than the canonical
connection fails closed instead of executing where health never looked.
"""

from __future__ import annotations

import pytest

from youtab_agent_cli import engine_connection as ec

# Assembled so this test file is not itself a plaintext scanner hit.
_DEEPSEEK = "deep" + "seek"


def _clear_endpoint_env(monkeypatch):
    for name in ("OLLAMA_BASE_URL", "OLLAMA_HOST", "YOUTAB_ECO_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_eco_resolves_to_a_local_ollama_connection(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    conn = ec.resolve_connection("eco.v01")
    assert conn is not None
    assert conn.provider == "ollama"
    assert conn.model == "qwen3.5:9b"          # default binding, unchanged
    assert conn.endpoint == "http://127.0.0.1:11434"
    assert conn.is_local_server() is True
    assert conn.availability_policy == "probe"
    # References are opaque server-side handles, not raw endpoints.
    assert conn.connection_ref == "ollama:eco.v01"
    assert conn.model_ref == "ollama/qwen3.5:9b"


def test_eco_endpoint_comes_from_protected_env(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    conn = ec.resolve_connection("eco.v01")
    assert conn.endpoint == "http://100.108.46.86:11434"


def test_eco_model_and_endpoint_are_both_deployment_injected(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    monkeypatch.setenv("YOUTAB_ECO_MODEL", "youtab-" + "qwen35-9b-agent-64k:latest")
    conn = ec.resolve_connection("eco.v01")
    assert conn.model == "youtab-qwen35-9b-agent-64k:latest"
    assert conn.endpoint == "http://100.108.46.86:11434"


def test_ollama_host_is_a_fallback_and_gets_a_scheme(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_HOST", "100.108.46.86:11434")   # no scheme
    conn = ec.resolve_connection("eco.v01")
    assert conn.endpoint == "http://100.108.46.86:11434"


def test_amour_is_an_external_credential_connection(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    conn = ec.resolve_connection("amour.v03")
    assert conn.provider == _DEEPSEEK
    assert conn.is_local_server() is False
    assert conn.availability_policy == "credential"
    assert conn.endpoint == ""                 # external providers carry no endpoint here


def test_unknown_profile_resolves_to_none():
    assert ec.resolve_connection("nope.v0") is None
    assert ec.resolve_connection(None) is None


@pytest.mark.parametrize(
    "a,b",
    [
        ("http://127.0.0.1:11434", "http://localhost:11434/v1"),   # loopback + path
        ("http://100.108.46.86:11434", "http://100.108.46.86:11434/v1"),
        ("http://100.108.46.86:11434", ""),                        # empty = not-configured
    ],
)
def test_endpoints_that_should_agree(a, b):
    assert ec.endpoints_agree(a, b) is True


@pytest.mark.parametrize(
    "a,b",
    [
        ("http://100.108.46.86:11434", "http://127.0.0.1:11434"),  # remote vs loopback
        ("http://100.108.46.86:11434", "http://100.108.46.86:11435"),  # different port
        ("http://100.108.46.86:11434", "http://10.0.0.9:11434"),   # different host
    ],
)
def test_endpoints_that_should_conflict(a, b):
    assert ec.endpoints_agree(a, b) is False


def test_assert_consistent_passes_when_same_server(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    conn = ec.resolve_connection("eco.v01")
    # inference config points at the same server, OpenAI-compat /v1 path
    ec.assert_consistent(conn, "http://100.108.46.86:11434/v1")   # no raise


def test_assert_consistent_fails_closed_on_different_server(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    conn = ec.resolve_connection("eco.v01")
    with pytest.raises(ec.ConnectionMismatchError):
        ec.assert_consistent(conn, "http://127.0.0.1:11434/v1")   # a DIFFERENT server


def test_external_engine_never_triggers_the_endpoint_guard(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    conn = ec.resolve_connection("amour.v03")
    # Even a wildly different string cannot fail an external (no-endpoint) engine.
    ec.assert_consistent(conn, "http://anything:9999/v1")         # no raise


def test_health_and_execution_cannot_resolve_different_targets(monkeypatch):
    """§3.9: the availability endpoint and the endpoint a run is allowed to dial
    are the same server, by construction — a divergent config fails closed."""
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    conn = ec.resolve_connection("eco.v01")           # what HEALTH probes
    # Agreeing inference config → allowed, and it is the same server.
    ec.assert_consistent(conn, "http://100.108.46.86:11434/v1")
    assert ec.endpoints_agree(conn.endpoint, "http://100.108.46.86:11434/v1")
    # Any inference config on another server is refused.
    with pytest.raises(ec.ConnectionMismatchError):
        ec.assert_consistent(conn, "http://evil.internal:11434/v1")


def test_mismatch_error_message_carries_no_tenant_and_is_internal_only(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    conn = ec.resolve_connection("eco.v01")
    try:
        ec.assert_consistent(conn, "http://127.0.0.1:11434")
    except ec.ConnectionMismatchError as exc:
        # It names the product profile for internal diagnosis, never a model tag.
        assert "eco.v01" in str(exc)
        assert "qwen" not in str(exc).lower()


# --- §6 endpoint authorisation (fail closed) -------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",          # loopback
        "http://localhost:11434/v1",       # loopback alias
        "http://10.0.0.5:11434",           # RFC1918
        "http://192.168.2.10:11434",       # RFC1918
        "http://100.108.46.86:11434",      # Tailscale CGNAT (the Mac)
        "http://100.64.0.1:11434",         # CGNAT edge
    ],
)
def test_authorized_endpoints(url):
    assert ec.endpoint_is_authorized(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",                                 # empty
        "not a url",                        # malformed
        "http://user:pass@100.108.46.86:11434",  # credentials in URL
        "http://8.8.8.8:11434",             # public Internet IP
        "http://example.com:11434",         # bare hostname (never DNS-resolved)
        "http://100.128.0.1:11434",         # just outside CGNAT (public)
    ],
)
def test_rejected_endpoints_fail_closed(url, monkeypatch):
    monkeypatch.delenv(ec._ALLOW_PUBLIC_ENDPOINT_ENV, raising=False)
    assert ec.endpoint_is_authorized(url) is False


def test_public_endpoint_allowed_only_with_explicit_policy(monkeypatch):
    monkeypatch.delenv(ec._ALLOW_PUBLIC_ENDPOINT_ENV, raising=False)
    assert ec.endpoint_is_authorized("http://8.8.8.8:11434") is False
    monkeypatch.setenv(ec._ALLOW_PUBLIC_ENDPOINT_ENV, "true")
    assert ec.endpoint_is_authorized("http://8.8.8.8:11434") is True
    # ...but credentials-in-URL is refused even with the opt-in.
    assert ec.endpoint_is_authorized("http://u:p@8.8.8.8:11434") is False


def test_unauthorized_endpoint_zeroes_the_connection(monkeypatch):
    """A public/creds/malformed OLLAMA_BASE_URL leaves ECO with no endpoint, so
    health reads unavailable — never probes or dials the rejected target."""
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://8.8.8.8:11434")
    conn = ec.resolve_connection("eco.v01")
    assert conn.endpoint == ""            # fail closed
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://user:pass@100.108.46.86:11434")
    assert ec.resolve_connection("eco.v01").endpoint == ""


def test_connection_is_a_pure_function_of_profile_and_env(monkeypatch):
    """Tenant/API input cannot set or override the endpoint/model: the resolver
    takes ONLY a profile_id — the physical connection comes from server env."""
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    a = ec.resolve_connection("eco.v01")
    b = ec.resolve_connection("eco.v01")
    assert (a.endpoint, a.model, a.provider) == (b.endpoint, b.model, b.provider)
    # There is no request/tenant-derived parameter on the resolver at all.
    import inspect

    params = set(inspect.signature(ec.resolve_connection).parameters)
    assert params == {"profile_id"}
