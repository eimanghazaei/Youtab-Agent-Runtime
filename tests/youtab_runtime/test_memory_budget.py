from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    BudgetAllocation,
    DeploymentClass,
    allocate,
    estimate_tokens,
    legacy_capsule_fits,
)
from youtab_runtime.memory.budget import _CAPSULE_HARD_TOKEN_CAP


@pytest.mark.parametrize(
    "window,klass",
    [
        (32_000, DeploymentClass.LOCAL_SMALL),
        (128_000, DeploymentClass.STANDARD),
        (200_000, DeploymentClass.LARGE),
    ],
)
def test_allocation_never_exceeds_window(window: int, klass: DeploymentClass) -> None:
    alloc = allocate(window, klass)
    assert isinstance(alloc, BudgetAllocation)
    assert alloc.total_allocated() == window  # reserve absorbs remainder exactly
    assert alloc.dynamic_reserve_tokens > 0


def test_capsule_is_hard_capped_regardless_of_window() -> None:
    big = allocate(1_000_000, DeploymentClass.LOCAL_SMALL)
    assert big.capsule_tokens <= _CAPSULE_HARD_TOKEN_CAP


def test_larger_window_gives_more_retrieval_than_small() -> None:
    small = allocate(32_000, DeploymentClass.LOCAL_SMALL)
    large = allocate(200_000, DeploymentClass.LARGE)
    assert large.retrieval_tokens > small.retrieval_tokens
    # both deployment classes keep a healthy dynamic tail (>= 50% of the window)
    assert small.dynamic_reserve_tokens / 32_000 >= 0.50
    assert large.dynamic_reserve_tokens / 200_000 >= 0.50


def test_estimate_tokens_uses_supplied_counter() -> None:
    assert estimate_tokens("anything", counter=lambda s: 42) == 42


def test_conservative_estimate_rounds_up_and_never_undercounts() -> None:
    # 5 chars / 2.0 chars-per-token -> ceil(2.5) = 3
    assert estimate_tokens("abcde") == 3
    assert estimate_tokens("") == 0


def test_legacy_capsule_fits_hard_cap() -> None:
    # 2200 + 1375 = 3575 chars -> ceil(3575/2) = 1788 tokens <= 3000
    assert legacy_capsule_fits() is True
    assert legacy_capsule_fits(memory_chars=2200, user_chars=1375) is True


def test_zero_window_is_rejected() -> None:
    with pytest.raises(ValueError):
        allocate(0)
