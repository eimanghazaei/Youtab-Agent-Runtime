"""P5 — a non-durable terminal completion is never exposed as a durable result.

Scenario: a run is admitted and persisted (QUEUED/RUNNING mirror to the store
succeeds), then the durable store fails exactly at the TERMINAL write (e.g.
PostgreSQL lost after admission but before the result is persisted). The
best-effort mirror swallows the write error, so without care a client could poll
status=completed with output — a "durable completed result" that then DISAPPEARS
after a restart (in-memory only, never in the store).

The fix: in server durable mode a terminal state whose durable write fails is
reported as reconciliation_required (terminal=false, durable=false), and the
store is left at its last durably-persisted non-terminal state — so a restart
that reads the store shows the non-terminal truth, and the client never observed
a durable completion that vanished.
"""

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


class _TerminalFailStore:
    """Persists non-terminal states; raises on the terminal write (PG lost)."""

    TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}

    def __init__(self):
        self.rows = {}

    def get_run(self, run_id):
        return self.rows.get(run_id)

    def create_run(self, identity, **k):
        rid = identity.run_id
        self.rows[rid] = {
            "state": "QUEUED", "created_at": 0.0, "updated_at": 0.0,
            "principal_id": "local", "result_ref": None, "error_ref": None,
        }
        return self.rows[rid]

    def set_state(self, run_id, to_state, *, strict=False, result_ref=None,
                  error_ref=None, kind=None):
        name = getattr(to_state, "value", str(to_state))
        if name in self.TERMINAL:
            raise RuntimeError("connection refused: PostgreSQL is down")
        row = self.rows.setdefault(run_id, {
            "state": "QUEUED", "created_at": 0.0, "updated_at": 0.0,
            "principal_id": "local", "result_ref": None, "error_ref": None,
        })
        row["state"] = name
        return row


def _adapter():
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def test_terminal_write_failure_is_reconciliation_not_false_completed(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    store = _TerminalFailStore()
    adapter._run_store = store

    rid = "run_termtest"
    # Admission + running persist to the store fine.
    adapter._set_run_status(rid, "queued", session_id="local", model="agent")
    adapter._set_run_status(rid, "running")
    assert store.rows[rid]["state"] == "RUNNING"

    # Terminal completion: the store write fails (PG down).
    final = adapter._set_run_status(rid, "completed", output="CUSTOMER_TASK_OK_42")

    # The client must NOT see a durable completed result.
    assert final["status"] == "reconciliation_required"
    assert final.get("durable") is False
    assert final.get("pending_status") == "completed"
    assert final.get("reconciliation_reason") == "terminal_durable_commit_failed"

    # Uncommitted output/error/usage must NOT be exposed as a recoverable result.
    assert final.get("output") is None
    assert final.get("error") is None
    assert "usage" not in final or final.get("usage") is None

    # /result would report terminal=false for this status.
    terminal = final["status"].lower() in (
        "completed", "succeeded", "failed", "error", "cancelled", "canceled"
    )
    assert terminal is False

    # The store never recorded a terminal state — a restart reading the store
    # shows RUNNING (the last durable truth), not a completion that vanished.
    assert store.rows[rid]["state"] == "RUNNING"


def test_startup_reconcile_moves_orphaned_running_to_unknown(monkeypatch, tmp_path):
    """A non-terminal run owned by a dead prior instance is moved to UNKNOWN at
    startup, so it does NOT read back as ordinary running after a restart."""
    from youtab_runtime.durable_run_store import RunIdentity, RunState, SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    # Simulate a run admitted+running by a PRIOR process (a dead pid).
    dead_pid = 999999
    store.admit(
        RunIdentity(task_id="run_orphan", run_id="run_orphan", tenant_id="local",
                    organization_id="local", workspace_id="local",
                    principal_id="local", agent_id="agent"),
        owner=f"pid:{dead_pid}", initial_state=RunState.RUNNING,
    )
    assert store.get_run("run_orphan")["state"] == "RUNNING"

    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = store
    adapter._reconcile_orphaned_runs_on_startup()

    row = store.get_run("run_orphan")
    assert row["state"] == "UNKNOWN"  # discoverable unresolved state, not RUNNING


def test_local_mode_keeps_best_effort_completion(monkeypatch, tmp_path):
    # Without an explicit server backend, a terminal write failure does NOT flip
    # to reconciliation_required (desktop best-effort posture is preserved).
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    adapter = _adapter()
    adapter._run_store = _TerminalFailStore()
    adapter._set_run_status("run_local", "queued", session_id="local", model="agent")
    final = adapter._set_run_status("run_local", "completed", output="LOCAL_OK")
    assert final["status"] == "completed"
