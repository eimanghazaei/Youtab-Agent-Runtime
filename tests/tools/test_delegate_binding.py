"""WAVE-30H — a delegated MANAGED child inherits the parent's effective binding, or
records an explicit, parent-linked rebinding; provider/model drift fails closed and
a cost-escalating drift (free local parent -> billed cloud child) is refused.

Deterministic, offline. Mirrors the FakeAgent/_build_child_agent pattern in
tests/tools/test_delegate_kanban_isolation.py; the kanban write is stubbed so no DB
is touched and the recorded child-binding event is captured in-memory.
"""
from __future__ import annotations

import contextlib
import types

import pytest


def _binding(**over):
    base = {
        "binding_version": 1, "provider": "ollama", "model": "qwen:tag",
        "model_ref": "ollama/qwen:tag", "model_identifier_status": "resolved",
        "execution": "local", "endpoint_class": "loopback",
        "provider_cost_policy": "local_zero_verified",
        "digest_status": "not_probed", "model_digest": None,
        "bound_at": "2026-09-11T00:00:00Z",
    }
    base.update(over)
    return base


def _install(monkeypatch, *, parent_binding, recorded, managed=True):
    import run_agent
    from youtab_agent_cli import kanban_db as kb
    from youtab_runtime import execution_tree_budget as etb
    from tools import delegate_tool

    class FakeAgent:
        def __init__(self, **kwargs):
            self.valid_tool_names = {"terminal"}
            self.session_id = "child-session"

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(
        etb, "snapshot", lambda root: types.SimpleNamespace(max_spawn_depth=8)
    )

    # Stub the kanban write so the child-binding event is captured, not persisted.
    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(kb, "connect", lambda: _Conn())
    monkeypatch.setattr(kb, "write_txn", lambda conn: contextlib.nullcontext())
    monkeypatch.setattr(
        kb, "_append_event",
        lambda conn, run_id, kind, payload: recorded.append((run_id, kind, payload)),
    )

    env = types.SimpleNamespace(task_id="parent-run-1", root_run_id="parent-run-1")
    admitted = types.SimpleNamespace(envelope=env)

    class Parent:
        enabled_toolsets = ["terminal"]
        valid_tool_names = {"terminal"}
        model = "qwen:tag"
        provider = "ollama"
        base_url = "http://127.0.0.1:11434"
        api_mode = "chat_completions"
        platform = "cli"
        session_id = "parent-session"
        if managed:
            _execution_tree_root = "parent-run-1"
            _admitted_command = admitted
            _execution_tree_limits = None
            _runtime_effective_binding = parent_binding

    return delegate_tool, Parent()


def _build(delegate_tool, parent, **over):
    kwargs = dict(
        task_index=0, goal="work", context=None, toolsets=None, model=None,
        max_iterations=3, task_count=1, parent_agent=parent,
    )
    kwargs.update(over)
    return delegate_tool._build_child_agent(**kwargs)


def test_child_inherits_parent_binding_by_default(monkeypatch):
    recorded = []
    dt, parent = _install(monkeypatch, parent_binding=_binding(), recorded=recorded)
    child = _build(dt, parent)  # no override -> child runs on parent's substrate
    assert child._runtime_effective_binding is parent._runtime_effective_binding
    assert recorded == []  # inheritance records nothing new


def test_cost_escalating_drift_fails_closed(monkeypatch):
    # A free local-zero parent must never spawn a billed cloud child silently.
    recorded = []
    dt, parent = _install(monkeypatch, parent_binding=_binding(), recorded=recorded)
    with pytest.raises(ValueError, match="local-zero parent"):
        _build(dt, parent, model="gpt-x", override_provider="openai",
               override_base_url="https://api.openai.com")
    assert recorded == []  # refused before any rebinding was recorded


def test_benign_cloud_rebind_is_recorded_and_linked(monkeypatch):
    # A cloud parent spawning a different cloud model records an explicit,
    # parent-linked child binding (non-silent) and attaches it to the child.
    recorded = []
    parent_binding = _binding(
        provider="openai", model="gpt-a", model_ref="openai/gpt-a",
        execution="cloud", endpoint_class="cloud",
        provider_cost_policy="campaign_budget_eur", digest_status="not_applicable",
    )
    dt, parent = _install(monkeypatch, parent_binding=parent_binding, recorded=recorded)
    child = _build(dt, parent, model="gpt-b", override_provider="openai",
                   override_base_url="https://api.openai.com")
    assert len(recorded) == 1
    run_id, kind, payload = recorded[0]
    assert run_id == "parent-run-1"
    assert kind == "runtime_child_effective_binding"
    assert payload["provider"] == "openai" and payload["model"] == "gpt-b"
    assert payload["parent_run_id"] == "parent-run-1"
    assert payload["parent_binding_version"] == 1
    assert payload["subagent_id"]
    assert child._runtime_effective_binding["model"] == "gpt-b"


def test_standalone_child_has_no_binding(monkeypatch):
    # A non-managed parent (no tree root / no binding) is untouched.
    recorded = []
    dt, parent = _install(
        monkeypatch, parent_binding=None, recorded=recorded, managed=False
    )
    child = _build(dt, parent)
    assert getattr(child, "_runtime_effective_binding", None) is None
    assert recorded == []
