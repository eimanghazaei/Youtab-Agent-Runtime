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


# --- max_retries=0 semantics (WAVE-30D canary retry-contract fix) ------------


def test_max_retries_zero_accepted_and_preserved():
    lim = RunLimits.validate_and_clamp({"max_retries": 0})
    assert lim.max_retries == 0
    # 0 is not None, so it round-trips through to_dict() (the set sent on create-run).
    assert lim.to_dict().get("max_retries") == 0


def test_max_retries_one_still_accepted():
    assert RunLimits.validate_and_clamp({"max_retries": 1}).max_retries == 1


def test_max_retries_clamped_to_ceiling():
    assert (
        RunLimits.validate_and_clamp({"max_retries": 999}).max_retries
        == RUN_CEILINGS["max_retries"]
    )


def test_max_retries_negative_rejected():
    with pytest.raises(RunLimitError, match="max_retries must be >= 0"):
        RunLimits.validate_and_clamp({"max_retries": -1})


@pytest.mark.parametrize(
    "field",
    [
        "max_iterations",
        "max_requests",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "max_runtime_seconds",
        "max_concurrency",
        "failure_threshold",
    ],
)
def test_zero_still_rejected_for_positive_only_fields(field):
    # Only max_retries gained a zero exemption; every other limit must still
    # reject zero (a run with zero iterations/requests/tokens is meaningless).
    with pytest.raises(RunLimitError, match="must be positive"):
        RunLimits.validate_and_clamp({field: 0})


@pytest.mark.parametrize("field", ["max_retries", "max_iterations", "max_requests"])
def test_boolean_rejected_as_non_integer(field):
    # bool is an int subclass; True/False must not be silently coerced to 1/0.
    with pytest.raises(RunLimitError, match="must be an integer"):
        RunLimits.validate_and_clamp({field: True})
    with pytest.raises(RunLimitError, match="must be an integer"):
        RunLimits.validate_and_clamp({field: False})


def test_official_canary_payload_accepted_by_create_run_validation():
    # The EXACT limit set the Stage-1 canary profile sends on POST /runs.
    # Before this fix it raised: 422 invalid_limits: max_retries must be positive.
    from tests.benchmark.harness.preflight import STAGE_PROFILES

    canary = dict(STAGE_PROFILES["canary"]["limits"])
    assert canary["max_retries"] == 0  # the profile's deliberate no-retry contract
    lim = RunLimits.validate_and_clamp(canary)  # the create-run validation path
    assert lim.max_retries == 0
    assert lim.max_requests == 1
    assert lim.max_iterations == 1
    assert Decimal(lim.max_cost_eur) == Decimal("0.10")


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


def test_per_run_cost_cap_stops_before_campaign_ceiling(ledger):
    # Run cap €0.10, worst_case €0.06 -> 2nd reservation would exceed the run cap
    # even though the €10 campaign still has room.
    e = _enforcer(ledger, RunLimits(max_cost_eur="0.10"), worst_case="0.06")
    assert e.pre_iteration(1) is None
    assert e.pre_iteration(2) == "run_cost_cap"


def test_note_failure_trips_threshold(ledger):
    e = _enforcer(ledger, RunLimits(failure_threshold=2))
    e.pre_iteration(1)
    assert e.note_failure() is None
    assert e.note_failure() == "failure_threshold"
    assert e.total_failures == 2


def test_within_iteration_retry_actuals_sum_not_overwrite(ledger):
    # M3: two observe_calls for the same iteration accumulate (0.10 + 0.10) rather
    # than the second overwriting the first.
    e = _enforcer(ledger, RunLimits(), worst_case="4.00", actual="0.10")
    e.pre_iteration(1)
    e.observe_call(api_call_count=1, input_tokens=10, output_tokens=10)
    e.observe_call(api_call_count=1, input_tokens=10, output_tokens=10)
    # reservation 4.00 replaced by summed actual 0.20 -> remaining 9.80
    assert cb.remaining_eur("camp", db_path=ledger) == Decimal("9.80")


def test_attach_wires_enforcer_and_clamps_iterations(ledger, monkeypatch):
    from youtab_runtime import run_limits as rl

    class _Agent:
        model = "m"
        provider = "p"
        max_iterations = 500

    # stub the pricing bridges so no real pricing engine is needed
    monkeypatch.setattr(cb, "price_worst_case_eur", lambda *a, **k: Decimal("0.05"))
    monkeypatch.setattr(cb, "price_actual_eur", lambda *a, **k: Decimal("0.01"))

    agent = _Agent()
    enf = rl.attach_run_limit_enforcer(
        agent,
        limits=RunLimits(max_iterations=8, max_cost_eur="2.00", max_retries=1),
        run_id="runX",
        campaign_id="campX",
        fx_usd_to_eur="0.86",
        fx_source="t",
        fx_asof="d",
        model="m",
        provider="p",
        db_path=ledger,
    )
    assert agent._run_limit_enforcer is enf
    assert agent.max_iterations == 8            # clamped from 500
    assert enf.retry_multiplier == 2            # 1 + max_retries(1)
    assert enf.pre_iteration(1) is None         # reserves fine under the run cap


def test_attach_from_environment_noop_without_campaign(monkeypatch):
    from youtab_runtime import run_limits as rl

    class _Agent:
        model = "m"
        provider = "p"
        max_iterations = 50

    monkeypatch.delenv("YOUTAB_AGENT_BENCHMARK_CAMPAIGN_ID", raising=False)
    assert rl.attach_enforcer_from_environment(
        _Agent(), run_id="r", limits_dict={"max_iterations": 5}
    ) is None


def test_attach_from_environment_end_to_end_stops_at_budget_ceiling(ledger, monkeypatch):
    """Integration: env-configured campaign -> enforcer wired -> the run stops
    fail-closed at the campaign ceiling (the loop-top gate breaks on this reason)."""
    from youtab_runtime import run_limits as rl

    class _Agent:
        model = "m"
        provider = "p"
        max_iterations = 500

    monkeypatch.setattr(cb, "price_worst_case_eur", lambda *a, **k: Decimal("4.00"))
    monkeypatch.setattr(cb, "price_actual_eur", lambda *a, **k: Decimal("4.00"))
    monkeypatch.setenv("YOUTAB_AGENT_BENCHMARK_CAMPAIGN_ID", "camp")  # reuse the tmp ledger campaign
    monkeypatch.setenv("YOUTAB_AGENT_BENCHMARK_FX_USD_EUR", "1.0")
    monkeypatch.setenv("YOUTAB_AGENT_BENCHMARK_FX_SOURCE", "test")
    monkeypatch.setenv("YOUTAB_AGENT_BENCHMARK_FX_ASOF", "2026-09-05")
    # point the helper at the tmp ledger by monkeypatching the default db path
    monkeypatch.setattr(cb, "_default_db_path", lambda: __import__("pathlib").Path(ledger))

    agent = _Agent()
    enf = rl.attach_enforcer_from_environment(
        agent, run_id="run1", limits_dict={"max_iterations": 10}
    )
    assert enf is not None and agent._run_limit_enforcer is enf
    assert enf.pre_iteration(1) is None       # reserve 4.00
    assert enf.pre_iteration(2) is None       # reserve 8.00 (total 8.00)
    assert enf.pre_iteration(3) == "budget_ceiling"  # 12.00 > €10 -> fail closed


def test_max_retries_zero_means_no_retry_budget(ledger, monkeypatch):
    """max_retries=0 -> retry_multiplier 1: a single attempt with NO retry. The
    iteration reserves exactly ONE worst-case call, never a retry-doubled amount,
    so a canary genuinely cannot retry."""
    from youtab_runtime import run_limits as rl

    class _Agent:
        model = "m"
        provider = "p"
        max_iterations = 8

    monkeypatch.setattr(cb, "price_worst_case_eur", lambda *a, **k: Decimal("1.00"))
    monkeypatch.setattr(cb, "price_actual_eur", lambda *a, **k: Decimal("0.10"))

    agent = _Agent()
    enf = rl.attach_run_limit_enforcer(
        agent,
        limits=RunLimits(max_requests=5, max_retries=0),
        run_id="runR0",
        campaign_id="campR0",
        fx_usd_to_eur="1.0",
        fx_source="t",
        fx_asof="d",
        model="m",
        provider="p",
        db_path=ledger,
    )
    assert enf.retry_multiplier == 1  # 1 + max_retries(0) -> one attempt, no retry
    assert enf.pre_iteration(1) is None
    # Exactly one worst-case (1.00) reserved — not a retry-doubled reservation.
    assert cb.remaining_eur("campR0", db_path=ledger) == Decimal("9.00")


def test_attach_from_environment_fails_closed_without_fx(monkeypatch):
    from youtab_runtime import run_limits as rl

    class _Agent:
        model = "m"
        provider = "p"
        max_iterations = 50

    monkeypatch.setenv("YOUTAB_AGENT_BENCHMARK_CAMPAIGN_ID", "campE")
    monkeypatch.delenv("YOUTAB_AGENT_BENCHMARK_FX_USD_EUR", raising=False)
    with pytest.raises(RunLimitError, match="FX snapshot"):
        rl.attach_enforcer_from_environment(
            _Agent(), run_id="r", limits_dict={"max_iterations": 5}
        )
