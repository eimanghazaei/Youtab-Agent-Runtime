"""WAVE-30E — task-class budgets + per-category token accounting."""
from __future__ import annotations

from youtab_runtime.context_budget import (
    CANARY_TASK_CLASS,
    FULL_CTX_CAPABILITY,
    TASK_CLASS_BUDGETS,
    budget_for,
    estimate_schema_tokens,
    estimate_tokens,
    measure_prompt_categories,
    numeric_category_summary,
    plan_context,
    within_budget,
)


def test_adaptive_ladder_keeps_64k_capability_at_every_class():
    # The prompt-token TARGET narrows by class, but the context CAPABILITY is
    # always the full authorized 64K — the two axes are independent.
    for b in TASK_CLASS_BUDGETS.values():
        assert b.ctx_capability == 64_000
    assert budget_for("simple").target_max == 8_000
    assert budget_for("tool_heavy").target_max == 16_000
    assert budget_for("memory_rag").target_max == 32_000
    assert budget_for("document").target_max == 64_000
    # canary is the simple class → 4K-8K target
    assert CANARY_TASK_CLASS == "simple"
    assert budget_for(CANARY_TASK_CLASS).target_min == 4_000


def test_within_budget_is_target_max_gate():
    assert within_budget("simple", 7_000) is True
    assert within_budget("simple", 8_000) is True
    assert within_budget("simple", 8_001) is False
    # a document task legitimately exceeds the simple target
    assert within_budget("document", 40_000) is True


def test_estimate_tokens_char_over_four():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_measure_categories_splits_skills_tools_conversation():
    system = (
        "You are a helpful agent. POLICY: be safe.\n"
        "<available_skills>\nskill-a: does a\nskill-b: does b\n</available_skills>\n"
        "More system guidance here."
    )
    tools = [
        {"type": "function", "function": {"name": "kanban_show", "description": "show",
                                          "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "memory", "description": "remember",
                                          "parameters": {"type": "object", "properties": {}}}},
    ]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "work kanban task t_123"},
    ]
    cats = measure_prompt_categories(system_prompt=system, tool_defs=tools, messages=messages)

    # Skills index is carved out of the system prompt.
    assert cats["skills_index"]["chars"] > 0
    assert "<available_skills>" not in "x"  # sanity
    assert cats["system_other"]["chars"] == len(system) - cats["skills_index"]["chars"]
    # Tool schemas measured from the defs.
    schema_chars, schema_tokens = estimate_schema_tokens(tools)
    assert cats["tool_schemas"]["chars"] == schema_chars
    assert cats["tool_schemas"]["est_tokens"] == schema_tokens
    assert cats["tool_count"] == 2
    # Conversation excludes the system message.
    assert cats["conversation"]["chars"] == len("work kanban task t_123")
    # Total = system + tools + conversation.
    assert cats["total"]["chars"] == (
        cats["system_prompt_total"]["chars"] + schema_chars + cats["conversation"]["chars"]
    )


def test_numeric_summary_is_all_numeric_and_carries_no_text():
    system = "sys <available_skills>\nskill-a: a\n</available_skills> tail"
    cats = measure_prompt_categories(
        system_prompt=system,
        tool_defs=[{"type": "function", "function": {"name": "t", "description": "d",
                                                     "parameters": {"type": "object"}}}],
        messages=[{"role": "user", "content": "hello"}],
    )
    summary = numeric_category_summary(cats)
    assert summary  # non-empty
    for k, v in summary.items():
        assert isinstance(k, str)
        assert isinstance(v, int)
    assert "tool_schemas_est_tokens" in summary
    assert "skills_index_est_tokens" in summary


def test_known_blocks_attribution():
    kanban_guidance = "KANBAN: always call kanban_show first."
    system = "identity...\n" + kanban_guidance + "\nmore"
    cats = measure_prompt_categories(
        system_prompt=system, tool_defs=[], messages=[],
        known_blocks={"kanban_guidance": kanban_guidance, "absent": "not here"},
    )
    assert cats["block_kanban_guidance"]["chars"] == len(kanban_guidance)
    assert cats["block_absent"]["chars"] == 0  # not present → zero, not fabricated
    # The unattributed remainder is the system-other minus the found block, and is
    # never negative (honest floor for what the accounting could not yet name).
    assert cats["system_unattributed"]["chars"] == (
        cats["system_other"]["chars"] - len(kanban_guidance)
    )
    assert cats["system_unattributed"]["chars"] >= 0


def test_task_vs_history_split_and_percentages():
    system = "You are an agent."
    tools = [{"type": "function", "function": {"name": "kanban_show", "description": "d",
                                               "parameters": {"type": "object"}}}]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "old turn"},
        {"role": "assistant", "content": "old reply"},
        {"role": "user", "content": "the actual task"},
    ]
    cats = measure_prompt_categories(system_prompt=system, tool_defs=tools, messages=messages)
    # The task is only the LAST non-system message; the rest is history.
    assert cats["task"]["chars"] == len("the actual task")
    assert cats["history"]["chars"] == len("old turn") + len("old reply")
    # Percentages cover the disjoint top-level categories and sum to ~100%.
    pct = cats["percentages"]
    assert set(pct) == {"system_prompt_total", "tool_schemas", "conversation"}
    assert abs(sum(pct.values()) - 100.0) < 0.5


def test_plan_context_reserves_output_and_tool_loop_and_fits():
    # A lean simple prompt fits with output + tool-loop reserved.
    plan = plan_context("simple", 6_000, num_ctx=64_000)
    assert plan.reserve_output_tokens == 2_000
    assert plan.reserve_tool_loop_tokens == 2_000
    assert plan.required_tokens == 10_000
    assert plan.headroom_tokens == 54_000
    assert plan.fits is True
    assert plan.must_escalate is False
    assert plan.ctx_capability == FULL_CTX_CAPABILITY


def test_plan_context_refuses_silent_truncation_and_escalates():
    # Prompt + reserves exceed a small configured ctx → must escalate, never drop.
    plan = plan_context("simple", 7_000, num_ctx=8_000)
    assert plan.required_tokens == 11_000
    assert plan.fits is False
    assert plan.must_escalate is True
    assert plan.dropped == ()  # this planner NEVER silently truncates
    assert plan.recommended_ctx >= plan.required_tokens
    assert plan.recommended_ctx <= FULL_CTX_CAPABILITY


def test_plan_context_boundary_classes_retain_64k_capability():
    # Every class retains the full 64K capability regardless of its prompt target.
    for cls, ctx in (("simple", 8_000), ("tool_heavy", 16_000),
                     ("memory_rag", 32_000), ("document", 64_000)):
        plan = plan_context(cls, 1_000, num_ctx=ctx)
        assert plan.num_ctx == ctx
        assert plan.ctx_capability == 64_000
        assert plan.fits is True
    # A document-class prompt whose reserves push it PAST the 64K ctx escalates
    # rather than truncating; recommended_ctx is capped at the 64K ceiling and the
    # caller must then shed content explicitly (never silently).
    plan = plan_context("document", 61_000, num_ctx=64_000)  # +4000 reserves = 65000 > 64000
    assert plan.fits is False
    assert plan.must_escalate is True
    assert plan.recommended_ctx == FULL_CTX_CAPABILITY
