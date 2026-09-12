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
# authoritative_provider — local alias canonicalization (Batch5 #3)             #
# --------------------------------------------------------------------------- #
def test_authoritative_provider_canonicalizes_local_alias():
    # ollama is dispatched via the custom transport, but its authoritative provider
    # (and its binding) stays ollama.
    assert wa.authoritative_provider("custom", "ollama") == "ollama"
    assert wa.authoritative_provider("custom", "vllm") == "vllm"


def test_authoritative_provider_passes_through_builtins_and_bare_custom():
    assert wa.authoritative_provider("anthropic", "anthropic") == "anthropic"
    assert wa.authoritative_provider("youtab", "youtab") == "youtab"
    assert wa.authoritative_provider("custom", "custom") == "custom"
    assert wa.authoritative_provider("openrouter", "auto") == "openrouter"


# --------------------------------------------------------------------------- #
# assert_bound_provider_admissible — PRE-CREDENTIAL provider gate (Batch5 #2)    #
# --------------------------------------------------------------------------- #
def test_bound_provider_admissible_accepts_concrete_match():
    wa.assert_bound_provider_admissible(_IDENTITY, requested_provider="ollama")


def test_bound_provider_admissible_noop_for_standalone():
    wa.assert_bound_provider_admissible(None, requested_provider="anything")


def test_bound_provider_admissible_refuses_auto_binding():
    with pytest.raises(ManagedWorkerAdmissionError, match="binding provider is not concrete"):
        wa.assert_bound_provider_admissible(
            {"task_id": "t", "provider": "auto", "model": "m", "endpoint_fingerprint": _FP},
            requested_provider="auto",
        )


def test_bound_provider_admissible_refuses_auto_requested():
    with pytest.raises(ManagedWorkerAdmissionError, match="requested provider is unresolved"):
        wa.assert_bound_provider_admissible(_IDENTITY, requested_provider="auto")


def test_bound_provider_admissible_refuses_provider_drift():
    # ollama-bound run whose requested provider is a drifting cloud primary -> refused
    # BEFORE any credential resolution (this is what stops the vertex/youtab mint).
    with pytest.raises(ManagedWorkerAdmissionError, match="drifted from the bound identity"):
        wa.assert_bound_provider_admissible(_IDENTITY, requested_provider="vertex")


# --------------------------------------------------------------------------- #
# Batch5 #4: a built-in endpoint override (Anthropic on Azure) resolves to the  #
# configured URL and matches its binding — NOT substituted with a registry      #
# default. Uses the REAL resolver (explicit base_url+key => no pool/mint/net).   #
# --------------------------------------------------------------------------- #
def test_builtin_anthropic_azure_override_resolves_and_matches_binding(monkeypatch):
    from youtab_agent_cli import runtime_provider as rp

    azure = "https://my-azure.openai.azure.com"
    monkeypatch.setenv("AZURE_ANTHROPIC_KEY", "azure-not-a-real-key")
    resolved = rp.resolve_runtime_provider(
        requested="anthropic", explicit_base_url=azure, explicit_api_key="azure-not-a-real-key",
    )
    assert resolved["provider"] == "anthropic"
    assert resolved["base_url"] == azure  # configured URL, NOT a registry default
    # A binding produced by create for this built-in engine identity matches.
    binding = eb.build_effective_binding(provider="anthropic", model="claude-x", endpoint=azure)
    identity = {
        "task_id": "t", "provider": "anthropic", "model": "claude-x",
        "endpoint_fingerprint": binding["endpoint_fingerprint"],
    }
    assert_route_matches_binding(
        identity,
        provider=wa.authoritative_provider(resolved["provider"], "anthropic"),
        model="claude-x", base_url=resolved["base_url"],
    )


# --------------------------------------------------------------------------- #
# Batch5 #1: grant expiry re-validated at final admission (before authority)    #
# --------------------------------------------------------------------------- #
def test_establish_refuses_grant_expired_during_startup():
    """A snapshot whose grant expired after pre-admission must be refused by
    establish_managed_admission BEFORE the AdmittedCommand is attached as authority."""
    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(minutes=5)

    class _Env:
        tenant_id = None
        workspace_id = None
        expires_at = past  # already elapsed
        reasoning = None

    class _Admitted:
        envelope = _Env()

    class _Ctx:
        task_id = "t"
        admitted = _Admitted()
        capability_binding = None
        created_at = None
        binding = {"provider": "ollama", "model": "m", "endpoint_fingerprint": "fp"}

    class _Agent:
        provider = None
        model = None
        base_url = None
        _admitted_command = None

    agent = _Agent()
    snap = wa.ManagedPreadmission(context=_Ctx())
    with pytest.raises(ManagedWorkerAdmissionError, match="expired during startup"):
        wa.establish_managed_admission(agent, snapshot=snap)
    # Authority was NOT attached before the expiry refusal.
    assert agent._admitted_command is None


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
