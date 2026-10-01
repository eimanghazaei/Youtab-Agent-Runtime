"""WAVE-30F (#7) — always_on config semantics: literal names AND glob patterns.

Proves the previously-silent footgun is fixed: ``always_on: ["kanban_*"]`` now
pins every kanban tool eager (glob), while a literal ``"kanban_show"`` still
matches itself. Guards the experimental default-off lean loadout only; it keeps
tools EAGER (never hides capability).
"""
from __future__ import annotations

from tools.tool_search import ToolSearchConfig, _matches_always_on, is_deferrable_tool_name


def test_literal_name_matches_itself():
    assert _matches_always_on("kanban_show", frozenset({"kanban_show"})) is True
    assert _matches_always_on("kanban_list", frozenset({"kanban_show"})) is False


def test_glob_pattern_matches_family():
    ao = frozenset({"kanban_*"})
    assert _matches_always_on("kanban_show", ao) is True
    assert _matches_always_on("kanban_complete", ao) is True
    # A non-kanban tool is NOT pinned by the kanban glob.
    assert _matches_always_on("memory", ao) is False


def test_empty_always_on_pins_nothing():
    assert _matches_always_on("kanban_show", frozenset()) is False


def test_glob_keeps_core_tool_eager_under_defer_core():
    # With defer_core on and a glob always_on, a matching core tool is NOT
    # deferrable (stays eager); a non-matching core tool becomes deferrable.
    cfg = ToolSearchConfig.from_raw(
        {"enabled": "on", "defer_core": True, "always_on": ["kanban_*"]}
    )
    # kanban_show is a core tool pinned by the glob → not deferrable.
    assert is_deferrable_tool_name("kanban_show", config=cfg) is False
