"""WAVE-30H — a delegated MANAGED child inherits the EXACT parent effective binding
(substrate-digest verified, endpoint included), or FAILS CLOSED.

A tampered parent binding, or ANY provider/model/endpoint drift, refuses the child
(a signed single-use rebind authorization — the only sanctioned way to change a
child's substrate — is a separate primitive; until it lands, drift is refused, never
silently recorded-and-run). Deterministic, offline; mirrors the FakeAgent pattern in
tests/tools/test_delegate_kanban_isolation.py.
"""
from __future__ import annotations

import types

import pytest

from youtab_agent_cli import effective_binding as eb


def _binding(*, provider="ollama", model="qwen:tag", endpoint="http://127.0.0.1:11434"):
    # Built via the real builder so it carries a valid binding_hash/fingerprint.
    return eb.build_effective_binding(
        provider=provider, model=model, endpoint=endpoint,
        run_id="parent-run-1", root_run_id="parent-run-1", tenant="t1",
    )


def _install(monkeypatch, *, parent_binding, managed=True,
             parent_provider="ollama", parent_model="qwen:tag",
             parent_base_url="http://127.0.0.1:11434"):
    import run_agent
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

    env = types.SimpleNamespace(task_id="parent-run-1", root_run_id="parent-run-1")
    admitted = types.SimpleNamespace(envelope=env)

    class Parent:
        enabled_toolsets = ["terminal"]
        valid_tool_names = {"terminal"}
        model = parent_model
        provider = parent_provider
        base_url = parent_base_url
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


def test_child_inherits_exact_parent_binding(monkeypatch):
    dt, parent = _install(monkeypatch, parent_binding=_binding())
    child = _build(dt, parent)  # no override -> identical substrate
    assert child._runtime_effective_binding is parent._runtime_effective_binding


def test_provider_model_drift_fails_closed(monkeypatch):
    dt, parent = _install(monkeypatch, parent_binding=_binding())
    with pytest.raises(ValueError, match="substrate drift"):
        _build(dt, parent, model="gpt-x", override_provider="openai",
               override_base_url="https://api.openai.com")


def test_cost_escalating_drift_fails_closed(monkeypatch):
    # A local-zero parent spawning a cloud child is a substrate drift → refused.
    dt, parent = _install(monkeypatch, parent_binding=_binding())
    with pytest.raises(ValueError, match="substrate drift"):
        _build(dt, parent, model="gpt-x", override_provider="openai",
               override_base_url="https://api.openai.com")


def test_endpoint_only_drift_fails_closed(monkeypatch):
    # Same provider+model but a DIFFERENT endpoint must NOT silently inherit
    # (the latent bug a bare provider+model check missed). Substrate digest differs.
    dt, parent = _install(monkeypatch, parent_binding=_binding())
    with pytest.raises(ValueError, match="substrate drift"):
        _build(dt, parent, override_base_url="http://127.0.0.1:11435")


def test_tampered_parent_binding_fails_closed(monkeypatch):
    b = _binding()
    b["model"] = "evil-swap"  # breaks the binding_hash
    dt, parent = _install(monkeypatch, parent_binding=b)
    with pytest.raises(ValueError, match="integrity failure"):
        _build(dt, parent)


def test_standalone_child_has_no_binding(monkeypatch):
    dt, parent = _install(monkeypatch, parent_binding=None, managed=False)
    child = _build(dt, parent)
    assert getattr(child, "_runtime_effective_binding", None) is None
