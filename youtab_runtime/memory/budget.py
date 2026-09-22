"""Model-aware context-budget policy.

Replaces flat character limits with a token budget *derived from the actual
deployment*, while preserving backward compatibility with the legacy
2200/1375-char capsule. The injected capsule stays bounded, but the bound is
configurable per deployment/model/task class — it is NOT a universal constant.

The window is partitioned as::

    context_window
      = output_reserve            (max model output)
      + tool_schema_tokens        (system + tool schemas)
      + safety_context_tokens     (approval/safety context)
      + compaction_reserve_tokens (headroom for in-place compaction)
      + capsule_tokens            (bounded always-injected rules/profile)
      + task_state_tokens         (active plan/checkpoint capsule)
      + retrieval_tokens          (SELECTIVELY retrieved memory evidence)
      + recent_turns_tokens       (recent conversation)
      + dynamic_reserve_tokens    (remainder / rounding headroom)

Large enterprise memory is never fully injected: only ``retrieval_tokens`` worth
of selected evidence enters the prompt, independent of corpus size. Token
counting is deployment-supplied (no fixed char/token ratio across English,
Persian, Dutch, code, JSON); the fallback rounds up conservatively.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_CONSERVATIVE_CHARS_PER_TOKEN = 2.0


class DeploymentClass(StrEnum):
    LOCAL_SMALL = "local_small"  # e.g. 32K local models
    STANDARD = "standard"  # e.g. 128K
    LARGE = "large"  # 200K+


class TaskClass(StrEnum):
    INTERACTIVE = "interactive"  # short turns, more recent-conversation budget
    LONG_RUNNING = "long_running"  # more task-state + retrieval budget


# Configurable default capsule caps per deployment class (tokens). These are
# starting points for evaluation, overridable per call, NOT a universal constant.
_DEFAULT_CAPSULE_CAP: dict[DeploymentClass, int] = {
    DeploymentClass.LOCAL_SMALL: 1_500,
    DeploymentClass.STANDARD: 2_500,
    DeploymentClass.LARGE: 3_000,
}

# How the memory-available pool (after fixed reserves + capsule) splits into
# (task_state, retrieval, recent_turns). The remainder is dynamic reserve.
_MEMORY_SPLIT: dict[TaskClass, tuple[float, float, float]] = {
    TaskClass.INTERACTIVE: (0.15, 0.25, 0.35),
    TaskClass.LONG_RUNNING: (0.30, 0.35, 0.15),
}


class BudgetInputs(BaseModel):
    """The deployment facts a budget is derived from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_window: int = Field(ge=1)
    output_reserve_tokens: int = Field(ge=0)
    tool_schema_tokens: int = Field(default=0, ge=0)
    safety_context_tokens: int = Field(default=0, ge=0)
    compaction_reserve_tokens: int = Field(default=0, ge=0)
    deployment_class: DeploymentClass = DeploymentClass.STANDARD
    task_class: TaskClass = TaskClass.INTERACTIVE
    # Per-call override of the capsule cap; None uses the per-class default.
    capsule_cap_tokens: int | None = Field(default=None, ge=0)

    def fixed_reserve(self) -> int:
        return (
            self.output_reserve_tokens
            + self.tool_schema_tokens
            + self.safety_context_tokens
            + self.compaction_reserve_tokens
        )

    def effective_capsule_cap(self) -> int:
        if self.capsule_cap_tokens is not None:
            return self.capsule_cap_tokens
        return _DEFAULT_CAPSULE_CAP[self.deployment_class]

    @model_validator(mode="after")
    def _room_for_memory(self) -> "BudgetInputs":
        if self.fixed_reserve() >= self.context_window:
            raise ValueError("fixed reserves leave no room in the context window")
        return self


class BudgetAllocation(BaseModel):
    """Tokens allocated to each context section (derived, bounded)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_window: int = Field(ge=1)
    output_reserve_tokens: int = Field(ge=0)
    tool_schema_tokens: int = Field(ge=0)
    safety_context_tokens: int = Field(ge=0)
    compaction_reserve_tokens: int = Field(ge=0)
    capsule_tokens: int = Field(ge=0)
    task_state_tokens: int = Field(ge=0)
    retrieval_tokens: int = Field(ge=0)
    recent_turns_tokens: int = Field(ge=0)
    dynamic_reserve_tokens: int = Field(ge=0)

    def total_allocated(self) -> int:
        return (
            self.output_reserve_tokens
            + self.tool_schema_tokens
            + self.safety_context_tokens
            + self.compaction_reserve_tokens
            + self.capsule_tokens
            + self.task_state_tokens
            + self.retrieval_tokens
            + self.recent_turns_tokens
            + self.dynamic_reserve_tokens
        )


def estimate_tokens(text: str, counter: Callable[[str], int] | None = None) -> int:
    """Count tokens with a supplied tokenizer, else a conservative char ratio."""

    if counter is not None:
        return counter(text)
    return math.ceil(len(text) / _CONSERVATIVE_CHARS_PER_TOKEN)


def allocate(inputs: BudgetInputs) -> BudgetAllocation:
    """Derive a bounded per-section allocation from deployment facts."""

    available = inputs.context_window - inputs.fixed_reserve()
    capsule = min(inputs.effective_capsule_cap(), available)
    memory_pool = available - capsule
    task_f, ret_f, recent_f = _MEMORY_SPLIT[inputs.task_class]
    task_state = int(memory_pool * task_f)
    retrieval = int(memory_pool * ret_f)
    recent = int(memory_pool * recent_f)
    dynamic_reserve = memory_pool - (task_state + retrieval + recent)
    if dynamic_reserve < 0:  # defensive: split fractions sum < 1.0 above
        raise ValueError("memory split over-allocates the pool")
    return BudgetAllocation(
        context_window=inputs.context_window,
        output_reserve_tokens=inputs.output_reserve_tokens,
        tool_schema_tokens=inputs.tool_schema_tokens,
        safety_context_tokens=inputs.safety_context_tokens,
        compaction_reserve_tokens=inputs.compaction_reserve_tokens,
        capsule_tokens=capsule,
        task_state_tokens=task_state,
        retrieval_tokens=retrieval,
        recent_turns_tokens=recent,
        dynamic_reserve_tokens=dynamic_reserve,
    )


def selective_retrieval_tokens(
    allocation: BudgetAllocation, available_memory_tokens: int
) -> int:
    """Tokens of memory actually injected: min(retrieval budget, what exists).

    This is the proof that large enterprise memory stays EXTERNAL: the injected
    amount is capped by ``retrieval_tokens`` regardless of how large the corpus
    is. A corpus far larger than the budget contributes only the budgeted slice.
    """

    return min(allocation.retrieval_tokens, max(0, available_memory_tokens))


def legacy_capsule_fits(
    inputs: BudgetInputs,
    memory_chars: int = 2200,
    user_chars: int = 1375,
    counter: Callable[[str], int] | None = None,
) -> bool:
    """Backward-compat: the legacy char capsule fits the effective capsule cap."""

    approx = estimate_tokens("x" * (memory_chars + user_chars), counter)
    return approx <= inputs.effective_capsule_cap()
