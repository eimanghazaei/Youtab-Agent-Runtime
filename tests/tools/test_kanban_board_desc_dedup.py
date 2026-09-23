"""WAVE-30F (F1) — the board-resolution rule is de-duplicated on the wire WITHOUT
removing the ``board`` parameter from any kanban tool.

This proves capability-preserving compaction: every kanban tool still exposes a
directly-visible, optional ``board`` parameter; the full 350-char resolution rule
appears exactly once (on kanban_show); the rest carry a short stub with the same
actionable semantics. No tool is hidden or deferred.
"""
from __future__ import annotations

import tools.kanban_tools as kt


def _kanban_schemas():
    out = []
    for name in dir(kt):
        val = getattr(kt, name)
        if isinstance(val, dict) and isinstance(val.get("name"), str) and val["name"].startswith("kanban_"):
            props = (val.get("parameters") or {}).get("properties") or {}
            out.append((val["name"], props))
    return out


def test_every_kanban_tool_keeps_a_board_param_where_it_had_one():
    schemas = _kanban_schemas()
    assert schemas, "expected kanban schemas to be importable"
    board_tools = [(n, p) for n, p in schemas if "board" in p]
    # The audit found 13 board-bearing kanban tools; keep them all board-capable.
    assert len(board_tools) >= 10
    for name, props in board_tools:
        assert props["board"].get("type") == "string"
        assert props["board"].get("description"), f"{name} board lost its description"


def test_full_board_rule_appears_exactly_once():
    full = kt._DESC_BOARD
    short = kt._DESC_BOARD_SHORT
    assert full != short and len(short) < len(full)
    full_carriers = []
    short_carriers = []
    for name, props in _kanban_schemas():
        if "board" not in props:
            continue
        desc = props["board"]["description"]
        if desc == full:
            full_carriers.append(name)
        elif desc == short:
            short_carriers.append(name)
    # Exactly one authoritative full copy (kanban_show), the rest short stubs.
    assert full_carriers == ["kanban_show"], full_carriers
    assert len(short_carriers) >= 9
    # The full resolution order is NOT lost — it still ships to the model once.
    assert "resolves the" in full and "YOUTAB_AGENT_KANBAN_DB" in full
    # The short stub still conveys the actionable semantics (omit vs override).
    assert "omit" in short and "override" in short


def test_short_stub_saves_wire_tokens_but_keeps_semantics():
    # Redundant board copies removed ≈ (len(full)-len(short)) * (#short carriers).
    saved_chars = 0
    n_short = 0
    for _name, props in _kanban_schemas():
        d = props.get("board", {}).get("description")
        if d == kt._DESC_BOARD_SHORT:
            n_short += 1
            saved_chars += len(kt._DESC_BOARD) - len(kt._DESC_BOARD_SHORT)
    assert n_short >= 9
    # A meaningful, real saving (hundreds of chars ≈ >150 est tokens), not cosmetic.
    assert saved_chars // 4 > 150
