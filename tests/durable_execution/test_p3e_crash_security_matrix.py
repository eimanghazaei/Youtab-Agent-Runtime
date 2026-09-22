"""P3 crash/security matrix + execution-deadline enforcement (shipped RunStore).

Includes Codex's two precise crash-injection points:
 1. failure after create_run/claim but BEFORE admission completes — the persisted
    state is recoverable and can never be mistaken for a running effectful child;
 2. parent death AFTER admission commits but BEFORE gate release — the run is
    discoverable and reconciled to UNKNOWN, without claiming the child survived or
    that an external effect occurred.

Plus: concurrent claim / fenced takeover, same-key replay vs changed-payload
conflict, explicit cancel, and ACTIVE execution-deadline enforcement (not just a
persisted column). Lane-1-dependent effect reconciliation stays BLOCKED
(fail-closed) until its accepted contract is integrated.
"""

import threading
import time
from pathlib import Path

import pytest

from youtab_runtime.durable_run_store import (
    AbsentEffectLedger,
    DurableRunStore,
    IdempotencyConflict,
    RunIdentity,
    RunState,
    create_run_store,
)


@pytest.fixture
def youtab_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    return home


def _ident(run_id="r", **kw):
    base = dict(task_id=run_id, run_id=run_id, tenant_id="t", organization_id="o",
                workspace_id="w", principal_id="p", agent_id="a", operation="delegate")
    base.update(kw)
    return RunIdentity(**base)


# ---- Crash injection point 1: fail after create_run, before claim/RUNNING ---- #
def test_crash_after_create_before_admission_is_recoverable_not_running(youtab_home):
    store = DurableRunStore(db_path=Path(store_db(youtab_home)))
    # Simulate the partial state: create_run committed, but claim/RUNNING never
    # happened (admission aborted, e.g. DB error or parent death mid-admission).
    row = store.create_run(_ident("c1"))
    assert row["state"] == "QUEUED", "created but not admitted -> QUEUED, never RUNNING"
    assert row["lease_owner"] is None
    # It cannot be mistaken for a running effectful child.
    assert store.get_run("c1")["state"] != "RUNNING"
    # A recovery sweep reconciles the abandoned, ownerless run to UNKNOWN.
    reconciled = store.reconcile_dead_owner(lambda pid: True)  # owner is None -> swept
    assert "c1" in reconciled
    assert store.get_run("c1")["state"] == "UNKNOWN"


# ---- Crash injection point 2: parent death after commit, before gate release -- #
def test_crash_after_admission_before_gate_reconciles_unknown_no_effect(youtab_home):
    store = DurableRunStore(db_path=Path(store_db(youtab_home)))
    store.create_run(_ident("c2"))
    store.claim("c2", owner="pid:999999")          # fenced ownership acquired
    store.set_state("c2", RunState.RUNNING, strict=False, kind="delegate.admitted")
    # Parent dies here, BEFORE releasing the start gate: the child never ran.
    reconciled = store.reconcile_dead_owner(lambda pid: False)
    assert "c2" in reconciled
    row = store.get_run("c2")
    assert row["state"] == "UNKNOWN", "discoverable + reconciled, not lost"
    # Must NOT claim the child survived / completed / produced an effect.
    assert row["result_ref"] is None
    assert row["state"] not in ("SUCCEEDED", "RUNNING")


# ---- Concurrent claim / fenced takeover ---- #
def test_concurrent_claim_single_winner_fenced(youtab_home):
    db = store_db(youtab_home)
    DurableRunStore(db_path=Path(db)).create_run(_ident("cc"))
    winners = []
    barrier = threading.Barrier(6)

    def _c(i):
        s = DurableRunStore(db_path=Path(db))
        barrier.wait()
        e = s.claim("cc", owner=f"pid:{1000+i}")
        if e is not None:
            winners.append(e)

    ts = [threading.Thread(target=_c, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert len(winners) == 1
    # A stale epoch cannot heartbeat (fencing).
    s = DurableRunStore(db_path=Path(db))
    assert s.heartbeat("cc", "pid:1000", winners[0] - 1) is False


# ---- Same-key replay vs changed-payload conflict ---- #
def test_same_key_replay_vs_conflict(youtab_home):
    s = DurableRunStore(db_path=Path(store_db(youtab_home)))
    a = s.create_run(_ident("k1", idempotency_key="K", request_digest="D1"))
    b = s.create_run(_ident("k2", idempotency_key="K", request_digest="D1"))
    assert a["run_id"] == b["run_id"], "same key+digest replays original"
    with pytest.raises(IdempotencyConflict):
        s.create_run(_ident("k3", idempotency_key="K", request_digest="D2"))


# ---- Explicit cancel (only caller-driven stop) ---- #
def test_explicit_cancel(youtab_home):
    s = DurableRunStore(db_path=Path(store_db(youtab_home)))
    s.create_run(_ident("cx"))
    s.claim("cx", owner="pid:1")
    s.transition("cx", RunState.RUNNING)
    row = s.request_cancel("cx", reason="user", by="principal:p")
    assert row["state"] == "CANCELLING" and row["cancel_requested_at"] is not None


# ---- ACTIVE execution-deadline enforcement (independent of wait) ---- #
def test_execution_deadline_is_actively_enforced(youtab_home):
    s = DurableRunStore(db_path=Path(store_db(youtab_home)))
    s.create_run(_ident("dl", execution_deadline=time.time() - 1))  # already past
    s.claim("dl", owner="pid:1")
    s.transition("dl", RunState.RUNNING)
    enforced = s.enforce_execution_deadlines()
    assert "dl" in enforced
    row = s.get_run("dl")
    assert row["state"] == "CANCELLING"
    assert row["cancel_reason"] == "execution_deadline_reached"
    # A run with a future deadline is untouched (deadline != wait).
    s.create_run(_ident("dl2", execution_deadline=time.time() + 3600))
    s.claim("dl2", owner="pid:1")
    s.transition("dl2", RunState.RUNNING)
    assert "dl2" not in s.enforce_execution_deadlines()


# ---- Lane-1 effect reconciliation: BLOCKED / fail-closed ---- #
def test_lane1_effect_reconciliation_is_blocked_fail_closed(youtab_home):
    """UNKNOWN-after-effect reconciliation requires the canonical Lane-1 effect
    ledger, which is NOT integrated on this base. The boundary is fail-closed:
    every lookup is UNKNOWN (never a fabricated committed/absent) and begin/commit
    raise — so no blind retry and no second ledger. Reported BLOCKED, not passed."""
    led = AbsentEffectLedger()
    assert led.lookup("eff")["status"] == "unknown"
    with pytest.raises(Exception):
        led.begin_effect("eff", "p", "scope")
    with pytest.raises(Exception):
        led.commit_effect("eff", {"r": 1})
    pytest.skip("BLOCKED: Lane-1 effect-ledger contract not integrated on this base "
                "(fail-closed). Effect UNKNOWN/reconciliation cannot be proven here.")


def store_db(home):
    return str(Path(home) / "durable_runs.db")
