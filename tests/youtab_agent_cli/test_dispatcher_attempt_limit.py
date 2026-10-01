"""WAVE-30D dispatcher worker-attempt semantics (canary respawn defect).

Live canary ``t_f6ef707c`` exposed that a per-run ``max_retries=0`` (no retry)
did NOT bound the dispatcher's per-task circuit breaker: a timed-out worker was
silently respawned (journal ``claimed→spawned→timed_out→claimed→spawned→
timed_out→gave_up`` = TWO attempts), and the second worker could issue a second
model request even though the run declared ``max_requests=1``.

The runtime now maps the authoritative per-run retry budget onto
``tasks.max_retries`` (total attempts = ``1 + RunLimits.max_retries``), which is
the failure count at which the breaker trips. These tests pin the DISPATCHER
axis: the exact number of worker attempts a FAILURE (timeout / crash /
spawn-failure / protocol-violation) is allowed to produce, since every one of
those respawn paths resolves ``tasks.max_retries`` with top precedence.

Scope note: the per-task bound governs failure-driven respawns. TTL claim
reclamation (``release_stale_claims``) is a separate LIVENESS path — it does not
count a failure or consult ``max_retries`` — but it cannot fire for the canary:
the claim TTL (900 s) far exceeds the canary ``max_runtime_seconds`` (60 s), so
``enforce_max_runtime`` always trips first and a live worker's claim is extended,
never reclaimed.

The complementary MODEL-request / retry axis (a single worker's own provider
retries and ``max_requests`` gate) is proven in
``tests/youtab_runtime/test_run_limits.py`` — ``test_max_retries_zero_means_no_
retry_budget`` (retry_multiplier == 1) and ``test_max_requests_stop`` (no second
model request). Together the two files prove ``max_retries=0`` disables BOTH a
provider-call retry AND a dispatcher worker respawn.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from youtab_agent_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _spawn_once(conn, tid):
    """Claim the ready task + attach a live worker pid (one dispatch spawn)."""
    import os

    claimed = kb.claim_task(conn, tid)
    assert claimed is not None, "expected the ready task to be claimable"
    kb._set_worker_pid(conn, tid, os.getpid())
    return claimed


def _force_timeout(conn, tid, monkeypatch):
    """Backdate the active attempt past its runtime cap and run the reaper.

    Returns the list of timed-out task ids from ``enforce_max_runtime``.
    """
    old_started = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old_started, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old_started, tid),
        )
    # SIGTERM "succeeds" immediately so the reaper doesn't poll a real process.
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    killed: list = []
    return kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))


def test_zero_retry_gives_up_after_single_timeout_no_respawn(kanban_home, monkeypatch):
    """``tasks.max_retries=1`` (the canary mapping of run ``max_retries=0``): the
    breaker trips on the FIRST timeout — the task ends ``blocked``/``gave_up`` and
    is NOT reset to ``ready``, so no second worker can ever be claimed/spawned."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="canary", assignee="worker",
            max_runtime_seconds=1, max_retries=1,  # 1 attempt, zero retries
        )
        _spawn_once(conn, tid)  # attempt #1
        timed_out = _force_timeout(conn, tid, monkeypatch)
        assert tid in timed_out

        task = kb.get_task(conn, tid)
        # No silent respawn: the task is parked blocked, NOT back to ready.
        assert task.status == "blocked", f"expected blocked, got {task.status!r}"
        assert task.worker_pid is None

        kinds = [e.kind for e in kb.list_events(conn, tid)]
        # Timeout stays an honest failure AND the breaker records giving up.
        assert "timed_out" in kinds
        assert "gave_up" in kinds
        # It never masqueraded as success.
        assert "completed" not in kinds

        # A blocked task is not claimable: a subsequent dispatch cannot spawn #2.
        assert kb.claim_task(conn, tid) is None
    finally:
        conn.close()


def test_one_retry_allows_exactly_one_respawn_then_gives_up(kanban_home, monkeypatch):
    """``tasks.max_retries=2`` (pilot/full mapping of run ``max_retries=1``): the
    first timeout resets to ``ready`` (respawn allowed), the SECOND trips the
    breaker — exactly two worker attempts, then ``gave_up``."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="pilot", assignee="worker",
            max_runtime_seconds=1, max_retries=2,  # 2 attempts, one retry
        )
        # Attempt #1 → timeout → below threshold → ready (respawn permitted).
        _spawn_once(conn, tid)
        assert tid in _force_timeout(conn, tid, monkeypatch)
        assert kb.get_task(conn, tid).status == "ready"

        # Attempt #2 → timeout → threshold reached → blocked + gave_up.
        _spawn_once(conn, tid)
        assert tid in _force_timeout(conn, tid, monkeypatch)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"

        gave_up = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]
        assert len(gave_up) == 1
        assert int(task.consecutive_failures) == 2  # exactly two failed attempts
    finally:
        conn.close()


def test_no_retry_budget_preserves_dispatcher_default(kanban_home, monkeypatch):
    """A task with NO per-task ``max_retries`` (ordinary run / Track B) keeps the
    dispatcher's ``DEFAULT_FAILURE_LIMIT`` (2): the first timeout still resets to
    ``ready`` — the fix does not change unbounded-budget recovery."""
    assert kb.DEFAULT_FAILURE_LIMIT == 2
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="ordinary", assignee="worker",
            max_runtime_seconds=1,  # max_retries left NULL
        )
        assert kb.get_task(conn, tid).max_retries is None
        _spawn_once(conn, tid)
        assert tid in _force_timeout(conn, tid, monkeypatch)
        # Default limit (2) not yet reached after the first failure → recover.
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()
