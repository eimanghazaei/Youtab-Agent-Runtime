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

import os
import subprocess
import sys
import time

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


def test_empty_success_output_is_preserved_in_durable_mirror(monkeypatch, tmp_path):
    """An empty successful response is a real result, not a missing result."""
    from youtab_runtime.durable_run_store import SqliteRunStore

    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    adapter._run_store = store

    run_id = "run_empty_output"
    adapter._set_run_status(run_id, "queued", session_id="local", model="agent")
    adapter._set_run_status(run_id, "running")
    completed = adapter._set_run_status(run_id, "completed", output="")

    assert completed["status"] == "completed"
    assert completed["output"] == ""
    row = store.get_run(run_id)
    assert row is not None and row["state"] == "SUCCEEDED"
    assert row["result_ref"] == ""
    assert adapter._run_status_from_store(run_id)["output"] == ""


@pytest.mark.parametrize("prior_owner", [
    "pid:999999",
    f"pid:{os.getpid()}",  # prior instance had the SAME numeric pid as this one
    "inst:5f0c0ffee0000000000000000000dead:7",
])
def test_startup_reconcile_moves_orphaned_running_to_unknown(monkeypatch, tmp_path, prior_owner):
    """A non-terminal run owned by a prior instance is moved to UNKNOWN once this
    instance holds the exclusive run authority, even if it reused the prior PID."""
    from youtab_runtime.durable_run_store import RunIdentity, RunState, SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    store.admit(
        RunIdentity(task_id="run_orphan", run_id="run_orphan", tenant_id="local",
                    organization_id="local", workspace_id="local",
                    principal_id="local", agent_id="agent"),
        owner=prior_owner, initial_state=RunState.RUNNING,
    )
    assert store.get_run("run_orphan")["state"] == "RUNNING"

    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = store
    adapter._reconcile_orphaned_runs_on_startup()
    try:
        row = store.get_run("run_orphan")
        assert row["state"] == "UNKNOWN"  # discoverable unresolved state, not RUNNING
        assert row["result_ref"] is None
        assert adapter._run_authority_held()
    finally:
        adapter._release_run_authority()


class _AuthorityRecorder:
    def __init__(self):
        self.released = False
        self.held = True

    def add_loss_listener(self, _cb):
        pass

    def release(self):
        self.released = True
        self.held = False


def test_startup_reconcile_store_failure_fails_closed(monkeypatch):
    """A failed authority acquisition cannot leave a ready server behind."""
    class _UnavailableStore:
        def acquire_instance_authority(self, **_kw):
            raise RuntimeError("durable store unavailable during recovery")

        def reconcile_prior_instances(self, _authority):
            raise AssertionError("must not reconcile without authority")

    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = _UnavailableStore()

    with pytest.raises(RuntimeError, match="durable store unavailable during recovery"):
        adapter._reconcile_orphaned_runs_on_startup()
    assert not adapter._run_authority_held()


def test_startup_recovery_failure_releases_authority_and_fails_closed(monkeypatch):
    """Authority acquired but recovery failed: startup fails AND gives the
    authority back, so a retried start is not blocked by a dead half-start."""
    authority = _AuthorityRecorder()

    class _RecoveryFailsStore:
        def acquire_instance_authority(self, **_kw):
            return authority

        def reconcile_prior_instances(self, _authority):
            raise RuntimeError("durable store unavailable during recovery")

    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = _RecoveryFailsStore()
    with pytest.raises(RuntimeError, match="durable store unavailable during recovery"):
        adapter._reconcile_orphaned_runs_on_startup()
    assert authority.released is True
    assert not adapter._run_authority_held()


@pytest.mark.parametrize(
    "store, message",
    [(None, "requires a run store"), (object(), "reconciliation unsupported")],
)
def test_startup_reconcile_missing_authority_fails_closed(monkeypatch, store, message):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = store
    with pytest.raises(RuntimeError, match=message):
        adapter._reconcile_orphaned_runs_on_startup()


_LIVE_INSTANCE = """
import os, sys
from youtab_runtime.durable_run_store import RunIdentity, RunState, SqliteRunStore
store = SqliteRunStore(sys.argv[1])
authority = store.acquire_instance_authority()
store.admit(RunIdentity(task_id="run_live", run_id="run_live", tenant_id="local",
                        organization_id="local", workspace_id="local",
                        principal_id="local", agent_id="agent"),
            owner=authority.owner, initial_state=RunState.RUNNING)
print("READY", flush=True)
sys.stdin.read()  # parent closes stdin -> die abruptly, never releasing
os._exit(9)
"""


def test_startup_does_not_reconcile_another_live_instance(monkeypatch, tmp_path):
    """A second server sharing the store must not start, and must not mark the
    live authority holder's run UNKNOWN. Once that instance is gone, the next
    start recovers its run."""
    from youtab_runtime.durable_run_authority import AuthorityHeld
    from youtab_runtime.durable_run_store import SqliteRunStore

    db = str(tmp_path / "runs.db")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, PYTHONPATH=root)
    owner = subprocess.Popen([sys.executable, "-c", _LIVE_INSTANCE, db],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             cwd=root, env=env, text=True)
    try:
        assert owner.stdout.readline().strip() == "READY"
        store = SqliteRunStore(db)
        monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
        adapter = _adapter()
        monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
        adapter._run_store = store
        with pytest.raises(AuthorityHeld):
            adapter._reconcile_orphaned_runs_on_startup()
        assert not adapter._run_authority_held()
        assert store.get_run("run_live")["state"] == "RUNNING"
    finally:
        owner.stdin.close()
        owner.wait(timeout=30)

    # The holder died without releasing: the OS dropped its lock (Windows
    # releases a dead process's byte-range locks asynchronously).
    for _ in range(300):
        try:
            adapter._reconcile_orphaned_runs_on_startup()
            break
        except AuthorityHeld:
            time.sleep(0.1)
    try:
        assert store.get_run("run_live")["state"] == "UNKNOWN"
    finally:
        adapter._release_run_authority()


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
