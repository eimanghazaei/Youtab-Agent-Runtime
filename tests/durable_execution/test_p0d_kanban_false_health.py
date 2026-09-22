"""P0-D reproduction — Kanban false health (liveness != progress) + breaker.

Durable-execution requirement: a live heartbeat without progress must eventually
produce a visible STALLED/BLOCKED state. Heartbeat (liveness) must be separable
from a monotonic progress marker / checkpoint digest.

This reproduction proves the CURRENT behavior on origin/main (0.19.1):
- heartbeat_worker only touches `last_heartbeat_at` (liveness); it advances NO
  progress/step/checkpoint field, so a stuck-but-chatty worker looks healthy
  indefinitely [C-2.2b — CONFIRMED defect].
- Separately, the crash/retry circuit breaker DOES work: consecutive failures
  trip the breaker at the limit and the card becomes `blocked` (dead-letter)
  [C-2.2c — report claim of infinite respawn DISPROVED/FIXED].
"""

import time
from pathlib import Path

import pytest

from youtab_agent_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _ready_running_task(conn):
    task_id = kb.create_task(conn, title="long enterprise task", body="x")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (task_id,))
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None, "task should claim ready->running"
    return task_id


def _cols(conn, task_id):
    row = conn.execute(
        "SELECT last_heartbeat_at, current_step_key, status FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    return {"last_heartbeat_at": row[0], "current_step_key": row[1], "status": row[2]}


def test_heartbeat_is_liveness_only_no_progress_marker(kanban_home):
    conn = kb.connect()
    try:
        task_id = _ready_running_task(conn)

        before = _cols(conn, task_id)
        time.sleep(1.1)  # ensure integer-second clock advances
        # Worker "heartbeats" repeatedly but makes ZERO progress.
        for _ in range(3):
            assert kb.heartbeat_worker(conn, task_id) is True
        after = _cols(conn, task_id)

        # Liveness DID advance (None before any heartbeat -> an int after)...
        assert (after["last_heartbeat_at"] or 0) >= (before["last_heartbeat_at"] or 0)
        assert after["last_heartbeat_at"] is not None
        # ...but there is NO progress marker: the step key never moved.
        assert after["current_step_key"] == before["current_step_key"]
        # The task still looks perfectly healthy ("running") despite no progress.
        assert after["status"] == "running"

        # DEFECT: heartbeat_worker exposes no progress sequence / checkpoint
        # digest to distinguish "alive" from "advancing".
        import inspect
        src = inspect.getsource(kb.heartbeat_worker)
        assert "last_heartbeat_at" in src
        assert "current_step_key" not in src  # heartbeat never advances progress
        assert "checkpoint" not in src.lower()
    finally:
        conn.close()


def test_circuit_breaker_blocks_after_failure_limit(kanban_home):
    conn = kb.connect()
    try:
        task_id = _ready_running_task(conn)
        # First failure: counter increments, not yet blocked.
        blocked1 = kb._record_task_failure(
            conn, task_id, "crash 1", outcome="crashed",
            failure_limit=2, release_claim=True, end_run=True,
        )
        assert blocked1 is False
        # Re-claim for a second attempt, then fail again → breaker trips.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (task_id,))
        kb.claim_task(conn, task_id)
        blocked2 = kb._record_task_failure(
            conn, task_id, "crash 2", outcome="crashed",
            failure_limit=2, release_claim=True, end_run=True,
        )
        assert blocked2 is True, "breaker must trip at the failure limit"
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        assert status == "blocked", "card must become blocked (dead-letter), not respawn forever"
    finally:
        conn.close()
