"""Store-level qualification for open_approval (expected-RUNNING, single-open CAS).

Gateway c146 review found the OPENING-side mirror of the decide bug: opening an
approval via generic transition() (RUNNING -> WAITING_APPROVAL) also accepts a
same-state WAITING_APPROVAL -> WAITING_APPROVAL write, so a double-open appends a
SECOND approval_request event and re-homes the authoritative approval_id — the
originally issued id can no longer be approved.

open_approval is the from-state CAS: RUNNING is the only opener; a second open
with the SAME id is idempotent (no second event), a DIFFERENT id is refused
(ApprovalAlreadyOpen) and never replaces the pending id; an empty id is rejected.

SQLite is exercised here; PostgreSQL concurrent open/decide is gated on
YOUTAB_TEST_PG_DSN (Gateway acceptance requires real-PG concurrency).
"""

import os
import threading

import pytest

from youtab_runtime.durable_run_store import (
    ApprovalAlreadyOpen,
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


def _running(store, run_id="r"):
    store.create_run(_ident(run_id), initial_state=RunState.QUEUED)
    store.transition(run_id, RunState.CLAIMED)
    store.transition(run_id, RunState.RUNNING)
    return store


def _request_events(store, run_id="r"):
    return [e for e in store.get_events(run_id) if e["kind"] == "approval_request"]


def test_open_moves_running_to_waiting_and_records_one_request(tmp_path):
    store = _running(SqliteRunStore(str(tmp_path / "d.db")))
    row = store.open_approval("r", approval_id="A")
    assert row["state"] == "WAITING_APPROVAL"
    evs = _request_events(store)
    assert len(evs) == 1 and evs[0]["payload"]["approval_id"] == "A"


def test_double_open_same_id_is_idempotent(tmp_path):
    store = _running(SqliteRunStore(str(tmp_path / "d.db")))
    store.open_approval("r", approval_id="A")
    store.open_approval("r", approval_id="A")  # idempotent retry
    assert len(_request_events(store)) == 1  # no second request event
    # The original id is still the authoritative, decidable one.
    row = store.decide_open_approval("r", approval_id="A", to_state=RunState.RUNNING,
                                     kind="approval_approved")
    assert row["state"] == "RUNNING"


def test_double_open_different_id_is_refused_and_keeps_pending(tmp_path):
    store = _running(SqliteRunStore(str(tmp_path / "d.db")))
    store.open_approval("r", approval_id="A")
    with pytest.raises(ApprovalAlreadyOpen):
        store.open_approval("r", approval_id="B")  # must not replace A
    assert len(_request_events(store)) == 1
    # B was refused; the wrong id cannot decide, the original A still can.
    with pytest.raises(ApprovalNotOpen):
        store.decide_open_approval("r", approval_id="B", to_state=RunState.RUNNING,
                                   kind="approval_approved")
    row = store.decide_open_approval("r", approval_id="A", to_state=RunState.RUNNING,
                                     kind="approval_approved")
    assert row["state"] == "RUNNING"


def test_empty_or_blank_id_is_rejected(tmp_path):
    store = _running(SqliteRunStore(str(tmp_path / "d.db")))
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            store.open_approval("r", approval_id=bad)
    assert store.get_run("r")["state"] == "RUNNING"  # unchanged


def test_open_from_non_running_is_invalid(tmp_path):
    store = SqliteRunStore(str(tmp_path / "d.db"))
    store.create_run(_ident("r"), initial_state=RunState.QUEUED)  # not RUNNING
    with pytest.raises(InvalidTransition):
        store.open_approval("r", approval_id="A")


def test_restart_replay_is_idempotent_and_decidable(tmp_path):
    db = str(tmp_path / "d.db")
    store = _running(SqliteRunStore(db))
    store.open_approval("r", approval_id="A")
    # Fresh store on the same DB (a restart): the open approval is durable.
    reopened = SqliteRunStore(db)
    assert reopened.get_run("r")["state"] == "WAITING_APPROVAL"
    reopened.open_approval("r", approval_id="A")  # replayed open — idempotent
    assert len(_request_events(reopened)) == 1
    row = reopened.decide_open_approval("r", approval_id="A", to_state=RunState.RUNNING,
                                        kind="approval_approved")
    assert row["state"] == "RUNNING"


def test_concurrent_open_records_exactly_one_request(tmp_path):
    store = _running(SqliteRunStore(str(tmp_path / "d.db")))
    outcomes = []
    barrier = threading.Barrier(2)

    def _open(aid):
        barrier.wait()
        try:
            store.open_approval("r", approval_id=aid)
            outcomes.append("opened")
        except ApprovalAlreadyOpen:
            outcomes.append("refused")
        except Exception as exc:  # a sqlite BUSY loss is an acceptable non-winner
            outcomes.append(f"err:{type(exc).__name__}")

    t1 = threading.Thread(target=_open, args=("A",))
    t2 = threading.Thread(target=_open, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()
    # Whatever the interleaving: exactly one request event, run is WAITING_APPROVAL,
    # and the pending id is decidable exactly once.
    assert len(_request_events(store)) == 1, outcomes
    assert store.get_run("r")["state"] == "WAITING_APPROVAL"
    assert outcomes.count("opened") == 1, outcomes


# --------------------------- real PostgreSQL --------------------------------- #

_PG_DSN = os.environ.get("YOUTAB_TEST_PG_DSN",
                         "postgresql://youtab:devpass@127.0.0.1:55432/durable")


def _pg_reachable():
    try:
        import psycopg
    except Exception:
        return False
    try:
        with psycopg.connect(_PG_DSN, connect_timeout=3) as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False


_needs_pg = pytest.mark.skipif(not _pg_reachable(),
                               reason="no reachable PostgreSQL at YOUTAB_TEST_PG_DSN")


@_needs_pg
def test_pg_concurrent_open_single_request_then_single_decide():
    import uuid

    from youtab_runtime.durable_run_store_pg import PostgresRunStore
    store = PostgresRunStore(_PG_DSN)
    rid = "pgopen_" + uuid.uuid4().hex[:8]
    store.create_run(_ident(rid), initial_state=RunState.QUEUED)
    store.transition(rid, RunState.CLAIMED)
    store.transition(rid, RunState.RUNNING)

    opened = []
    barrier = threading.Barrier(2)

    def _open(aid):
        barrier.wait()
        try:
            store.open_approval(rid, approval_id=aid)
            opened.append(aid)
        except ApprovalAlreadyOpen:
            pass

    t1 = threading.Thread(target=_open, args=("A",))
    t2 = threading.Thread(target=_open, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()

    reqs = [e for e in store.get_events(rid) if e["kind"] == "approval_request"]
    assert len(reqs) == 1
    assert len(opened) == 1
    winner = reqs[0]["payload"]["approval_id"]

    # Concurrent decide of the winning id: exactly one wins.
    decided = []

    def _decide():
        barrier2.wait()
        try:
            store.decide_open_approval(rid, approval_id=winner, to_state=RunState.RUNNING,
                                       kind="approval_approved")
            decided.append("ok")
        except ApprovalNotOpen:
            decided.append("refused")

    barrier2 = threading.Barrier(2)
    d1 = threading.Thread(target=_decide)
    d2 = threading.Thread(target=_decide)
    d1.start(); d2.start(); d1.join(); d2.join()
    assert decided.count("ok") == 1, decided
    approved = [e for e in store.get_events(rid) if e["kind"] == "approval_approved"]
    assert len(approved) == 1
    assert store.get_run(rid)["state"] == "RUNNING"
