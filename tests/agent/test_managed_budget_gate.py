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


# ── SEC-9 #3: accounting failures fail CLOSED ────────────────────────────────


def test_debit_tokens_infra_failure_returns_durable_stop(monkeypatch):
    import sqlite3

    tb.open_tree("run-f3a", _params(max_total_tokens=100))
    agent = _Agent(root="run-f3a")

    def _boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tb, "consume", _boom)
    # A non-budget infra failure must NOT propagate/​swallow — it returns a durable
    # stop so the loop cannot make another provider call.
    assert mbg.execution_tree_debit_tokens(agent, input_tokens=5, output_tokens=5) == "tree_budget_error"


def test_pre_iteration_infra_failure_fails_closed(monkeypatch):
    tb.open_tree("run-f3b", _params())
    agent = _Agent(root="run-f3b")

    def _boom(*a, **k):
        raise RuntimeError("io error")

    monkeypatch.setattr(tb, "consume", _boom)
    assert mbg.execution_tree_pre_iteration(agent) == "tree_budget_error"


# ── SEC-9 #2: monetary ceiling enforced against real provider usage ──────────


def _cost_agent(root, *, provider=None, base_url=None, model="m", api_key=""):
    a = _Agent(root=root)
    a.provider = provider
    a.base_url = base_url
    a.model = model
    a.api_key = api_key
    return a


def test_cost_precheck_local_zero_allowed():
    # ollama @ loopback is a verified local-zero engine: allowed even at max_cost=0.
    tb.open_tree("run-lz", _params(max_cost_micros=0))
    agent = _cost_agent("run-lz", provider="ollama", base_url="http://127.0.0.1:11434")
    assert mbg.execution_tree_cost_precheck(agent) is None


def test_cost_precheck_zero_budget_blocks_known_paid(monkeypatch):
    from agent import usage_pricing as up

    tb.open_tree("run-zp", _params(max_cost_micros=0))
    agent = _cost_agent("run-zp", provider="openai", base_url="https://api.openai.com/v1")
    monkeypatch.setattr(up, "classify_local_zero", lambda *a, **k: False)
    monkeypatch.setattr(up, "has_known_pricing", lambda *a, **k: True)
    monkeypatch.setattr(up, "resolve_billing_route",
                        lambda *a, **k: type("R", (), {"billing_mode": "metered"})())
    # max_cost_micros == 0 + a KNOWN-PAID route => no paid call permitted.
    assert mbg.execution_tree_cost_precheck(agent) == "tree_cost_micros"


def test_cost_precheck_zero_budget_allows_unpriced_route(monkeypatch):
    from agent import usage_pricing as up

    tb.open_tree("run-zu", _params(max_cost_micros=0))
    agent = _cost_agent("run-zu", provider="local", model="local-deterministic")
    monkeypatch.setattr(up, "classify_local_zero", lambda *a, **k: False)
    monkeypatch.setattr(up, "has_known_pricing", lambda *a, **k: False)
    monkeypatch.setattr(up, "resolve_billing_route",
                        lambda *a, **k: type("R", (), {"billing_mode": "metered"})())
    # An unpriced/local route costs nothing; a "spend nothing" grant allows it.
    assert mbg.execution_tree_cost_precheck(agent) is None


def test_cost_precheck_positive_budget_unpriced_fails_closed(monkeypatch):
    from agent import usage_pricing as up

    tb.open_tree("run-pu", _params(max_cost_micros=10_000))
    agent = _cost_agent("run-pu", provider="mystery", base_url="https://api.example.com")
    monkeypatch.setattr(up, "classify_local_zero", lambda *a, **k: False)
    monkeypatch.setattr(up, "has_known_pricing", lambda *a, **k: False)
    monkeypatch.setattr(up, "resolve_billing_route",
                        lambda *a, **k: type("R", (), {"billing_mode": "metered"})())
    # A euro ceiling is set but the route cannot be priced => cannot enforce => stop.
    assert mbg.execution_tree_cost_precheck(agent) == "tree_cost_pricing_unavailable"


def test_cost_precheck_positive_budget_exhausted_blocks(monkeypatch):
    from agent import usage_pricing as up

    tb.open_tree("run-pe", _params(max_cost_micros=1_000))
    agent = _cost_agent("run-pe", provider="openai", base_url="https://api.openai.com/v1")
    monkeypatch.setattr(up, "classify_local_zero", lambda *a, **k: False)
    monkeypatch.setattr(up, "has_known_pricing", lambda *a, **k: True)
    monkeypatch.setattr(up, "resolve_billing_route",
                        lambda *a, **k: type("R", (), {"billing_mode": "metered"})())
    tb.consume("run-pe", cost_micros=1_000)  # exhaust the ceiling
    assert mbg.execution_tree_cost_precheck(agent) == "tree_cost_micros"


def test_cost_debit_within_budget_records_micros():
    tb.open_tree("run-cd", _params(max_cost_micros=1_000_000))
    agent = _Agent(root="run-cd")
    assert mbg.execution_tree_debit_cost(agent, amount_usd=0.5) is None
    assert tb.snapshot("run-cd").cost_micros_used == 500_000


def test_cost_debit_overflow_saturates_and_stops():
    tb.open_tree("run-co", _params(max_cost_micros=1_000))
    agent = _Agent(root="run-co")
    assert mbg.execution_tree_debit_cost(agent, amount_usd=0.5) == "tree_cost_micros"
    assert tb.snapshot("run-co").cost_micros_used == 1_000  # saturated, never beyond


def test_cost_debit_none_and_zero_are_noops():
    tb.open_tree("run-cn", _params(max_cost_micros=1_000))
    agent = _Agent(root="run-cn")
    assert mbg.execution_tree_debit_cost(agent, amount_usd=None) is None
    assert mbg.execution_tree_debit_cost(agent, amount_usd=0.0) is None
    assert tb.snapshot("run-cn").cost_micros_used == 0


def test_cost_debit_conservative_ceil_rounding():
    tb.open_tree("run-cr", _params(max_cost_micros=1_000_000))
    agent = _Agent(root="run-cr")
    # 0.0000001 USD => 0.1 micro => ceil to 1 micro (never rounds down to 0).
    assert mbg.execution_tree_debit_cost(agent, amount_usd=0.0000001) is None
    assert tb.snapshot("run-cr").cost_micros_used == 1


def test_cost_debit_infra_failure_fails_closed(monkeypatch):
    tb.open_tree("run-ci", _params(max_cost_micros=1_000_000))
    agent = _Agent(root="run-ci")

    def _boom(*a, **k):
        raise RuntimeError("io")

    monkeypatch.setattr(tb, "consume", _boom)
    assert mbg.execution_tree_debit_cost(agent, amount_usd=0.1) == "tree_budget_error"


def test_cost_gate_noop_for_non_managed():
    agent = _cost_agent(None, provider="openai", base_url="https://api.openai.com/v1")
    assert mbg.execution_tree_cost_precheck(agent) is None
    assert mbg.execution_tree_debit_cost(agent, amount_usd=1.0) is None


def test_delegation_permit_records_incarnation_for_reaping():
    tb.open_tree("run-perm5", _params(max_concurrent_agents=1))
    a = _Agent(root="run-perm5", subagent_id="sa-a")
    mbg.acquire_delegation_permit(a)
    # a crash skips release; the reaper reclaims via the recorded live incarnation
    from youtab_runtime import process_incarnation as pi

    reclaimed = tb.reap_dead_agents("run-perm5", is_alive=pi.is_alive_incarnation)
    assert reclaimed == []  # THIS process is alive -> not reclaimed
    assert tb.active_agent_count("run-perm5") == 1
