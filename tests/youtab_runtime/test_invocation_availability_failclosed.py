"""WAVE-30H Phase-C / C9 — the 60s anti-flap grace FAILS CLOSED at invocation.

The discovery path keeps a tool listed for up to ``_CHECK_FN_FAILURE_GRACE_SECONDS``
after a transient ``check_fn`` failure (anti-flap, so a flaky probe does not strip
a tool mid-session). That grace must smooth DISCOVERY only — it must never let a
tool whose dependency is ACTUALLY gone be EXECUTED. This proves:

  1. within the grace window, discovery (_check_fn_cached) still reports available;
  2. the strict invocation probe (available_strict) reports UNAVAILABLE;
  3. the managed invocation gate FAILS CLOSED for that tool; and
  4. it is capability-preserving — an available tool is NOT false-blocked.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.tool_executor import enforce_managed_tool_authority
from tools import registry as regmod
from tools.registry import ToolSpec, registry
from youtab_agent_cli import capability_manifest as cm
from youtab_runtime.contracts import BrainCommandEnvelopeV2
from youtab_runtime.managed_execution import AdmissionIdentity, admit_managed_run
from youtab_runtime.policy import AuthorityBoundary

KEY_ID = "brain-c9"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([61]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()


def _spec(name, check_fn):
    return ToolSpec(name=name, toolset="reader", schema={"name": name},
                    handler=lambda a, **k: "{}", side_effect_class="read",
                    provenance="test", version="1.0.0", check_fn=check_fn)


def _grant_header(now):
    env = BrainCommandEnvelopeV2(
        schema_version="youtab.agent-command.v2", issuer="youtab-one-brain",
        audience="youtab-agent-runtime", protocol_version="youtab.runtime-sig.v2",
        command_id="cmd-c9-0001", task_id="task-c9-0001", root_run_id="run-c9-0001",
        parent_task_id=None, attempt=1, tenant_id="tenant-a", workspace_id="-",
        user_id="user-a", membership_generation=1, authorization_epoch=1,
        agent_id="agent-default", engine_id="engine-local", trace_id="trace-c9",
        nonce="grant-c9-0123456789abcdef0123456789ab", objective="probe",
        allowed_toolsets=("*",), allowed_memory_scopes=("*",),
        allowed_artifact_scopes=(), effect_proposal_scopes=(),
        reasoning={"max_iterations": 5, "max_spawn_depth": 1, "max_concurrent_agents": 1,
                   "max_total_tokens": 1000, "max_cost_micros": 0, "max_retries": 0,
                   "deadline_at": now + timedelta(minutes=20)},
        issued_at=now, expires_at=now + timedelta(minutes=30),
        key_id=KEY_ID, signature="0" * 88)
    sig = base64.b64encode(_SIGNER.sign(env.canonical_payload())).decode()
    g = env.model_dump(mode="json"); g["signature"] = sig
    return env, base64.b64encode(json.dumps(g, separators=(",", ":")).encode()).decode()


class _Agent:
    _admitted_command = None


@pytest.fixture()
def managed_agent(monkeypatch):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    now = datetime.now(UTC).replace(microsecond=0)
    flag = {"ok": True}
    check = lambda: flag["ok"]  # noqa: E731
    # register the flaky tool in the GLOBAL registry (what the gate reads) while
    # it is available, so the frozen manifest authorizes it.
    registry.register_spec(_spec("flaky_read", check))
    env, header = _grant_header(now)
    binding = cm.build_ingress_binding(env, registry=registry)
    admitted = admit_managed_run(
        grant_header=header,
        identity=AdmissionIdentity(tenant="tenant-a", user="user-a", workspace="-"),
        boundary=AuthorityBoundary(), public_keys={KEY_ID: _PUB}, now=now,
        capability_binding=binding)
    agent = _Agent()
    agent._admitted_command = admitted
    regmod.invalidate_check_fn_cache()
    return agent, flag, check


def test_grace_keeps_discoverable_but_invocation_fails_closed(managed_agent):
    agent, flag, check = managed_agent
    # authorized + available now -> the gate ALLOWS (None). This proves the only
    # thing that can block later is the availability gate, not authorization.
    assert enforce_managed_tool_authority(agent, "flaky_read", {}) is None

    # prime discovery success, then the dependency vanishes.
    assert regmod._check_fn_cached(check) is True
    flag["ok"] = False

    # 1) discovery still reports available (anti-flap grace active)...
    assert regmod._check_fn_cached(check) is True
    # 2) ...but the strict invocation probe sees the truth.
    assert registry.available_strict("flaky_read") is False
    # 3) the managed invocation gate FAILS CLOSED.
    reason = enforce_managed_tool_authority(agent, "flaky_read", {})
    assert reason is not None and "not operational at invocation" in reason


def test_available_tool_is_not_false_blocked(managed_agent):
    agent, flag, check = managed_agent
    # capability-preserving: while genuinely available, the gate never blocks on
    # availability (authorization still governs effect-bearing tools elsewhere).
    assert flag["ok"] is True
    assert registry.available_strict("flaky_read") is True
    assert enforce_managed_tool_authority(agent, "flaky_read", {}) is None


def test_tool_without_check_fn_is_always_invocation_available():
    registry.register_spec(ToolSpec(name="no_check_read", toolset="reader",
                                    schema={"name": "no_check_read"},
                                    handler=lambda a, **k: "{}", side_effect_class="read",
                                    provenance="test", version="1.0.0"))
    assert registry.available_strict("no_check_read") is True
    assert registry.available_strict("does_not_exist") is False
