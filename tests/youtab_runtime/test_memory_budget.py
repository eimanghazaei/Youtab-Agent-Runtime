from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    BudgetAllocation,
    BudgetInputs,
    DeploymentClass,
    TaskClass,
    allocate,
    estimate_tokens,
    legacy_capsule_fits,
    selective_retrieval_tokens,
)


def _inputs(**overrides: object) -> BudgetInputs:
    base: dict[str, object] = {
        "context_window": 128_000,
        "output_reserve_tokens": 8_000,
        "tool_schema_tokens": 4_000,
        "safety_context_tokens": 1_000,
        "compaction_reserve_tokens": 4_000,
        "deployment_class": DeploymentClass.STANDARD,
        "task_class": TaskClass.INTERACTIVE,
    }
    base.update(overrides)
    return BudgetInputs(**base)


def test_allocation_is_derived_and_never_exceeds_window() -> None:
    alloc = allocate(_inputs())
    assert isinstance(alloc, BudgetAllocation)
    assert alloc.total_allocated() == 128_000
    # fixed reserves are passed through untouched
    assert alloc.output_reserve_tokens == 8_000
    assert alloc.tool_schema_tokens == 4_000
    assert alloc.safety_context_tokens == 1_000
    assert alloc.compaction_reserve_tokens == 4_000


def test_capsule_cap_is_configurable_not_universal() -> None:
    small = allocate(_inputs(deployment_class=DeploymentClass.LOCAL_SMALL, context_window=32_000))
    large = allocate(_inputs(deployment_class=DeploymentClass.LARGE, context_window=200_000))
    assert small.capsule_tokens == 1_500  # per-class default
    assert large.capsule_tokens == 3_000
    override = allocate(_inputs(capsule_cap_tokens=800))
    assert override.capsule_tokens == 800  # per-call override wins


def test_long_running_shifts_budget_to_task_state_and_retrieval() -> None:
    interactive = allocate(_inputs(task_class=TaskClass.INTERACTIVE))
    long_running = allocate(_inputs(task_class=TaskClass.LONG_RUNNING))
    assert long_running.task_state_tokens > interactive.task_state_tokens
    assert long_running.retrieval_tokens > interactive.retrieval_tokens
    assert interactive.recent_turns_tokens > long_running.recent_turns_tokens


def test_large_memory_stays_external_selective_retrieval() -> None:
    alloc = allocate(_inputs())
    huge_corpus = 50_000_000  # 50M tokens of enterprise memory
    injected = selective_retrieval_tokens(alloc, huge_corpus)
    # only the retrieval budget is injected, independent of corpus size
    assert injected == alloc.retrieval_tokens
    assert injected < huge_corpus
    # a tiny corpus injects only what exists
    assert selective_retrieval_tokens(alloc, 10) == 10


def test_fixed_reserves_that_fill_the_window_are_rejected() -> None:
    with pytest.raises(ValueError):
        BudgetInputs(context_window=1_000, output_reserve_tokens=1_000)


def test_estimate_tokens_uses_supplied_counter_else_conservative() -> None:
    assert estimate_tokens("anything", counter=lambda s: 42) == 42
    assert estimate_tokens("abcde") == 3  # ceil(5/2.0)
    assert estimate_tokens("") == 0


def test_legacy_capsule_fits_effective_cap() -> None:
    # 2200 + 1375 = 3575 chars -> ceil(3575/2) = 1788 tokens
    assert legacy_capsule_fits(_inputs()) is True  # cap 2500
    assert legacy_capsule_fits(_inputs(deployment_class=DeploymentClass.LARGE, context_window=200_000)) is True
    # a very tight capsule cap cannot hold the legacy capsule
    assert legacy_capsule_fits(_inputs(capsule_cap_tokens=1_000)) is False
