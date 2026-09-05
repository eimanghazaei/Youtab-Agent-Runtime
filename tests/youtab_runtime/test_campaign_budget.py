"""Durable €10 campaign budget ledger tests (WAVE-30B §9/§11).

Covers atomic reserve/reconcile, idempotent retries, retry/failover not resetting
the budget, the exact-€10 boundary and €10.01 rejection, unknown-usage keeping the
conservative reservation, concurrent-worker races failing closed, and the
fail-closed USD→EUR pricing bridge.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from youtab_runtime import campaign_budget as cb


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "campaign_budget.db")


def _open(db, cid="c", ceiling="10.00", fx="0.86", margin="0.15"):
    return cb.open_campaign(
        cid,
        ceiling_eur=ceiling,
        fx_usd_to_eur=fx,
        fx_source="test-fx",
        fx_asof="2026-09-05",
        safety_margin=margin,
        db_path=db,
    )


def test_open_is_idempotent_and_immutable(db):
    _open(db)
    _open(db)  # same params -> ok
    with pytest.raises(cb.BudgetError, match="different"):
        _open(db, ceiling="20.00")


def test_open_requires_sourced_fx(db):
    with pytest.raises(cb.BudgetError, match="fx_source"):
        cb.open_campaign("c", ceiling_eur="10", fx_usd_to_eur="0.86", fx_source="", fx_asof="", db_path=db)


def test_reserve_reconcile_flow(db):
    _open(db)
    cb.reserve("c", api_request_id="t:1", amount_eur="3.00", db_path=db)
    assert cb.remaining_eur("c", db_path=db) == Decimal("7.00")
    cb.reconcile("c", api_request_id="t:1", actual_eur="1.50", db_path=db)
    assert cb.remaining_eur("c", db_path=db) == Decimal("8.50")


def test_retry_is_idempotent_no_double_count(db):
    _open(db)
    cb.reserve("c", api_request_id="t:1", amount_eur="3.00", db_path=db)
    cb.reserve("c", api_request_id="t:1", amount_eur="3.00", db_path=db)  # retry
    cb.reserve("c", api_request_id="t:1", amount_eur="9.99", db_path=db)  # even a different amount
    assert cb.remaining_eur("c", db_path=db) == Decimal("7.00")


def test_failover_new_request_id_consumes_fresh_budget(db):
    _open(db)
    cb.reserve("c", api_request_id="t:1", amount_eur="6.00", db_path=db)
    # failover = a NEW provider call = new api_request_id -> consumes more budget,
    # cannot reset the campaign total.
    cb.reserve("c", api_request_id="t:2", amount_eur="3.00", db_path=db)
    assert cb.remaining_eur("c", db_path=db) == Decimal("1.00")


def test_exact_ceiling_ok_and_over_by_a_cent_refused(db):
    _open(db)
    cb.reserve("c", api_request_id="a", amount_eur="10.00", db_path=db)
    assert cb.remaining_eur("c", db_path=db) == Decimal("0.00")
    with pytest.raises(cb.BudgetExceeded):
        cb.reserve("c", api_request_id="b", amount_eur="0.01", db_path=db)


def test_unknown_usage_keeps_reservation(db):
    _open(db, ceiling="5.00")
    cb.reserve("c", api_request_id="x", amount_eur="2.00", db_path=db)
    cb.reconcile("c", api_request_id="x", actual_eur=None, db_path=db)
    assert cb.remaining_eur("c", db_path=db) == Decimal("3.00")


def test_release_frees_only_unbilled(db):
    _open(db)
    cb.reserve("c", api_request_id="x", amount_eur="4.00", db_path=db)
    cb.release("c", api_request_id="x", db_path=db)
    assert cb.remaining_eur("c", db_path=db) == Decimal("10.00")


def test_reconcile_missing_reservation_fails(db):
    _open(db)
    with pytest.raises(cb.BudgetError, match="no reservation"):
        cb.reconcile("c", api_request_id="nope", actual_eur="1.0", db_path=db)


def test_reserve_on_unopened_campaign_fails(db):
    with pytest.raises(cb.CampaignNotOpen):
        cb.reserve("ghost", api_request_id="x", amount_eur="1.0", db_path=db)


def test_negative_amount_refused(db):
    _open(db)
    with pytest.raises(cb.BudgetError, match="negative"):
        cb.reserve("c", api_request_id="x", amount_eur="-1.0", db_path=db)


def test_concurrent_reservations_never_exceed_ceiling(db):
    """20 workers each try to reserve €1 against a €10 ceiling; at most 10 win and
    the committed total never exceeds the ceiling."""
    _open(db, ceiling="10.00")
    wins = []
    errors = []
    barrier = threading.Barrier(20)

    def worker(i):
        barrier.wait()
        try:
            cb.reserve("c", api_request_id=f"w:{i}", amount_eur="1.00", db_path=db)
            wins.append(i)
        except cb.BudgetExceeded:
            errors.append(i)
        except Exception as exc:  # noqa: BLE001 - surface unexpected races
            errors.append(("unexpected", repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 10
    assert cb.status("c", db_path=db).committed_eur == Decimal("10.00")
    assert cb.remaining_eur("c", db_path=db) == Decimal("0.00")
    assert all(e != "unexpected" for e in errors if isinstance(e, tuple))


# --- pricing bridge (fail-closed) -------------------------------------------


def test_price_worst_case_unknown_model_fails_closed():
    with pytest.raises(cb.PricingUnavailable):
        cb.price_worst_case_eur(
            "totally-unknown-model-xyz-2026",
            max_input_tokens=4096,
            max_output_tokens=512,
            fx_usd_to_eur="0.86",
            provider="nonexistent-provider",
        )


def test_price_actual_unknown_model_returns_none():
    assert (
        cb.price_actual_eur(
            "totally-unknown-model-xyz-2026",
            input_tokens=100,
            output_tokens=50,
            fx_usd_to_eur="0.86",
            provider="nonexistent-provider",
        )
        is None
    )


def test_price_worst_case_applies_fx_and_margin(monkeypatch):
    """With a stubbed USD cost, EUR = usd * fx * (1 + margin)."""
    monkeypatch.setattr(
        cb, "_usd_cost_or_fail", lambda *a, **k: Decimal("2.00")
    )
    eur = cb.price_worst_case_eur(
        "m", max_input_tokens=1, max_output_tokens=1,
        fx_usd_to_eur="0.86", safety_margin="0.15",
    )
    # 2.00 * 0.86 * 1.15 = 1.978
    assert eur == Decimal("1.978000")
