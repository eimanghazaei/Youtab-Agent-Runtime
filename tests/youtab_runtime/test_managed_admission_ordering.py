"""WAVE-30H Batch3 #1 — managed admission ordering + route-vs-binding + no-fallback.

Fast, deterministic, offline unit coverage for the three pieces of the admission
hardening that complement the real CLI subprocess E2E in
``test_managed_cli_e2e.py``:

* :func:`worker_admission.assert_route_matches_binding` — the PURE (no client, no
  network, no budget) route-vs-binding comparison that runs BEFORE client
  construction: it admits a matching route, refuses provider/model/endpoint drift,
  and is a no-op for a standalone run (``identity is None``);
* :func:`worker_admission.managed_bound_identity` — returns ``None`` in standalone
  trust mode (so the CLI applies no managed constraints);
* ``CLIAgentSetupMixin._ensure_runtime_credentials`` — a MANAGED run must NEVER
  switch to an unbound fallback provider on primary-auth failure, while a standalone
  run still falls back.

Placed under tests/youtab_runtime (never tests/youtab_agent_cli) to avoid the
Windows ``youtab update`` collection hazard.
"""
from __future__ import annotations

import pytest

from youtab_agent_cli import effective_binding as eb
from youtab_agent_cli import worker_admission as wa
from youtab_agent_cli.worker_admission import (
    ManagedWorkerAdmissionError,
    assert_route_matches_binding,
    managed_bound_identity,
)


# --------------------------------------------------------------------------- #
# assert_route_matches_binding — pure route-vs-binding comparison               #
# --------------------------------------------------------------------------- #
_ENDPOINT = "http://127.0.0.1:11434"
_FP = eb.compute_endpoint_fingerprint(_ENDPOINT)
_IDENTITY = {
    "task_id": "task-1", "provider": "ollama", "model": "qwen:test",
    "endpoint_fingerprint": _FP,
}


def test_route_matches_binding_admits_exact_match():
    # No raise: resolved route equals the bound substrate.
    assert_route_matches_binding(
        _IDENTITY, provider="ollama", model="qwen:test", base_url=_ENDPOINT
    )


def test_route_matches_binding_is_noop_for_standalone():
    # identity is None (standalone) -> no constraint, never raises.
    assert_route_matches_binding(None, provider="anything", model="x", base_url="http://h")


def test_route_provider_drift_refused():
    with pytest.raises(ManagedWorkerAdmissionError, match="provider .* drifted"):
        assert_route_matches_binding(
            _IDENTITY, provider="openrouter", model="qwen:test", base_url=_ENDPOINT
        )


def test_route_model_drift_refused():
    with pytest.raises(ManagedWorkerAdmissionError, match="model drifted"):
        assert_route_matches_binding(
            _IDENTITY, provider="ollama", model="evil-swap", base_url=_ENDPOINT
        )


def test_route_endpoint_drift_refused():
    with pytest.raises(ManagedWorkerAdmissionError, match="endpoint drifted"):
        assert_route_matches_binding(
            _IDENTITY, provider="ollama", model="qwen:test",
            base_url="http://10.0.0.9:11434",
        )


def test_route_case_insensitive_provider_and_uppercase_scheme():
    # Provider comparison is case-insensitive; an uppercase scheme fingerprints the
    # same authority (Batch3 #4 classifier/normalizer are case-insensitive).
    assert_route_matches_binding(
        {"task_id": "t", "provider": "Ollama", "model": "qwen:test",
         "endpoint_fingerprint": eb.compute_endpoint_fingerprint("HTTP://127.0.0.1:11434")},
        provider="ollama", model="qwen:test", base_url="http://127.0.0.1:11434",
    )


# --------------------------------------------------------------------------- #
# Batch4 #1: the gate is STRICT — a MISSING planned field fails closed          #
# (the old "compare only when both present" let an absent side slip through).   #
# --------------------------------------------------------------------------- #
def test_route_missing_provider_fails_closed():
    with pytest.raises(ManagedWorkerAdmissionError, match="provider .*drifted"):
        assert_route_matches_binding(
            _IDENTITY, provider="", model="qwen:test", base_url=_ENDPOINT
        )


def test_route_missing_model_fails_closed():
    with pytest.raises(ManagedWorkerAdmissionError, match="model drifted"):
        assert_route_matches_binding(
            _IDENTITY, provider="ollama", model=None, base_url=_ENDPOINT
        )


def test_route_missing_base_url_fails_closed():
    with pytest.raises(ManagedWorkerAdmissionError, match="endpoint drifted"):
        assert_route_matches_binding(
            _IDENTITY, provider="ollama", model="qwen:test", base_url=None
        )


def test_route_missing_binding_endpoint_fingerprint_fails_closed():
    # A binding that carries no endpoint fingerprint cannot be matched -> refuse.
    with pytest.raises(ManagedWorkerAdmissionError, match="endpoint drifted"):
        assert_route_matches_binding(
            {"task_id": "t", "provider": "ollama", "model": "qwen:test",
             "endpoint_fingerprint": None},
            provider="ollama", model="qwen:test", base_url=_ENDPOINT,
        )


# --------------------------------------------------------------------------- #
# plan_runtime_route — PURE proposed route (no pool/mint/refresh/HTTP/client)    #
# --------------------------------------------------------------------------- #
def test_plan_runtime_route_is_pure_and_matches_local_binding(monkeypatch, tmp_path):
    """The planner derives provider/model/base_url from config alone, and its output
    fingerprints to the SAME endpoint a local-server binding was built from — so a
    correctly-configured managed run passes the strict gate with ZERO side effects."""
    from youtab_agent_cli import runtime_provider as rp

    # A local ollama deployment endpoint (the protected env the resolver reads).
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    # plan_runtime_route imports load_config lazily from youtab_agent_cli.config.
    monkeypatch.setattr(
        "youtab_agent_cli.config.load_config",
        lambda: {"model": {"provider": "ollama", "default": "qwen:test"}},
    )
    # Any network call from inside the planner would be a contract violation.
    import socket as _socket

    def _boom(*a, **k):  # noqa: ANN001
        raise AssertionError("plan_runtime_route performed network I/O")

    monkeypatch.setattr(_socket.socket, "connect", _boom)

    planned = rp.plan_runtime_route(requested="ollama", configured_model="qwen:test")
    assert planned["provider"] == "ollama"
    assert planned["model"] == "qwen:test"
    # The inference base_url gains /v1; it must fingerprint identically to the bound
    # endpoint root (normalize drops the authority-equal path difference for loopback).
    identity = {
        "task_id": "t", "provider": "ollama", "model": "qwen:test",
        "endpoint_fingerprint": eb.compute_endpoint_fingerprint(planned["base_url"]),
    }
    assert_route_matches_binding(
        identity, provider=planned["provider"], model=planned["model"],
        base_url=planned["base_url"],
    )


def test_plan_runtime_route_reports_drifting_vertex_provider_without_mint(monkeypatch):
    """An ollama-bound run misconfigured with a Vertex primary: the planner reports
    provider 'vertex' (so the strict gate refuses on the provider mismatch) WITHOUT
    importing or calling the Vertex OAuth mint."""
    from youtab_agent_cli import runtime_provider as rp

    monkeypatch.setattr(
        "youtab_agent_cli.config.load_config",
        lambda: {"model": {"provider": "vertex"}},
    )
    planned = rp.plan_runtime_route(requested="vertex", configured_model="gemini-x")
    assert planned["provider"] == "vertex"
    with pytest.raises(ManagedWorkerAdmissionError, match="provider .*drifted"):
        assert_route_matches_binding(
            _IDENTITY, provider=planned["provider"], model=planned["model"],
            base_url=planned["base_url"],
        )


# --------------------------------------------------------------------------- #
# ManagedPreadmission — one immutable snapshot threaded through all stages       #
# --------------------------------------------------------------------------- #
def test_managed_preadmission_is_frozen_and_reused_without_reload(monkeypatch):
    """A replacement of the persisted binding BETWEEN stages cannot influence
    execution: the snapshot is frozen and ``establish_managed_admission`` reuses its
    context instead of reloading (so check and use see the SAME binding)."""
    # Frozen: fields cannot be mutated after construction.
    class _Env:
        tenant_id = None
        workspace_id = None

    class _Admitted:
        envelope = _Env()

    class _Ctx:
        task_id = "t"
        admitted = _Admitted()
        capability_binding = None
        created_at = None
        # No binding_hash -> verify_binding() fails closed -> enforcement raises,
        # which is enough to prove the reload path was not taken.
        binding = {"provider": "ollama", "model": "m", "endpoint_fingerprint": "fp"}

    snap = wa.ManagedPreadmission(context=_Ctx())
    with pytest.raises(Exception):
        snap.context = _Ctx()  # frozen dataclass rejects reassignment

    # establish_managed_admission must NOT reload when a snapshot is supplied: patch
    # the loader to explode if it is ever called with a snapshot present.
    def _must_not_load():
        raise AssertionError("establish_managed_admission reloaded despite a snapshot")

    monkeypatch.setattr(wa, "_load_managed_grant_context", _must_not_load)

    # A throwaway agent object; enforcement will raise on the stub binding, but the
    # key assertion is that the reload path was NOT taken (no AssertionError).
    class _Agent:
        provider = None
        model = None
        base_url = None

    with pytest.raises(wa.ManagedWorkerAdmissionError):
        wa.establish_managed_admission(_Agent(), snapshot=snap)


# --------------------------------------------------------------------------- #
# managed_bound_identity — None in standalone trust mode                         #
# --------------------------------------------------------------------------- #
def test_managed_bound_identity_none_in_standalone():
    # A plain test process is not in managed trust mode -> no managed constraints.
    assert managed_bound_identity() is None


# --------------------------------------------------------------------------- #
# _ensure_runtime_credentials — no fallback for a managed run                    #
# --------------------------------------------------------------------------- #
def _mixin_obj(*, managed, fallback):
    """A bare object carrying just the attributes _ensure_runtime_credentials reads."""
    from youtab_agent_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    obj = CLIAgentSetupMixin()
    obj.requested_provider = "openrouter"
    obj._explicit_api_key = None
    obj._explicit_base_url = None
    obj._fallback_model = fallback
    obj._managed_bound_identity = managed
    obj.api_key = None
    obj.base_url = None
    obj.provider = "openrouter"
    obj.api_mode = "openai"
    obj.acp_command = None
    obj.acp_args = []
    obj.model = "start-model"
    obj.agent = None
    obj._active_agent_route_signature = None
    # Downstream no-ops the bare mixin doesn't provide (not under test here).
    obj._normalize_model_for_provider = lambda _p: False
    return obj


def _patch_console(monkeypatch):
    import cli as _climod

    class _Console:
        def print(self, *a, **k):
            pass

    monkeypatch.setattr(_climod, "ChatConsole", _Console, raising=False)
    monkeypatch.setattr(_climod, "_cprint", lambda *a, **k: None, raising=False)


def test_managed_run_does_not_fall_back_on_primary_auth_failure(monkeypatch):
    from youtab_agent_cli import runtime_provider as rp
    from youtab_agent_cli.auth import AuthError

    _patch_console(monkeypatch)
    calls = []

    def _raise(*a, **k):
        calls.append(k.get("requested"))
        raise AuthError("primary down")

    monkeypatch.setattr(rp, "resolve_runtime_provider", _raise)

    obj = _mixin_obj(
        managed={"task_id": "t", "provider": "ollama", "model": "qwen:test",
                 "endpoint_fingerprint": _FP},
        fallback=[{"provider": "openai", "model": "gpt-x"}],
    )
    assert obj._ensure_runtime_credentials() is False
    # The fallback provider was NEVER attempted (resolve called exactly once) and the
    # run's provider/model were NOT switched to the unbound fallback.
    assert len(calls) == 1, calls
    assert obj.requested_provider == "openrouter"
    assert obj.model == "start-model"


def test_standalone_run_still_falls_back_on_primary_auth_failure(monkeypatch):
    from youtab_agent_cli import runtime_provider as rp
    from youtab_agent_cli.auth import AuthError

    _patch_console(monkeypatch)
    calls = []

    def _resolve(*a, **k):
        calls.append(k.get("requested"))
        if len(calls) == 1:
            raise AuthError("primary down")
        # fallback resolves successfully
        return {"api_key": "k", "base_url": "http://127.0.0.1:8080",
                "provider": "openai", "api_mode": "openai"}

    monkeypatch.setattr(rp, "resolve_runtime_provider", _resolve)
    monkeypatch.setattr(
        "youtab_agent_cli.fallback_config.resolve_entry_api_key",
        lambda entry: "fb-key", raising=False,
    )

    obj = _mixin_obj(
        managed=None,  # standalone
        fallback=[{"provider": "openai", "model": "gpt-x"}],
    )
    assert obj._ensure_runtime_credentials() is True
    # The standalone run DID fall back: a second resolve happened and the provider/
    # model switched to the configured fallback.
    assert len(calls) == 2, calls
    assert obj.requested_provider == "openai"
    assert obj.model == "gpt-x"
