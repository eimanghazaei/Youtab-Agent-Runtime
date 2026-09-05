"""Authoritative per-run limit + budget enforcer tests (WAVE-30B §8/§10/§11)."""

from __future__ import annotations

from decimal import Decimal
from functools import partial

import pytest

from youtab_runtime import campaign_budget as cb
from youtab_runtime.run_limits import (
    CAMPAIGN_CEILING_EUR,
    RUN_CEILINGS,
    RunLimitEnforcer,
    RunLimitError,
    RunLimits,
)


# --- validate_and_clamp -----------------------------------------------------


def test_clamp_to_ceilings():
    lim = RunLimits.validate_and_clamp(
        {
            "max_iterations": 10_000,
            "max_requests": 10_000,
            "max_total_tokens": 9_999_999_999,
            "max_runtime_seconds": 999_999,
            "max_cost_eur": "50.00",
        }
    )
    assert lim.max_iterations == RUN_CEILINGS["max_iterations"]
    assert lim.max_requests == RUN_CEILINGS["max_requests"]
    assert lim.max_total_tokens == RUN_CEILINGS["max_total_tokens"]
    assert lim.max_runtime_seconds == RUN_CEILINGS["max_runtime_seconds"]
    assert Decimal(lim.max_cost_eur) == CAMPAIGN_CEILING_EUR


def test_clamp_keeps_smaller_values():
    lim = RunLimits.validate_and_clamp(
        {"max_iterations": 8, "max_cost_eur": "2.00", "max_total_tokens": 250_000}
    )
    assert lim.max_iterations == 8
    assert lim.max_cost_eur == "2.00"
    assert lim.max_total_tokens == 250_000


@pytest.mark.parametrize("bad", [{"max_iterations": 0}, {"max_requests": -1}, {"max_cost_eur": "-1"}])
def test_reject_non_positive(bad):
    with pytest.raises(RunLimitError):
        RunLimits.validate_and_clamp(bad)


def test_reject_non_numeric():
    with pytest.raises(RunLimitError):
        RunLimits.validate_and_clamp({"max_iterations": "abc"})


def test_unknown_keys_ignored():
    lim = RunLimits.validate_and_clamp({"nonsense": 5, "max_iterations": 3})
    assert lim.max_iterations == 3
    assert "nonsense" not in lim.to_dict()


# --- enforcer with the real durable ledger ----------------------------------


@pytest.fixture
def ledger(tmp_path):
    db = str(tmp_path / "cb.db")
    cb.open_campaign(
        "camp",
        ceiling_eur="10.00",
        fx_usd_to_eur="1.0",
        fx_source="t",
        fx_asof="d",
        db_path=db,
    )
    return db


def _enforcer(ledger, limits, *, worst_case="0.50", actual="0.10", retry_multiplier=1):
    return RunLimitEnforcer(
        run_id="run1",
        campaign_id="camp",
        limits=limits,
        model="m",
        provider="p",
        reserve_fn=partial(cb.reserve, db_path=ledger),
        reconcile_fn=partial(cb.reconcile, db_path=ledger),
        worst_case_eur_fn=lambda **k: Decimal(worst_case),
        actual_eur_fn=lambda **k: Decimal(actual),
        retry_multiplier=retry_multiplier,
    )


def test_pre_iteration_reserves_and_proceeds(ledger):
    e = _enforcer(ledger, RunLimits(max_requests=5))
    assert e.pre_iteration(1) is None
    assert e.requests_made == 1
    assert cb.remaining_eur("camp", db_path=ledger) == Decimal("9.50")


def test_max_requests_stop(ledger):
    e = _enforcer(ledger, RunLimits(max_requests=2))
    assert e.pre_iteration(1) is None
    assert e.pre_iteration(2) is None
    assert e.pre_iteration(3) == "max_requests"
    assert e.stopped_reason == "max_requests"


def test_budget_ceiling_stop(ledger):
    # worst case 4.00 × retry_multiplier 1; ceiling 10 -> 3rd reservation exceeds
    e = _enforcer(ledger, RunLimits(), worst_case="4.00")
    assert e.pre_iteration(1) is None
    assert e.pre_iteration(2) is None
    assert e.pre_iteration(3) == "budget_ceiling"


def test_retry_multiplier_reserves_more(ledger):
    e = _enforcer(ledger, RunLimits(), worst_case="1.00", retry_multiplier=3)
    e.pre_iteration(1)
    assert cb.remaining_eur("camp", db_path=ledger) == Decimal("7.00")


def test_pricing_unavailable_fail_closed(ledger):
    e = RunLimitEnforcer(
        run_id="run1", campaign_id="camp", limits=RunLimits(), model="m", provider="p",
        reserve_fn=partial(cb.reserve, db_path=ledger),
        reconcile_fn=partial(cb.reconcile, db_path=ledger),
        worst_case_eur_fn=lambda **k: (_ for _ in ()).throw(cb.PricingUnavailable("no price")),
        actual_eur_fn=lambda **k: None,
    )
    assert e.pre_iteration(1) == "pricing_unavailable"


def test_observe_reconciles_and_frees_margin(ledger):
    e = _enforcer(ledger, RunLimits(), worst_case="4.00", actual="0.50")
    e.pre_iteration(1)
    assert cb.remaining_eur("camp", db_path=ledger) == Decimal("6.00")
    e.observe_call(api_call_count=1, input_tokens=100, output_tokens=50, ok=True)
    # reservation 4.00 replaced by actual 0.50
    assert cb.remaining_eur("camp", db_path=ledger) == Decimal("9.50")
    assert e.total_tokens == 150


def test_failure_threshold_stop(ledger):
    e = _enforcer(ledger, RunLimits(failure_threshold=2))
    e.pre_iteration(1)
    assert e.observe_call(api_call_count=1, ok=False) is None
    assert e.observe_call(api_call_count=1, ok=False) == "failure_threshold"
    assert e.total_failures == 2


def test_consecutive_failures_reset_on_success(ledger):
    e = _enforcer(ledger, RunLimits(failure_threshold=3))
    e.pre_iteration(1)
    e.observe_call(api_call_count=1, ok=False)
    e.observe_call(api_call_count=1, ok=True)
    assert e.consecutive_failures == 0
    assert e.total_failures == 1


def test_total_tokens_stop_on_observe(ledger):
    e = _enforcer(ledger, RunLimits(max_total_tokens=100))
    e.pre_iteration(1)
    assert e.observe_call(api_call_count=1, input_tokens=80, output_tokens=40) == "max_total_tokens"


def test_snapshot_shape(ledger):
    e = _enforcer(ledger, RunLimits(max_requests=3))
    e.pre_iteration(1)
    snap = e.snapshot()
    assert snap.requests_made == 1
    assert snap.stopped_reason is None
