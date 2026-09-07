"""End-to-end managed-authority chain (WAVE-30H R3, item 7).

Exercises the REAL functions along create_run -> persist -> worker re-admit ->
tool_executor authority gate, at the function level (no subprocess), plus the
required negative controls: forged, expired, replayed, cross-tenant and
missing-grant. The full subprocess E2E (FastAPI create_run -> dispatcher spawn ->
worker) additionally runs in CI where the web/httpx stack is present.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.tool_executor import enforce_managed_tool_authority
from tools.registry import ToolSpec, registry
from youtab_runtime.contracts import BrainCommandEnvelopeV2
from youtab_runtime.managed_execution import (
    AdmissionIdentity,
    ManagedAdmissionError,
    admit_managed_run,
    re_admit_worker_grant,
)
from youtab_runtime.policy import AuthorityBoundary

KEY_ID = "brain-ed25519-e2e"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([11]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS = {KEY_ID: _PUB}
_NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _grant_header(*, signer=_SIGNER, toolsets=("*",), mem_scopes=(), nonce="grant-e2e-0123456789abcdef0123", **over):
    fields = dict(
        schema_version="youtab.agent-command.v2",
        issuer="youtab-one-brain",
        audience="youtab-agent-runtime",
        protocol_version="youtab.runtime-sig.v2",
        command_id="cmd-e2e0123456", task_id="task-e2e012345",
        root_run_id="run-e2e0123456", parent_task_id=None, attempt=1,
        tenant_id="tenant-alpha", workspace_id="-", user_id="user-eiman",
        membership_generation=1, authorization_epoch=1,
        agent_id="agent-default", engine_id="engine-local",
        trace_id="trace-e2e0123", nonce=nonce, objective="do the thing",
        allowed_toolsets=toolsets, allowed_memory_scopes=mem_scopes,
        allowed_artifact_scopes=(), effect_proposal_scopes=(),
        reasoning={"max_iterations": 5, "max_spawn_depth": 1, "max_concurrent_agents": 1,
                   "max_total_tokens": 1000, "max_cost_micros": 0, "max_retries": 0,
                   "deadline_at": _NOW + timedelta(minutes=20)},
        issued_at=_NOW, expires_at=_NOW + timedelta(minutes=30),
        key_id=KEY_ID, signature="0" * 88,
    )
    fields.update(over)
    env = BrainCommandEnvelopeV2(**fields)
    sig = base64.b64encode(signer.sign(env.canonical_payload())).decode()
    grant = env.model_dump(mode="json")
    grant["signature"] = sig
    return base64.b64encode(json.dumps(grant, separators=(",", ":")).encode()).decode()


class _Agent:
    """Minimal stand-in exposing only what the gate reads."""

    def __init__(self, admitted=None):
        self._admitted_command = admitted


# --- tool registration (global registry, cleaned up) ----------------------

@pytest.fixture
def registered_tools():
    names = ("e2e_read_tool", "e2e_write_tool", "e2e_forbidden_tool")
    registry.register_spec(ToolSpec(
        name="e2e_read_tool", toolset="reader", schema={"name": "e2e_read_tool"},
        handler=lambda a, **k: "ok", side_effect_class="read"))
    registry.register_spec(ToolSpec(
        name="e2e_write_tool", toolset="writer", schema={"name": "e2e_write_tool"},
        handler=lambda a, **k: "ok", side_effect_class="write"))
    registry.register_spec(ToolSpec(
        name="e2e_forbidden_tool", toolset="cognitive_authority",
        schema={"name": "e2e_forbidden_tool"}, handler=lambda a, **k: "ok",
        side_effect_class="read"))
    registry.register_spec(ToolSpec(
        name="e2e_memory_tool", toolset="memory", schema={"name": "e2e_memory_tool"},
        handler=lambda a, **k: "ok", side_effect_class="memory_write"))
    names = names + ("e2e_memory_tool",)
    try:
        yield names
    finally:
        for n in names:
            try:
                registry.deregister(n)
            except Exception:
                pass


def _admit_worker():
    return re_admit_worker_grant(
        grant_header=_grant_header(), boundary=AuthorityBoundary(),
        public_keys=_KEYS, now=_NOW)


# --- the happy chain ------------------------------------------------------

def test_full_chain_authorized_read_tool_runs(monkeypatch, registered_tools):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    agent = _Agent(_admit_worker())  # worker re-admitted the persisted grant
    # read tool within the "*" envelope -> gate allows (returns None)
    assert enforce_managed_tool_authority(agent, "e2e_read_tool", {}) is None


def test_effectful_tool_is_refused_execution(monkeypatch, registered_tools):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    agent = _Agent(_admit_worker())
    reason = enforce_managed_tool_authority(agent, "e2e_write_tool", {"x": 1})
    assert reason and "effect" in reason.lower()


def test_forbidden_toolset_hard_denied_under_full_envelope(monkeypatch, registered_tools):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    agent = _Agent(_admit_worker())
    reason = enforce_managed_tool_authority(agent, "e2e_forbidden_tool", {})
    assert reason and "authority-bearing" in reason.lower()


# --- R4: memory-scope enforcement at the gate -----------------------------

def test_memory_write_allowed_within_authorized_scope(monkeypatch, registered_tools):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    admitted = re_admit_worker_grant(
        grant_header=_grant_header(mem_scopes=("*",)),
        boundary=AuthorityBoundary(), public_keys=_KEYS, now=_NOW)
    agent = _Agent(admitted)
    assert enforce_managed_tool_authority(agent, "e2e_memory_tool", {"action": "add"}) is None


def test_memory_write_denied_when_grant_authorizes_no_memory(monkeypatch, registered_tools):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    admitted = re_admit_worker_grant(
        grant_header=_grant_header(mem_scopes=()),  # grant authorizes NO memory
        boundary=AuthorityBoundary(), public_keys=_KEYS, now=_NOW)
    agent = _Agent(admitted)
    reason = enforce_managed_tool_authority(agent, "e2e_memory_tool", {"action": "add"})
    assert reason and "memory" in reason.lower()


# --- item 6: no bypass, no standalone fallback ----------------------------

def test_managed_without_admitted_context_fails_closed(monkeypatch, registered_tools):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    agent = _Agent(admitted=None)  # worker never established a context
    reason = enforce_managed_tool_authority(agent, "e2e_read_tool", {})
    assert reason and "without a Simorgh execution grant" in reason


def test_standalone_gate_is_inert(monkeypatch, registered_tools):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "local-standalone")
    agent = _Agent(admitted=None)
    assert enforce_managed_tool_authority(agent, "e2e_write_tool", {}) is None


# --- negative controls on the grant itself --------------------------------

def test_forged_grant_rejected_at_worker(monkeypatch):
    forged = _grant_header(signer=Ed25519PrivateKey.from_private_bytes(bytes([99]) * 32))
    with pytest.raises(ManagedAdmissionError) as ei:
        re_admit_worker_grant(grant_header=forged, boundary=AuthorityBoundary(),
                              public_keys=_KEYS, now=_NOW)
    assert ei.value.code == "grant_rejected"


def test_expired_grant_rejected_at_worker():
    with pytest.raises(ManagedAdmissionError) as ei:
        re_admit_worker_grant(grant_header=_grant_header(), boundary=AuthorityBoundary(),
                              public_keys=_KEYS, now=_NOW + timedelta(hours=1))
    assert ei.value.code == "grant_rejected"


def test_replayed_grant_rejected_at_ingress():
    boundary = AuthorityBoundary()
    header = _grant_header()
    ident = AdmissionIdentity(tenant="tenant-alpha", user="user-eiman", workspace="-")
    admit_managed_run(grant_header=header, identity=ident, boundary=boundary,
                      public_keys=_KEYS, now=_NOW)
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=header, identity=ident, boundary=boundary,
                          public_keys=_KEYS, now=_NOW)
    assert ei.value.code == "grant_rejected"


def test_cross_tenant_grant_rejected_at_ingress():
    ident = AdmissionIdentity(tenant="tenant-OTHER", user="user-eiman", workspace="-")
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=_grant_header(), identity=ident,
                          boundary=AuthorityBoundary(), public_keys=_KEYS, now=_NOW)
    assert ei.value.code == "grant_identity_mismatch"


def test_missing_grant_rejected_at_ingress():
    ident = AdmissionIdentity(tenant="tenant-alpha", user="user-eiman", workspace="-")
    with pytest.raises(ManagedAdmissionError) as ei:
        admit_managed_run(grant_header=None, identity=ident,
                          boundary=AuthorityBoundary(), public_keys=_KEYS, now=_NOW)
    assert ei.value.code == "grant_required"
