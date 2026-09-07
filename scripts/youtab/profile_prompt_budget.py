"""WAVE-30E — offline prompt token-budget profiler for the local ECO path.

Deterministically reconstructs the kanban-worker tool loadout (the dominant
contributor to the 21,421-token canary prompt) and reports a per-category token
breakdown for the BASELINE (eager full catalog) vs the task-scoped LEAN loadout
(kanban primary tools eager + the rest deferred behind the tool_search bridge,
still fully discoverable).

No model, no network, no secret: it measures serialized tool-schema sizes with
the same char/4 estimate the tool-search gate uses. The exact serialized token
count is the provider's ``prompt_eval_count`` — captured live by the
instrumentation (youtab_runtime.model_timings), never fabricated here.

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


def main() -> int:
    from model_tools import get_tool_definitions

    defs = get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True) or []
    baseline_chars, baseline_tokens = estimate_schema_tokens(defs)

    always_on = [n for n in (_name(d) for d in defs) if n.startswith("kanban_")]
    cfg = ts.ToolSearchConfig.from_raw(
        {"enabled": "on", "defer_core": True, "always_on": always_on, "listing": "off"}
    )
    ts.reset_loadout_policy_cache()
    result = ts.assemble_tool_defs(defs, context_length=8000, config=cfg)
    lean_chars, lean_tokens = estimate_schema_tokens(result.tool_defs)

    _, deferrable = ts.classify_tools(defs, config=cfg)
    deferred_chars, deferred_tokens = estimate_schema_tokens(deferrable)

    budget = budget_for("simple")  # the canary is the simple class
    report = {
        "scenario": "single_complete_quality (kanban worker, agent=default)",
        "task_class": "simple",
        "target_tokens": [budget.target_min, budget.target_max],
        "ctx_capability_retained": budget.ctx_capability,
        "tool_schemas": {
            "baseline": {"tool_count": len(defs), "chars": baseline_chars,
                         "est_tokens": baseline_tokens},
            "lean": {"eager_tool_count": len(result.tool_defs), "chars": lean_chars,
                     "est_tokens": lean_tokens, "activated": result.activated,
                     "deferred_count": result.deferred_count},
            "deferred_still_discoverable": {"count": len(deferrable),
                                            "est_tokens": deferred_tokens},
            "reduction_est_tokens": baseline_tokens - lean_tokens,
        },
        "always_on_primary_tools": always_on,
        "lean_tool_schema_within_simple_budget": within_budget("simple", lean_tokens),
        "note": (
            "Tool schemas are the dominant contributor. system-prompt guidance for "
            "deferred tools also drops (guidance is gated on visible tools), further "
            "shrinking the initial prompt. Exact serialized tokens = provider "
            "prompt_eval_count, captured live by the instrumentation."
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
