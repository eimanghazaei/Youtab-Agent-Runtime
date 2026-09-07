"""Managed execution-tree loop gate (WAVE-30H R5 wiring).

Unit-tests the loop-facing helpers without driving the whole conversation loop:
the gate is a no-op for non-managed runs and fail-closed for managed ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent import managed_budget_gate as mbg
from youtab_runtime import execution_tree_budget as tb


class _Agent:
    def __init__(self, root=None, subagent_id=None, depth=1):
        if root is not None:
            self._execution_tree_root = root
        self._subagent_id = subagent_id
        self._delegate_depth = depth


def _params(**over):
    base = dict(
        max_iterations=2, max_spawn_depth=2, max_concurrent_agents=2,
        max_total_tokens=100, max_cost_micros=0, max_retries=0,
        deadline_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    base.update(over)
    return tb.TreeBudgetParams(**base)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    yield


# ── no-op for non-managed runs ───────────────────────────────────────────────


def test_non_managed_agent_is_noop():
    agent = _Agent(root=None)
    assert mbg.execution_tree_pre_iteration(agent) is None
    assert mbg.execution_tree_debit_tokens(agent, input_tokens=5, output_tokens=5) is None


# ── iteration gate ───────────────────────────────────────────────────────────


def test_pre_iteration_consumes_shared_iterations_then_stops():
    tb.open_tree("run-x", _params(max_iterations=2))
    agent = _Agent(root="run-x")
    assert mbg.execution_tree_pre_iteration(agent) is None  # used 1
    assert mbg.execution_tree_pre_iteration(agent) is None  # used 2
    assert mbg.execution_tree_pre_iteration(agent) == "tree_iterations"  # over
    assert tb.snapshot("run-x").iterations_used == 2


def test_deadline_stops_before_consuming():
    tb.open_tree("run-dl", _params(deadline_at=datetime(2020, 1, 1, tzinfo=UTC)))
    agent = _Agent(root="run-dl")
    # A now past the deadline -> stop; use monkeypatch-free explicit now via etb
    stop = mbg.execution_tree_pre_iteration(agent, now=datetime(2020, 6, 1, tzinfo=UTC))
    assert stop == "tree_deadline"
    assert tb.snapshot("run-dl").iterations_used == 0  # no iteration consumed


def test_carried_token_stop_is_honoured_at_loop_top():
    tb.open_tree("run-c", _params())
    agent = _Agent(root="run-c")
    agent._tree_token_stop = "tree_tokens"
    assert mbg.execution_tree_pre_iteration(agent) == "tree_tokens"


def test_missing_tree_row_fails_closed():
    agent = _Agent(root="never-opened")
    assert mbg.execution_tree_pre_iteration(agent) == "tree_budget_error"


# ── token debit ──────────────────────────────────────────────────────────────


def test_token_debit_within_budget_returns_none():
    tb.open_tree("run-t", _params(max_total_tokens=100))
    agent = _Agent(root="run-t")
    assert mbg.execution_tree_debit_tokens(agent, input_tokens=30, output_tokens=20) is None
    assert tb.snapshot("run-t").tokens_used == 50


def test_token_debit_overflow_saturates_and_signals_stop():
    tb.open_tree("run-o", _params(max_total_tokens=40))
    agent = _Agent(root="run-o")
    stop = mbg.execution_tree_debit_tokens(agent, input_tokens=30, output_tokens=30)
    assert stop == "tree_tokens"
    # saturated at the ceiling, never beyond
    assert tb.snapshot("run-o").tokens_used == 40


def test_zero_tokens_is_noop():
    tb.open_tree("run-z", _params())
    agent = _Agent(root="run-z")
    assert mbg.execution_tree_debit_tokens(agent, input_tokens=0, output_tokens=0) is None
    assert tb.snapshot("run-z").tokens_used == 0


# ── delegation concurrency permit ────────────────────────────────────────────


def test_delegation_permit_noop_for_non_managed():
    agent = _Agent(root=None, subagent_id="sa-1")
    mbg.acquire_delegation_permit(agent)  # no raise, no state
    assert getattr(agent, "_delegation_permit", None) is None
    mbg.release_delegation_permit(agent)  # safe no-op


def test_delegation_permit_acquire_and_release():
    tb.open_tree("run-perm", _params(max_concurrent_agents=2))
    a = _Agent(root="run-perm", subagent_id="sa-a")
    mbg.acquire_delegation_permit(a)
    assert tb.active_agent_count("run-perm") == 1
    assert a._delegation_permit == ("run-perm", "sa-a")
    mbg.release_delegation_permit(a)
    assert tb.active_agent_count("run-perm") == 0
    assert a._delegation_permit is None


def test_delegation_permit_release_is_idempotent():
    tb.open_tree("run-perm2", _params())
    a = _Agent(root="run-perm2", subagent_id="sa-a")
    mbg.acquire_delegation_permit(a)
    mbg.release_delegation_permit(a)
    mbg.release_delegation_permit(a)  # no underflow / no raise
    assert tb.active_agent_count("run-perm2") == 0


def test_delegation_permit_refused_past_ceiling():
    tb.open_tree("run-perm3", _params(max_concurrent_agents=1))
    a = _Agent(root="run-perm3", subagent_id="sa-a")
    b = _Agent(root="run-perm3", subagent_id="sa-b")
    mbg.acquire_delegation_permit(a)
    with pytest.raises(tb.TreeConcurrencyExceeded):
        mbg.acquire_delegation_permit(b)
    assert tb.active_agent_count("run-perm3") == 1  # b did not leak a permit


def test_delegation_permit_refused_past_depth():
    tb.open_tree("run-perm4", _params(max_spawn_depth=1))
    a = _Agent(root="run-perm4", subagent_id="sa-a", depth=2)
    with pytest.raises(tb.TreeDepthExceeded):
        mbg.acquire_delegation_permit(a)
    assert tb.active_agent_count("run-perm4") == 0


def test_delegation_permit_records_incarnation_for_reaping():
    tb.open_tree("run-perm5", _params(max_concurrent_agents=1))
    a = _Agent(root="run-perm5", subagent_id="sa-a")
    mbg.acquire_delegation_permit(a)
    # a crash skips release; the reaper reclaims via the recorded live incarnation
    from youtab_runtime import process_incarnation as pi

    reclaimed = tb.reap_dead_agents("run-perm5", is_alive=pi.is_alive_incarnation)
    assert reclaimed == []  # THIS process is alive -> not reclaimed
    assert tb.active_agent_count("run-perm5") == 1
