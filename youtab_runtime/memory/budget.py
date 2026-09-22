"""Model-aware context-budget policy.

Replaces character-limit thinking with a token budget while preserving backward
compatibility with the legacy 2200/1375-char capsule. The policy divides a
model's context window into bounded sections (always-injected rules/profile,
active task capsule, retrieved evidence) and reserves a dynamic tail for tool
schemas, the current input and the output. It never injects all history.

Token counting is deployment-supplied: character-to-token ratios are not constant
across English, Persian, Dutch, code and JSON, so an accurate policy takes a
tokenizer. Where none is available it falls back to a *conservative* ratio (fewer
chars per token) so the budget under-fills rather than overflows.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Hard ceiling on always-injected content, independent of model size. The legacy
# capsule (2200 + 1375 chars) must always fit inside this bound.
_CAPSULE_HARD_TOKEN_CAP = 3_000
# Conservative fallback: assume dense scripts (~2.0 chars/token) so we round the
# token estimate UP and never under-count.
_CONSERVATIVE_CHARS_PER_TOKEN = 2.0


class DeploymentClass(StrEnum):
    LOCAL_SMALL = "local_small"  # e.g. 32K local models
    STANDARD = "standard"  # e.g. 128K
    LARGE = "large"  # 200K+


class BudgetAllocation(BaseModel):
    """Tokens allocated to each context section, plus the dynamic reserve."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_window: int = Field(ge=1)
    capsule_tokens: int = Field(ge=0)
    task_state_tokens: int = Field(ge=0)
    retrieval_tokens: int = Field(ge=0)
    recent_turns_tokens: int = Field(ge=0)
    dynamic_reserve_tokens: int = Field(ge=0)

    def total_allocated(self) -> int:
        return (
            self.capsule_tokens
            + self.task_state_tokens
            + self.retrieval_tokens
            + self.recent_turns_tokens
            + self.dynamic_reserve_tokens
        )


# Section fractions per deployment class: (capsule, task_state, retrieval,
# recent_turns, dynamic_reserve). Reserve is deliberately large for small models.
_PROFILE: dict[DeploymentClass, tuple[float, float, float, float, float]] = {
    DeploymentClass.LOCAL_SMALL: (0.08, 0.12, 0.15, 0.15, 0.50),
    DeploymentClass.STANDARD: (0.04, 0.10, 0.16, 0.20, 0.50),
    DeploymentClass.LARGE: (0.02, 0.08, 0.18, 0.22, 0.50),
}


def estimate_tokens(text: str, counter: Callable[[str], int] | None = None) -> int:
    """Count tokens with a supplied tokenizer, else a conservative char ratio."""

    if counter is not None:
        return counter(text)
    # Round up: never under-count when guessing.
    return math.ceil(len(text) / _CONSERVATIVE_CHARS_PER_TOKEN)


def allocate(
    context_window: int,
    deployment_class: DeploymentClass = DeploymentClass.STANDARD,
) -> BudgetAllocation:
    """Divide a context window into bounded sections with a hard capsule cap."""

    if context_window < 1:
        raise ValueError("context_window must be positive")
    cap_f, task_f, ret_f, recent_f, _reserve_f = _PROFILE[deployment_class]
    capsule = min(int(context_window * cap_f), _CAPSULE_HARD_TOKEN_CAP)
    task_state = int(context_window * task_f)
    retrieval = int(context_window * ret_f)
    recent = int(context_window * recent_f)
    # The reserve absorbs rounding and is whatever remains; it can never be negative
    # because the non-reserve fractions sum to <= 0.60 above.
    reserve = context_window - (capsule + task_state + retrieval + recent)
    if reserve < 0:  # defensive: should be unreachable with the profiles above
        raise ValueError("budget profile over-allocates the context window")
    return BudgetAllocation(
        context_window=context_window,
        capsule_tokens=capsule,
        task_state_tokens=task_state,
        retrieval_tokens=retrieval,
        recent_turns_tokens=recent,
        dynamic_reserve_tokens=reserve,
    )


def legacy_capsule_fits(
    memory_chars: int = 2200,
    user_chars: int = 1375,
    counter: Callable[[str], int] | None = None,
) -> bool:
    """Backward-compat check: the legacy char capsule fits the hard token cap.

    Uses the conservative estimator by default, so a True result holds for any
    real tokenizer that counts fewer tokens than the dense-script fallback.
    """

    approx = estimate_tokens("x" * (memory_chars + user_chars), counter)
    return approx <= _CAPSULE_HARD_TOKEN_CAP
