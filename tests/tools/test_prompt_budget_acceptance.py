"""WAVE-30E — acceptance: the task-scoped lean loadout brings the kanban-worker
tool schema into the simple-task budget (4K–8K) WITHOUT removing capability.

This ties together the budget module + the lean loadout + the real tool registry
as the offline before/after acceptance evidence the Owner requires. It asserts a
DIRECTION and a BUDGET, not an environment-specific absolute, so it is stable
across boxes (the canary box lacked httpx browser tools; CI may differ).
"""
from __future__ import annotations

import os

import pytest

from youtab_runtime.context_budget import budget_for, estimate_schema_tokens, within_budget
from tools import tool_search as ts


def _name(td):
    return (td.get("function") or {}).get("name", "?")


@pytest.fixture(autouse=True)
def _kanban_env(monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", "t_probe")
    ts.reset_loadout_policy_cache()
    yield
    ts.reset_loadout_policy_cache()


def test_lean_loadout_hits_simple_budget_and_preserves_discovery():
    from model_tools import get_tool_definitions

    defs = get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True) or []
    _, baseline_tokens = estimate_schema_tokens(defs)
    if baseline_tokens <= budget_for("simple").target_max:
        pytest.skip(f"environment tool catalog already small ({baseline_tokens} est tokens)")

    always_on = [n for n in (_name(d) for d in defs) if n.startswith("kanban_")]
    assert "kanban_show" in always_on, "kanban worker must have its primary tools"

    cfg = ts.ToolSearchConfig.from_raw(
        {"enabled": "on", "defer_core": True, "always_on": always_on, "listing": "off"}
    )
    result = ts.assemble_tool_defs(defs, context_length=8000, config=cfg)
    _, lean_tokens = estimate_schema_tokens(result.tool_defs)

    # 1) Material reduction into the simple-task target (≤ 8K).
    assert lean_tokens < baseline_tokens
    assert within_budget("simple", lean_tokens), f"lean tool schema {lean_tokens} > 8000"

    # 2) The task's primary tools stayed eager (no discovery round-trip needed).
    eager_names = {_name(d) for d in result.tool_defs}
    assert "kanban_show" in eager_names

    # 3) Capability preserved: the deferred tail is still fully discoverable +
    #    reachable via the bridge (nothing removed).
    _, deferrable = ts.classify_tools(defs, config=cfg)
    deferred_names = {_name(d) for d in deferrable}
    assert deferred_names, "expected a deferred tail"
    # a representative non-kanban core tool is deferred, not deleted
    assert any(n in deferred_names for n in ("memory", "session_search", "read_file"))
    ts._CACHED_LOADOUT_CONFIG = cfg  # dispatch consults the cached policy
    scoped = ts.scoped_deferrable_names(defs)
    # STRICT invariant: EVERY deferred tool must be reachable via tool_call, or a
    # discovered tool would be a dead end (silent capability loss). No soft OR.
    assert deferred_names.issubset(scoped), deferred_names - scoped

    # 4) 64K context capability is untouched by the prompt-token reduction.
    assert budget_for("simple").ctx_capability == 64_000
