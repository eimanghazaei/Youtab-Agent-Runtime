"""WAVE-30H — worker pre-dispatch binding enforcement (unit, offline, deterministic).

``_enforce_effective_binding`` runs inside ``establish_managed_admission`` and
returns BEFORE the agent loop constructs any provider client or debits any budget.
A raise here therefore PROVES zero provider calls and zero budget consumption for
the refused run — no client is ever built, no tree/campaign debit occurs. These
tests exercise every fail-closed condition and the audited legacy quarantine.
"""
from __future__ import annotations

import types

import pytest

from youtab_agent_cli import effective_binding as eb
from youtab_agent_cli.worker_admission import (
    ManagedWorkerAdmissionError,
    _enforce_effective_binding,
    _legacy_binding_quarantine_active,
)


def _env(tenant="t1", workspace="w1"):
    return types.SimpleNamespace(
        tenant_id=tenant, workspace_id=workspace, task_id="run-1", root_run_id="run-1"
    )


def _agent(provider=None, model=None, base_url=None):
    a = types.SimpleNamespace(provider=provider, model=model)
    if base_url is not None:
        a.base_url = base_url
    return a


def _binding(provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434",
             tenant="t1"):
    return eb.build_effective_binding(
        provider=provider, model=model, endpoint=endpoint,
        run_id="run-1", root_run_id="run-1", tenant=tenant,
    )


# ── presence / integrity / tenant (enforced for EVERY managed run) ───────────

def test_missing_binding_fails_closed(monkeypatch):
    monkeypatch.delenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", raising=False)
    with pytest.raises(ManagedWorkerAdmissionError, match="no effective binding"):
        _enforce_effective_binding(_agent(), _env(), None, "run-1", require_client_match=False)


def test_corrupt_binding_fails_closed():
    with pytest.raises(ManagedWorkerAdmissionError, match="corrupt"):
        _enforce_effective_binding(
            _agent(), _env(), {"__corrupt__": True}, "run-1", require_client_match=False)


def test_tampered_hash_fails_closed():
    b = _binding()
    b["model"] = "evil-swap"  # breaks the self-hash
    with pytest.raises(ManagedWorkerAdmissionError, match="hash mismatch"):
        _enforce_effective_binding(_agent(), _env(), b, "run-1", require_client_match=False)


def test_cross_tenant_fails_closed():
    b = _binding(tenant="t1")
    with pytest.raises(ManagedWorkerAdmissionError, match="tenant mismatch"):
        _enforce_effective_binding(
            _agent(), _env(tenant="t2"), b, "run-1", require_client_match=False)


def test_valid_binding_stub_worker_passes():
    # Deterministic/stub worker (no provider client) — presence/integrity/tenant only.
    _enforce_effective_binding(_agent(), _env(), _binding(), "run-1", require_client_match=False)


# ── resolved + drift (enforced only for a real-model worker) ─────────────────

def test_unresolved_binding_fails_closed_for_real_worker():
    b = eb.build_effective_binding(provider="openai", model=None, run_id="run-1", tenant="t1")
    with pytest.raises(ManagedWorkerAdmissionError, match="not fully resolved"):
        _enforce_effective_binding(
            _agent(provider="openai", model="gpt-x"), _env(), b, "run-1",
            require_client_match=True)


def test_provider_drift_fails_closed():
    b = _binding(provider="ollama", model="qwen:tag")
    with pytest.raises(ManagedWorkerAdmissionError, match="provider drifted"):
        _enforce_effective_binding(
            _agent(provider="openai", model="qwen:tag"), _env(), b, "run-1",
            require_client_match=True)


def test_model_drift_fails_closed():
    b = _binding(provider="ollama", model="qwen:tag")
    with pytest.raises(ManagedWorkerAdmissionError, match="model drifted"):
        _enforce_effective_binding(
            _agent(provider="ollama", model="other-model"), _env(), b, "run-1",
            require_client_match=True)


def test_endpoint_drift_fails_closed():
    b = _binding(provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434")
    a = _agent(provider="ollama", model="qwen:tag", base_url="http://127.0.0.1:11435")
    with pytest.raises(ManagedWorkerAdmissionError, match="endpoint drifted"):
        _enforce_effective_binding(a, _env(), b, "run-1", require_client_match=True)


def test_matching_real_worker_passes():
    b = _binding(provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434")
    a = _agent(provider="ollama", model="qwen:tag", base_url="http://127.0.0.1:11434")
    _enforce_effective_binding(a, _env(), b, "run-1", require_client_match=True)  # no raise


# ── legacy quarantine: fail-closed by default, bounded, audited ──────────────

def test_legacy_quarantine_allows_missing_when_future_dated(monkeypatch):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2099-01-01")
    import youtab_agent_cli.worker_admission as wa
    audited = {}
    monkeypatch.setattr(
        wa, "_audit_binding_quarantine",
        lambda task_id, reason: audited.update({"task_id": task_id, "reason": reason}),
    )
    _enforce_effective_binding(_agent(), _env(), None, "run-1", require_client_match=False)
    assert audited == {"task_id": "run-1", "reason": "missing_binding"}


def test_legacy_quarantine_expired_fails_closed(monkeypatch):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", "2000-01-01")
    with pytest.raises(ManagedWorkerAdmissionError, match="no effective binding"):
        _enforce_effective_binding(_agent(), _env(), None, "run-1", require_client_match=False)


@pytest.mark.parametrize("val", ["forever", "not-a-date", ""])
def test_quarantine_nondate_fails_closed(monkeypatch, val):
    monkeypatch.setenv("YOUTAB_MANAGED_BINDING_LEGACY_UNTIL", val)
    assert _legacy_binding_quarantine_active() is False
