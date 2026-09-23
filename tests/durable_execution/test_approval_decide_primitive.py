"""Store-level qualification for decide_open_approval (id-bound, single-use CAS).

Fixes two reproduced defects that an adapter could not fix (get_run->transition
is two txns, racy; transition() permits a same-state no-op so it is not an
expected-from-state CAS):
  1) the decision was not bound to the OPEN approval id;
  2) the decision was not single-use.

These are canonical-store tests (SqliteRunStore). Adapter-level and PG-concurrency
negatives are added by the ingress/adapter owner on top of this primitive.
"""

import threading

import pytest

from youtab_runtime.durable_run_store import (
    ApprovalNotOpen,
    InvalidTransition,
    RunIdentity,
    RunState,
    SqliteRunStore,
)


def _ident(run_id):
    return RunIdentity(
        task_id=run_id, run_id=run_id, tenant_id="local", organization_id="local",
        workspace_id="local", principal_id="local", agent_id="agent",
    )


def _store_with_open_approval(tmp_path, approval_id="appr-A"):
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    store.create_run(_ident("r"), initial_state=RunState.QUEUED)
    store.transition("r", RunState.CLAIMED)
    store.transition("r", RunState.RUNNING)
    store.transition("r", RunState.WAITING_APPROVAL, kind="approval_request",
                     payload={"approval_id": approval_id})
    return store


def test_approve_binds_id_and_moves_state(tmp_path):
    store = _store_with_open_approval(tmp_path)
    row = store.decide_open_approval("r", approval_id="appr-A", to_state=RunState.RUNNING,
                                     kind="approval_approved")
    assert row["state"] == "RUNNING"
    kinds = [(e["kind"], e["payload"].get("approval_id")) for e in store.get_events("r")]
    assert ("approval_approved", "appr-A") in kinds


def test_wrong_id_is_refused(tmp_path):
    store = _store_with_open_approval(tmp_path, approval_id="appr-A")
    with pytest.raises(ApprovalNotOpen):
        store.decide_open_approval("r", approval_id="appr-WRONG", to_state=RunState.RUNNING,
                                   kind="approval_approved")
    assert store.get_run("r")["state"] == "WAITING_APPROVAL"  # unchanged


def test_single_use_second_decision_refused(tmp_path):
    store = _store_with_open_approval(tmp_path)
    store.decide_open_approval("r", approval_id="appr-A", to_state=RunState.RUNNING,
                               kind="approval_approved")
    with pytest.raises(ApprovalNotOpen):
        store.decide_open_approval("r", approval_id="appr-A", to_state=RunState.CANCELLING,
                                   kind="approval_denied")


def test_not_waiting_is_refused(tmp_path):
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    store.create_run(_ident("r"), initial_state=RunState.QUEUED)
    store.transition("r", RunState.CLAIMED)
    store.transition("r", RunState.RUNNING)  # never entered WAITING_APPROVAL
    with pytest.raises(ApprovalNotOpen):
        store.decide_open_approval("r", approval_id="x", to_state=RunState.RUNNING,
                                   kind="approval_approved")


def test_invalid_target_state_is_refused(tmp_path):
    store = _store_with_open_approval(tmp_path)
    with pytest.raises(InvalidTransition):
        store.decide_open_approval("r", approval_id="appr-A", to_state=RunState.SUCCEEDED,
                                   kind="approval_approved")


def test_deny_leaves_result_ref_null_and_is_durable(tmp_path):
    store = _store_with_open_approval(tmp_path)
    row = store.decide_open_approval("r", approval_id="appr-A", to_state=RunState.FAILED,
                                     kind="approval_denied", error_ref="denied by operator")
    assert row["state"] == "FAILED"
    assert row["result_ref"] is None  # no fabricated receipt
    # Durable across a fresh store on the same DB.
    reopened = SqliteRunStore(str(tmp_path / "runs.db"))
    r2 = reopened.get_run("r")
    assert r2["state"] == "FAILED" and r2["result_ref"] is None


def test_concurrent_duplicate_exactly_one_wins(tmp_path):
    store = _store_with_open_approval(tmp_path)
    results = []
    barrier = threading.Barrier(2)

    def _decide(target):
        barrier.wait()
        try:
            store.decide_open_approval("r", approval_id="appr-A", to_state=target,
                                       kind="approval_decision")
            results.append("won")
        except ApprovalNotOpen:
            results.append("refused")
        except Exception as exc:  # sqlite busy is acceptable as a loss, but record it
            results.append(f"err:{type(exc).__name__}")

    t1 = threading.Thread(target=_decide, args=(RunState.RUNNING,))
    t2 = threading.Thread(target=_decide, args=(RunState.CANCELLING,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results.count("won") == 1, results
    assert "refused" in results or any(r.startswith("err") for r in results), results
