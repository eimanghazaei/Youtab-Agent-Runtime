"""WAVE-30F — offline prompt-budget profiler for the local ECO path.

Scope correction (Owner directive): the FIX is NOT to hide tools. The primary,
capability-preserving lever measured here is **schema compaction with every tool
kept directly visible** (F1: de-duplicate the board-resolution rule on the wire).
The deferred/lean loadout is reported ONLY as a clearly-labelled, default-off
EXPERIMENTAL A/B — it is not the Track A baseline and must not be enabled for the
root-cause canary without separate Owner authorization.

What is deterministic here (no model, no network, no secret):
  * the FULL-TOOL array size (all tools directly visible), with the F1 board-desc
    de-duplication already applied;
  * the F1 saving, computed from the two description constants;
  * the per-category accounting STRUCTURE for the tool-schema category.

What is NOT claimed here (requires the live canary):
  * the COMPLETE serialized prompt size (system + constitutional/security policy +
    identity + skills index + task + conversation + wrappers). Only the model's own
    ``prompt_eval_count`` is authoritative for that, captured at the authorized
    canary via youtab_runtime.model_timings. This profiler deliberately does NOT
    assert the whole request is within 4K–8K — it reports the tool-schema category
    only, plus the fields the live category accounting will fill.

Run:  YOUTAB_AGENT_KANBAN_TASK=t_probe python scripts/youtab/profile_prompt_budget.py
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("YOUTAB_AGENT_KANBAN_TASK", "t_probe")

from youtab_runtime.context_budget import (  # noqa: E402
    budget_for,
    estimate_schema_tokens,
    within_budget,
)
from tools import tool_search as ts  # noqa: E402


def _name(td):
    return (td.get("function") or {}).get("name", "?")


def _f1_board_dedup_saving():
    """Deterministic token saving from the F1 board-description de-duplication."""
    import tools.kanban_tools as kt
    full, short = kt._DESC_BOARD, kt._DESC_BOARD_SHORT
    n_short = 0
    for n in dir(kt):
        v = getattr(kt, n)
        if isinstance(v, dict) and str(v.get("name", "")).startswith("kanban_"):
            props = (v.get("parameters") or {}).get("properties") or {}
            if props.get("board", {}).get("description") == short:
                n_short += 1
    saved_chars = (len(full) - len(short)) * n_short
    return {"stubbed_tools": n_short, "saved_chars": saved_chars,
            "saved_est_tokens": saved_chars // 4}


def main() -> int:
    from model_tools import get_tool_definitions

    # FULL-tool array: every tool directly visible (no deferral). This is the
    # baseline the root-cause canary must use.
    defs = get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True) or []
    full_chars, full_tokens = estimate_schema_tokens(defs)

    report = {
        "scenario": "single_complete_quality (kanban worker, agent=default)",
        "approach": "FULL TOOLS DIRECTLY VISIBLE + capability-preserving schema compaction",
        "full_tool_array": {
            "tool_count": len(defs),
            "chars": full_chars,
            "est_tokens": full_tokens,
            "note": "all tools directly visible; no hiding/deferral; F1 dedup applied",
        },
        "f1_board_desc_dedup": _f1_board_dedup_saving(),
        "tool_schema_category_target_only": {
            "task_class": "simple",
            "target_tokens": [budget_for("simple").target_min, budget_for("simple").target_max],
            "note": (
                "This target applies to the tool-schema CATEGORY as an engineering "
                "aim, NOT to the whole serialized prompt. The complete prompt "
                "(system+policy+identity+skills+task+wrappers) is measured live; its "
                "exact size is the provider prompt_eval_count at the canary."
            ),
        },
        "complete_prompt_accounting": {
            "status": "measured_live_at_canary",
            "categories": [
                "system_prompt_total", "system_other", "system_unattributed",
                "skills_index", "tool_schemas", "conversation", "task", "history",
            ],
            "authority": "provider prompt_eval_count (exact); char/4 is a labelled estimate",
        },
        "ctx_capability_retained": budget_for("simple").ctx_capability,
        "experimental_deferred_loadout": _experimental_lean(defs),
    }
    print(json.dumps(report, indent=2))
    return 0


def _experimental_lean(defs):
    """EXPERIMENTAL, default-off A/B ONLY — not the baseline, not for the canary.

    Reports what a task-scoped deferred loadout WOULD look like, purely so the A/B
    can be evaluated later with a capability study. Every 'deferred' tool remains
    discoverable + callable via the tool_search bridge; this is NOT enabled for
    Track A and must not be without separate Owner authorization.
    """
    always_on = [n for n in (_name(d) for d in defs) if n.startswith("kanban_")]
    cfg = ts.ToolSearchConfig.from_raw(
        {"enabled": "on", "defer_core": True, "always_on": ["kanban_*"], "listing": "off"}
    )
    ts.reset_loadout_policy_cache()
    result = ts.assemble_tool_defs(defs, context_length=8000, config=cfg)
    _, lean_tokens = estimate_schema_tokens(result.tool_defs)
    _, deferrable = ts.classify_tools(defs, config=cfg)
    ts.reset_loadout_policy_cache()
    return {
        "status": "EXPERIMENTAL_DEFAULT_OFF_NOT_BASELINE",
        "always_on_glob": ["kanban_*"],
        "always_on_expanded": always_on,
        "eager_tool_count": len(result.tool_defs),
        "eager_est_tokens": lean_tokens,
        "deferred_count": len(deferrable),
        "deferred_still_discoverable": True,
        "within_simple_budget": within_budget("simple", lean_tokens),
        "caveat": "capability study required before this could ever be a baseline",
    }


if __name__ == "__main__":
    sys.exit(main())
