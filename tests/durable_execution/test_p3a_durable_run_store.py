"""Phase 3-A DESIRED-INVARIANT tests for the canonical DurableRunStore.

These assert the PRODUCTION invariants (they fail against the old in-memory
/v1/runs design, pass against the durable store): durable survival across
restart, reconnect-from-sequence, validated state machine, exactly-once terminal,
fenced concurrent claim, liveness!=progress + visible STALLED, and — the Owner
hard requirement — caller wait decoupled from task lifetime.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from youtab_runtime.durable_run_store import (
    AbsentEffectLedger,
    DurableRunStore,
    EffectLedger,
    IdempotencyConflict,
    InvalidTransition,
    RunIdentity,
    RunState,
)


def _identity(db_run_id="run-1", **kw):
    base = dict(
        task_id="task-1", run_id=db_run_id, tenant_id="t1", organization_id="o1",
        workspace_id="w1", principal_id="p1", agent_id="a1",
    )
    base.update(kw)
    return RunIdentity(**base)


@pytest.fixture
def store(tmp_path):
    return DurableRunStore(db_path=tmp_path / "durable_runs.db")


def test_create_is_durable_and_idempotent(tmp_path):
    db = tmp_path / "d.db"
    s = DurableRunStore(db_path=db)
    row = s.create_run(_identity(idempotency_key="k1", request_digest="d1"))
    assert row["state"] == "QUEUED"
    # same key + same digest -> same run (idempotent)
    again = s.create_run(_identity(idempotency_key="k1", request_digest="d1"))
    assert again["run_id"] == row["run_id"]
    # same key + different digest -> fail closed
    with pytest.raises(IdempotencyConflict):
        s.create_run(_identity(db_run_id="run-2", idempotency_key="k1", request_digest="d2"))


def test_transition_validation_and_terminal_immutability(store):
    store.create_run(_identity())
    store.claim("run-1", "worker-a")
    store.transition("run-1", RunState.RUNNING)
    store.transition("run-1", RunState.SUCCEEDED, result_ref="r://1")
    # terminal is immutable
    with pytest.raises(InvalidTransition):
        store.transition("run-1", RunState.RUNNING)


def test_events_monotonic_and_reconnect_from_seq(store):
    store.create_run(_identity())
    store.claim("run-1", "worker-a")
    store.transition("run-1", RunState.RUNNING)
    for i in range(5):
        store.record_progress("run-1", step=f"s{i}")
    all_events = store.get_events("run-1", from_seq=0)
    seqs = [e["seq"] for e in all_events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "monotonic, unique"
    # reconnect from a mid cursor -> only later events, no dup/miss
    mid = seqs[3]
    tail = store.get_events("run-1", from_seq=mid)
    assert [e["seq"] for e in tail] == [s for s in seqs if s > mid]
    # two simultaneous consumers see identical replay
    c1 = store.get_events("run-1", from_seq=0)
    c2 = store.get_events("run-1", from_seq=0)
    assert [e["event_id"] for e in c1] == [e["event_id"] for e in c2]


def test_durable_survives_restart(tmp_path):
    db = tmp_path / "d.db"
    s1 = DurableRunStore(db_path=db)
    s1.create_run(_identity())
    s1.claim("run-1", "worker-a")
    s1.transition("run-1", RunState.RUNNING)
    s1.record_progress("run-1", step="halfway", checkpoint_ref="ckpt://1")
    s1.transition("run-1", RunState.SUCCEEDED, result_ref="r://done")
    del s1
    # "restart": brand-new store instance on the same DB file.
    s2 = DurableRunStore(db_path=db)
    row = s2.get_run("run-1")
    assert row is not None and row["state"] == "SUCCEEDED"
    assert row["result_ref"] == "r://done"
    assert row["checkpoint_ref"] == "ckpt://1"
    # events replay after restart
    ev = s2.get_events("run-1", from_seq=0)
    kinds = [e["kind"] for e in ev]
    assert "accepted" in kinds and "state.succeeded" in kinds
    assert sum(1 for k in kinds if k == "state.succeeded") == 1, "exactly-once terminal"


def test_concurrent_claim_exactly_one_winner(tmp_path):
    import threading
    db = tmp_path / "d.db"
    s = DurableRunStore(db_path=db)
    s.create_run(_identity())
    epochs = []
    barrier = threading.Barrier(6)

    def _claim(i):
        cs = DurableRunStore(db_path=db)
        barrier.wait()
        e = cs.claim("run-1", f"worker-{i}")
        if e is not None:
            epochs.append(e)

    ts = [threading.Thread(target=_claim, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert len(epochs) == 1, f"exactly one claimer wins; got {epochs}"


def test_heartbeat_is_liveness_only_and_stall_is_visible(store):
    store.create_run(_identity())
    epoch = store.claim("run-1", "worker-a")
    store.transition("run-1", RunState.RUNNING)
    before = store.get_run("run-1")["progress_seq"]
    # Heartbeats prove liveness but MUST NOT advance progress.
    for _ in range(3):
        assert store.heartbeat("run-1", "worker-a", epoch) is True
    assert store.get_run("run-1")["progress_seq"] == before
    # A stale epoch cannot renew the lease (fencing).
    assert store.heartbeat("run-1", "worker-a", epoch - 1) is False
    # Live but no progress -> STALLED (visible, recoverable), not killed.
    time.sleep(0.05)
    stalled = store.detect_stalled(stall_threshold=0.01, liveness_window=3600.0)
    assert "run-1" in stalled
    assert store.get_run("run-1")["state"] == "STALLED"


def test_wait_timeout_never_kills_run(store):
    store.create_run(_identity())
    store.claim("run-1", "worker-a")
    store.transition("run-1", RunState.RUNNING)
    store.record_progress("run-1", step="s0")
    wr = store.wait_for_terminal("run-1", wait_timeout=0.3)
    # Caller wait ended, run is NOT terminal, typed identity + reconnect returned.
    assert wr.terminal is False
    assert wr.state == "RUNNING"
    assert wr.task_id == "task-1" and wr.run_id == "run-1"
    assert wr.last_progress_seq >= 1
    assert "from_seq=" in wr.reconnect
    # The run still exists and is untouched by the wait timeout.
    assert store.get_run("run-1")["state"] == "RUNNING"


def test_explicit_cancel_is_the_only_caller_stop(store):
    store.create_run(_identity())
    store.claim("run-1", "worker-a")
    store.transition("run-1", RunState.RUNNING)
    row = store.request_cancel("run-1", reason="user asked", by="user:eiman")
    assert row["state"] == "CANCELLING"
    assert row["cancel_requested_at"] is not None


def test_lane1_absent_ledger_is_fail_closed():
    led = AbsentEffectLedger()
    assert isinstance(led, EffectLedger)  # satisfies the typed Protocol
    # unknown -> reconcile, never a fabricated 'committed'/'absent'
    assert led.lookup("eff-1")["status"] == "unknown"
    with pytest.raises(Exception):
        led.begin_effect("eff-1", "p1", "scope-digest")


# --------------------------------------------------------------------------- #
# OWNER HARD-REQUIREMENT ACCEPTANCE TEST — real cross-process, wait != lifetime
# --------------------------------------------------------------------------- #
_WORKER = r'''
import os, sys, time
from youtab_runtime.durable_run_store import DurableRunStore, RunState
db, run_id, owner, pidfile = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
open(pidfile, "w").write(str(os.getpid()))
s = DurableRunStore(db)
epoch = s.claim(run_id, owner)
s.transition(run_id, RunState.RUNNING)
# A long task, compressed: 20 real progress steps across ~4s cross-process.
for i in range(20):
    s.record_progress(run_id, step="step-%d" % i)
    s.heartbeat(run_id, owner, epoch)
    time.sleep(0.2)
s.transition(run_id, RunState.SUCCEEDED, result_ref="result://exactly-once")
'''


def _alive(pid: int) -> bool:
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


def test_acceptance_long_task_survives_short_wait_and_completes_once(tmp_path):
    db = tmp_path / "durable_runs.db"
    pidfile = tmp_path / "worker.pid"
    store = DurableRunStore(db_path=db)
    store.create_run(_identity())

    worker = subprocess.Popen([sys.executable, "-c", _WORKER, str(db), "run-1", "worker-x", str(pidfile)])
    try:
        # Wait for the worker to register a pid.
        deadline = time.time() + 15
        while time.time() < deadline and not pidfile.exists():
            time.sleep(0.05)
        assert pidfile.exists(), "worker never started"
        wpid = int(pidfile.read_text().strip())

        # Caller waits only 2s for a task that needs ~4s.
        wr = store.wait_for_terminal("run-1", wait_timeout=2.0)
        assert wr.terminal is False, "wait must return before the task finishes"
        assert wr.state in ("CLAIMED", "RUNNING")
        assert wr.task_id == "task-1" and wr.run_id == "run-1"

        # The worker is STILL ALIVE — the short wait did not kill it.
        assert _alive(wpid), "wait timeout must NOT kill the durable worker"

        # Reconnect from another client: monotonic progress is observable.
        ev1 = store.get_events("run-1", from_seq=wr.last_progress_seq)
        time.sleep(0.6)
        ev2 = store.get_events("run-1", from_seq=wr.last_progress_seq)
        assert len(ev2) >= len(ev1) and len(ev2) > 0, "monotonic progress after reconnect"

        # "UI restart": a brand-new store handle on the same DB still sees the run.
        restarted = DurableRunStore(db_path=db)
        assert restarted.get_run("run-1") is not None

        # Let it complete; observe exactly-once terminal + final result.
        final = store.wait_for_terminal("run-1", wait_timeout=15.0)
        assert final.terminal is True and final.state == "SUCCEEDED"
        assert final.result_ref == "result://exactly-once"
        succ = [e for e in store.get_events("run-1", from_seq=0) if e["kind"] == "state.succeeded"]
        assert len(succ) == 1, "exactly-once terminal event"

        # Worker exits cleanly — zero orphans.
        assert worker.wait(timeout=10) == 0
        assert not _alive(wpid), "no orphan worker process"
    finally:
        if worker.poll() is None:
            worker.kill()
