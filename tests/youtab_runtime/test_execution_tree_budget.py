"""Shared execution-tree budget (WAVE-30H R5).

Proves the grant's ReasoningEnvelopeV2 becomes ONE durable budget for the whole
tree keyed on root_run_id: the root seeds it, children/retries debit the SAME
row (never a fresh budget), every dimension fails closed, and the atomic debit
holds under concurrent processes.
"""

from __future__ import annotations

import multiprocessing as mp
from datetime import UTC, datetime, timedelta

import pytest

from youtab_runtime import execution_tree_budget as tb

_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)


def _params(**over):
    base = dict(
        max_iterations=10,
        max_spawn_depth=3,
        max_concurrent_agents=2,
        max_total_tokens=1000,
        max_cost_micros=5000,
        max_retries=1,
        deadline_at=_DEADLINE,
    )
    base.update(over)
    return tb.TreeBudgetParams(**base)


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "tree_budget.db")


# ── pure adapters ────────────────────────────────────────────────────────────


def test_reasoning_to_tree_params_from_model_and_mapping():
    from youtab_runtime.contracts import ReasoningEnvelopeV2

    model = ReasoningEnvelopeV2(
        max_iterations=7, max_spawn_depth=2, max_concurrent_agents=3,
        max_total_tokens=500, max_cost_micros=200, max_retries=2,
        deadline_at=_DEADLINE,
    )
    p1 = tb.reasoning_to_tree_params(model)
    p2 = tb.reasoning_to_tree_params(
        {
            "max_iterations": 7, "max_spawn_depth": 2, "max_concurrent_agents": 3,
            "max_total_tokens": 500, "max_cost_micros": 200, "max_retries": 2,
            "deadline_at": _DEADLINE.isoformat(),
        }
    )
    assert p1 == p2
    assert p1.max_iterations == 7 and p1.max_cost_micros == 200


def test_reasoning_to_run_limits_maps_dimensionless_ceilings():
    from youtab_runtime.contracts import ReasoningEnvelopeV2

    limits = tb.reasoning_to_run_limits(
        ReasoningEnvelopeV2(
            max_iterations=9, max_spawn_depth=1, max_concurrent_agents=1,
            max_total_tokens=800, max_cost_micros=0, max_retries=3,
            deadline_at=_DEADLINE,
        )
    )
    assert limits.max_iterations == 9
    assert limits.max_total_tokens == 800
    assert limits.max_retries == 3
    assert limits.max_cost_eur is None  # cost stays in the tree's micros dimension


# ── seed idempotency ─────────────────────────────────────────────────────────


def test_open_tree_is_idempotent_children_do_not_reseed(db):
    tb.open_tree("root-1", _params(), db_path=db)
    tb.consume("root-1", iterations=4, tokens=100, db_path=db)
    # a child/retry re-opens with (say) a wider budget -> must be ignored
    snap = tb.open_tree("root-1", _params(max_iterations=9999, max_total_tokens=9_999_999), db_path=db)
    assert snap.iterations_used == 4  # counters preserved
    assert snap.max_iterations == 10  # ceiling NOT widened
    assert snap.max_total_tokens == 1000


# ── shared debit across the tree ─────────────────────────────────────────────


def test_children_share_the_same_remaining_budget(db):
    tb.open_tree("root-2", _params(max_iterations=10), db_path=db)
    tb.consume("root-2", iterations=6, db_path=db)   # root uses 6
    tb.consume("root-2", iterations=3, db_path=db)   # a child uses 3 -> 9 total
    with pytest.raises(tb.TreeBudgetExceeded) as ei:
        tb.consume("root-2", iterations=2, db_path=db)  # would be 11 > 10
    assert ei.value.dimension == "iterations"
    # the failed debit was not applied
    assert tb.snapshot("root-2", db_path=db).iterations_used == 9


@pytest.mark.parametrize(
    "kwargs,dim",
    [
        (dict(tokens=2000), "tokens"),
        (dict(cost_micros=6000), "cost_micros"),
        (dict(iterations=11), "iterations"),
    ],
)
def test_each_dimension_fails_closed(db, kwargs, dim):
    tb.open_tree("root-dim", _params(), db_path=db)
    with pytest.raises(tb.TreeBudgetExceeded) as ei:
        tb.consume("root-dim", db_path=db, **kwargs)
    assert ei.value.dimension == dim


def test_zero_cost_ceiling_denies_any_cost(db):
    tb.open_tree("root-zero", _params(max_cost_micros=0), db_path=db)
    with pytest.raises(tb.TreeBudgetExceeded):
        tb.consume("root-zero", cost_micros=1, db_path=db)


# ── spawn depth / concurrency / retries / deadline ──────────────────────────


def test_spawn_depth_ceiling(db):
    tb.open_tree("root-d", _params(max_spawn_depth=2), db_path=db)
    tb.enter_agent("root-d", depth=2, db_path=db)  # ok
    tb.exit_agent("root-d", db_path=db)
    with pytest.raises(tb.TreeDepthExceeded):
        tb.enter_agent("root-d", depth=3, db_path=db)


def test_concurrent_agent_ceiling(db):
    tb.open_tree("root-c", _params(max_concurrent_agents=2), db_path=db)
    tb.enter_agent("root-c", depth=1, db_path=db)
    tb.enter_agent("root-c", depth=1, db_path=db)  # 2 live
    with pytest.raises(tb.TreeConcurrencyExceeded):
        tb.enter_agent("root-c", depth=1, db_path=db)  # would be 3
    tb.exit_agent("root-c", db_path=db)
    tb.enter_agent("root-c", depth=1, db_path=db)  # slot freed -> ok


def test_retry_ceiling(db):
    tb.open_tree("root-r", _params(max_retries=1), db_path=db)
    tb.register_retry("root-r", db_path=db)  # 1st retry ok
    with pytest.raises(tb.TreeRetryExceeded):
        tb.register_retry("root-r", db_path=db)  # 2nd exceeds


def test_deadline(db):
    tb.open_tree("root-dl", _params(deadline_at=datetime(2020, 1, 1, tzinfo=UTC)), db_path=db)
    with pytest.raises(tb.TreeDeadlineExceeded):
        tb.check_deadline("root-dl", now=datetime(2020, 1, 2, tzinfo=UTC), db_path=db)
    # a not-yet-passed deadline is fine
    tb.open_tree("root-dl2", _params(deadline_at=_DEADLINE), db_path=db)
    tb.check_deadline("root-dl2", now=datetime(2029, 1, 1, tzinfo=UTC), db_path=db)


def test_consume_on_unopened_tree_fails_closed(db):
    with pytest.raises(tb.TreeBudgetError):
        tb.consume("never-opened", iterations=1, db_path=db)


# ── concurrency: atomic shared debit across processes ───────────────────────


def _worker(db_path, root, n):
    ok = 0
    for _ in range(n):
        try:
            tb.consume(root, iterations=1, db_path=db_path)
            ok += 1
        except tb.TreeBudgetExceeded:
            pass
    return ok


def test_multiprocess_debit_never_exceeds_ceiling(db):
    # 8 processes each try 50 single-iteration debits against a ceiling of 100:
    # the tree must grant EXACTLY 100 and no more, proving the debit is atomic
    # across processes (BEGIN IMMEDIATE), not per-process.
    tb.open_tree("root-mp", _params(max_iterations=100, max_total_tokens=10**9), db_path=db)
    ctx = mp.get_context("spawn")
    with ctx.Pool(8) as pool:
        granted = sum(pool.starmap(_worker, [(db, "root-mp", 50)] * 8))
    assert granted == 100
    assert tb.snapshot("root-mp", db_path=db).iterations_used == 100
