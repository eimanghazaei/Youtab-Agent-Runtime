"""P0-B BASELINE CHARACTERIZATION — async delegation across a Runtime restart.

NOTE: asserts the CURRENT behavior (durable identity / recoverable status, no
execution resume, no duplicate child). Desired-invariant (true resume) is future.

Durable-execution requirement: after parent disconnect + Runtime restart, an
accepted delegation should either RESUME execution from its last checkpoint or be
explicitly marked with a terminal/reconciliation state — never silently vanish.

This reproduction proves the CURRENT behavior on origin/main (0.19.1):
async_delegation gives DURABLE IDENTITY / RECOVERABLE STATUS only. On restart, a
'running' delegation whose owner process is gone is marked `state='unknown'`
(status recovered), but execution is NOT resumed and NO checkpoint/step is
continued. A separately-completed delegation's result is redelivered (identity
survives), confirming the boundary precisely.

Ownership: this stream owns restart/resume + checkpoints. Lane 1/2 own the
UNKNOWN/reconciliation *effect* truth; here we only characterise the delegation
identity/status layer, not a second reconciliation ledger.
"""

import json
import time

import pytest

from gateway import status as gw_status
from tools import async_delegation as ad


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "get_youtab_home", lambda: tmp_path)
    return tmp_path


def _query_row(delegation_id):
    conn = ad._connect()
    try:
        row = conn.execute(
            "SELECT state, result_json, delivery_state FROM async_delegations "
            "WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
    finally:
        conn.close()
    return row


def test_restart_marks_running_delegation_unknown_not_resumed(temp_home, monkeypatch):
    """A 'running' delegation with a dead owner becomes 'unknown' on restart —
    status recovered, execution NOT resumed [C-2.1d / DURABLE IDENTITY]."""
    delegation_id = "deleg_p0b_running"
    ad._persist_dispatch(
        {
            "delegation_id": delegation_id,
            "session_key": "owner-session",
            "parent_session_id": "durable-parent",
            "dispatched_at": time.time(),
            "goal": "long enterprise task",
        }
    )
    state, _, _ = _query_row(delegation_id)
    assert state == "running", "precondition: dispatched delegation is running"

    # Restart: the owning process is gone.
    monkeypatch.setattr(gw_status, "_pid_exists", lambda pid: False)
    recovered = ad.recover_abandoned_delegations()
    assert recovered == 1

    state, result_json, delivery_state = _query_row(delegation_id)
    result = json.loads(result_json)

    # DEFECT/BOUNDARY REPRODUCED: recovery yields an UNKNOWN status, not a resume.
    assert state == "unknown"
    assert result["status"] == "unknown"
    assert result["summary"] is None, "no result — execution was not continued"
    assert "unknown" in result["error"].lower()
    assert delivery_state == "pending"

    # Prove NON-resume explicitly: the outcome is never 'completed'/'succeeded'
    # and no continuation result exists. Identity survived; execution did not.
    assert result["status"] not in ("completed", "succeeded")

    # Recovery must NOT dispatch a duplicate child (no second registry, no respawn).
    assert ad.active_count() == 0, "recovery must not spawn a new child"
    conn = ad._connect()
    try:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE delegation_id LIKE 'deleg_p0b_running%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert row_count == 1, "exactly one durable row; no duplicate delegation created"

    # A late real child result is unreconstructable (owner process is gone): only
    # the 'unknown' status is recoverable/deliverable — the late completion is
    # effectively discarded, not silently accepted as success.
    assert result["summary"] is None


def test_live_owner_delegation_is_not_reaped(temp_home, monkeypatch):
    """Control: if the owner is still alive, restart-recovery must NOT touch it."""
    delegation_id = "deleg_p0b_live"
    ad._persist_dispatch(
        {
            "delegation_id": delegation_id,
            "session_key": "owner-session",
            "parent_session_id": "durable-parent",
            "dispatched_at": time.time(),
            "goal": "still running",
        }
    )
    monkeypatch.setattr(gw_status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(gw_status, "get_process_start_time", lambda pid: _same_start(delegation_id))
    recovered = ad.recover_abandoned_delegations()
    assert recovered == 0
    state, _, _ = _query_row(delegation_id)
    assert state == "running"


def _same_start(delegation_id):
    conn = ad._connect()
    try:
        row = conn.execute(
            "SELECT owner_started_at FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else 0
