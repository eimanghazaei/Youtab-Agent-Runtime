"""WAVE-30E — task-scoped lean tool loadout (defer_core + always_on).

Proves the prompt-shrinking loadout is capability-preserving: the task's primary
tools stay eager, the long tail is deferred behind the existing tool_search
bridge, and every deferred CORE tool remains discoverable (tool_search) and
callable (tool_call) — nothing is removed. Default config (defer_core off) is
byte-for-byte the prior behaviour.
"""
from __future__ import annotations

import pytest

from tools import tool_search as ts


def _td(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


# A representative core loadout (names drawn from _YOUTAB_AGENT_CORE_TOOLS).
CORE_NAMES = [
    "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
    "memory", "session_search", "delegate_task", "terminal", "skill_manage",
    "read_file", "write_file",
]


@pytest.fixture(autouse=True)
def _reset_cache():
    ts.reset_loadout_policy_cache()
    yield
    ts.reset_loadout_policy_cache()


def test_default_config_never_defers_core():
    """defer_core OFF (default) → core tools stay eager, exactly as before."""
    cfg = ts.ToolSearchConfig.from_raw({"enabled": "auto"})
    assert cfg.defer_core is False
    for name in CORE_NAMES:
        assert ts.is_deferrable_tool_name(name, config=cfg) is False
    visible, deferrable = ts.classify_tools([_td(n) for n in CORE_NAMES], config=cfg)
    assert deferrable == []
    assert {(_v["function"]["name"]) for _v in visible} == set(CORE_NAMES)


def test_lean_loadout_keeps_primary_eager_defers_the_rest():
    """defer_core ON + always_on=kanban → only kanban tools stay eager."""
    cfg = ts.ToolSearchConfig.from_raw({
        "enabled": "on",
        "defer_core": True,
        "always_on": ["kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat"],
    })
    visible, deferrable = ts.classify_tools([_td(n) for n in CORE_NAMES], config=cfg)
    vnames = {v["function"]["name"] for v in visible}
    dnames = {d["function"]["name"] for d in deferrable}
    # Primary (always_on) stays eager; everything else is deferred.
    assert vnames == {"kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat"}
    assert dnames == {"memory", "session_search", "delegate_task", "terminal",
                      "skill_manage", "read_file", "write_file"}


def test_deferred_core_tool_is_replaced_by_bridge_but_discoverable():
    """Assembly collapses the deferred tail into the 3 bridge tools; the always_on
    set stays. The deferred tools remain in the searchable catalog."""
    cfg = ts.ToolSearchConfig.from_raw({
        "enabled": "on", "defer_core": True,
        "always_on": ["kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat"],
        "listing": "off",  # force bare bridge so the assertion is about reachability
    })
    defs = [_td(n) for n in CORE_NAMES]
    result = ts.assemble_tool_defs(defs, context_length=8000, config=cfg)
    assert result.activated is True
    names = {(d.get("function") or {}).get("name") for d in result.tool_defs}
    # Eager = 4 kanban + 3 bridges.
    assert "kanban_show" in names
    assert ts.TOOL_SEARCH_NAME in names and ts.TOOL_CALL_NAME in names and ts.TOOL_DESCRIBE_NAME in names
    # The deferred core tool is NOT eager...
    assert "session_search" not in names
    # ...but it IS discoverable: it appears in the catalog built from the full defs.
    catalog = ts.build_catalog([d for d in defs if d["function"]["name"] not in
                                {"kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat"}])
    catalog_names = {e.name for e in catalog}
    assert "session_search" in catalog_names and "memory" in catalog_names


def test_deferred_core_tool_passes_the_scoped_call_gate(monkeypatch):
    """The dispatch-side gate (scoped_deferrable_names) must classify a deferred
    CORE tool as reachable so tool_call can invoke it — else discovery would be a
    dead end. Uses the cached policy (what dispatch actually consults)."""
    cfg = ts.ToolSearchConfig.from_raw({
        "enabled": "on", "defer_core": True, "always_on": ["kanban_show"],
    })
    monkeypatch.setattr(ts, "_CACHED_LOADOUT_CONFIG", cfg)
    full_defs = [_td(n) for n in CORE_NAMES]  # the un-collapsed session catalog
    scoped = ts.scoped_deferrable_names(full_defs)
    assert "session_search" in scoped  # a deferred core tool is reachable via tool_call
    assert "memory" in scoped
    assert "kanban_show" not in scoped  # the always-on primary is eager, not via bridge


def test_always_on_and_bridges_are_never_deferred():
    cfg = ts.ToolSearchConfig.from_raw({
        "enabled": "on", "defer_core": True, "always_on": ["kanban_show"],
    })
    assert ts.is_deferrable_tool_name("kanban_show", config=cfg) is False
    for bridge in (ts.TOOL_SEARCH_NAME, ts.TOOL_DESCRIBE_NAME, ts.TOOL_CALL_NAME):
        assert ts.is_deferrable_tool_name(bridge, config=cfg) is False
