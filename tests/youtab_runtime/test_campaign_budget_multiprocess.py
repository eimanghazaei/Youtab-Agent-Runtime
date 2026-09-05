"""WAVE-30C Fix B — REAL cross-process concurrency for the €10 campaign ledger.

The existing ``test_concurrent_reservations_never_exceed_ceiling`` races 20
*threads*, but they all share the module-level ``campaign_budget._lock`` (an
RLock), so SQLite's ``BEGIN IMMEDIATE`` write lock is never actually contended
cross-process — the RLock serialises them first. That under-tests the property
that actually protects the €10 ceiling across the real deployment topology (the
API process and the kanban worker subprocess are *separate processes*, each with
its own connection and its own ``_lock``).

This module closes the gap with real OS processes (``multiprocessing`` with the
``spawn`` context — the only start method on Windows and the honest one for
asserting cross-process locking). Worker bodies are module-level so they pickle
under spawn; every arg passed to a child is picklable; each test binds its DB to a
unique ``tmp_path`` so the file-parallel test runner cannot collide.

It proves, across independent processes:
  1/2. concurrent reservations against one €10 campaign never exceed €10;
  3.   the over-ceiling reservations are refused (``BudgetExceeded``), not errored;
  4.   the same stable ``api_request_id`` is idempotent across processes;
  5.   failover (distinct request IDs) consumes fresh budget, never resets;
  6.   a cross-process ``reconcile`` is consistent and durable;
  7/8. a process killed after reserving leaves the conservative reservation intact
       (no corruption, no auto-release) and reopening the DB preserves totals;
  9.   lock contention has a bounded ``busy_timeout`` and fails CLOSED (no row
       written) rather than corrupting the ledger.

Per the WAVE-30B investigation, ``reserve()`` is already genuinely cross-process
safe (the ceiling check and the insert are atomic inside one ``BEGIN IMMEDIATE``
transaction under ``synchronous=FULL``); these tests are the missing *proof*, and
they live under ``tests/youtab_runtime`` so both the Linux ``python-security``
gate and the Windows ``windows-runtime-cli`` gate execute them (``tests/stress``
is not run by any CI job).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

WT = str(Path(__file__).resolve().parents[2])

CID = "c-mp"
_FX = dict(
    ceiling_eur="10.00",
    fx_usd_to_eur="0.86",
    fx_source="test-fx",
    fx_asof="2026-09-05",
    safety_margin="0.15",
)


# --------------------------------------------------------------------------- #
# Worker bodies (module-level so they pickle under the spawn start method)     #
# --------------------------------------------------------------------------- #
def _reserve_worker(worker_id, db_path_str, api_request_id, amount, barrier, result_q):
    """Race to reserve ``amount`` the instant the barrier releases."""
    sys.path.insert(0, WT)
    from youtab_runtime import campaign_budget as cb

    try:
        barrier.wait(timeout=90)
    except Exception:  # noqa: BLE001 — a lagging worker still races, just later
        pass
    try:
        cb.reserve(CID, api_request_id=api_request_id, amount_eur=amount, db_path=db_path_str)
        result_q.put((worker_id, "won", os.getpid()))
    except cb.BudgetExceeded:
        result_q.put((worker_id, "refused", os.getpid()))
    except BaseException as exc:  # noqa: BLE001 — report, never hang the parent
        result_q.put((worker_id, "error:" + repr(exc), os.getpid()))


def _reconcile_worker(db_path_str, api_request_id, actual, result_q):
    sys.path.insert(0, WT)
    from youtab_runtime import campaign_budget as cb

    try:
        cb.reconcile(CID, api_request_id=api_request_id, actual_eur=actual, db_path=db_path_str)
        result_q.put(("ok", os.getpid()))
    except BaseException as exc:  # noqa: BLE001
        result_q.put(("error:" + repr(exc), os.getpid()))


def _reserve_then_block_worker(db_path_str, api_request_id, amount, ready_q):
    """Reserve (durably committed), announce it, then block for the kill.
    Deliberately never releases — models a crash after a billable reservation."""
    sys.path.insert(0, WT)
    from youtab_runtime import campaign_budget as cb

    cb.reserve(CID, api_request_id=api_request_id, amount_eur=amount, db_path=db_path_str)
    ready_q.put(("reserved", os.getpid()))
    while True:  # wait to be killed
        time.sleep(1)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _open(db_path):
    from youtab_runtime import campaign_budget as cb

    return cb.open_campaign(CID, db_path=db_path, **_FX)


# --------------------------------------------------------------------------- #
# 1/2/3. Concurrent reservations never exceed €10; overflow is refused         #
# --------------------------------------------------------------------------- #
def test_multiprocess_reservations_never_exceed_ceiling(tmp_path):
    from youtab_runtime import campaign_budget as cb

    db_path = str(tmp_path / "campaign_budget.db")
    _open(db_path)  # parent commits the campaign row before any child starts

    num = 15  # €1 each vs €10 ceiling -> 10 win, 5 refused
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(num)
    result_q = ctx.Queue()
    procs = [
        ctx.Process(
            target=_reserve_worker,
            args=(i, db_path, f"w:{i}", "1.00", barrier, result_q),
        )
        for i in range(num)
    ]
    for p in procs:
        p.start()

    # Drain before joining so a full pipe can never deadlock the join.
    results = [result_q.get(timeout=120) for _ in range(num)]
    for p in procs:
        p.join(timeout=30)
        if p.is_alive():
            p.terminate()
            p.join()

    errors = [r for r in results if str(r[1]).startswith("error:")]
    assert not errors, f"workers raised: {errors}"

    wins = [r for r in results if r[1] == "won"]
    refused = [r for r in results if r[1] == "refused"]
    assert len(wins) == 10, f"expected exactly 10 winners, got {results}"
    assert len(refused) == 5
    # Distinct PIDs prove real cross-process contention (not one process reused).
    assert len({r[2] for r in results}) == num

    # The ledger never exceeded the ceiling.
    assert cb.status(CID, db_path=db_path).committed_eur == Decimal("10.00")
    assert cb.remaining_eur(CID, db_path=db_path) == Decimal("0.00")

    # And now even a single cent more is refused (the €10.01 boundary), durably.
    with pytest.raises(cb.BudgetExceeded):
        cb.reserve(CID, api_request_id="over", amount_eur="0.01", db_path=db_path)


# --------------------------------------------------------------------------- #
# 4. Same stable api_request_id is idempotent across processes                 #
# --------------------------------------------------------------------------- #
def test_multiprocess_same_request_id_is_idempotent(tmp_path):
    from youtab_runtime import campaign_budget as cb

    db_path = str(tmp_path / "campaign_budget.db")
    _open(db_path)

    num = 8
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(num)
    result_q = ctx.Queue()
    procs = [
        ctx.Process(
            target=_reserve_worker,
            args=(i, db_path, "shared", "1.00", barrier, result_q),
        )
        for i in range(num)
    ]
    for p in procs:
        p.start()
    results = [result_q.get(timeout=120) for _ in range(num)]
    for p in procs:
        p.join(timeout=30)
        if p.is_alive():
            p.terminate()
            p.join()

    errors = [r for r in results if str(r[1]).startswith("error:")]
    assert not errors, f"workers raised: {errors}"
    # All succeed (idempotent reserve returns the existing row), but the campaign
    # is charged exactly once — the uq_reservation_key index + SELECT-first branch.
    assert all(r[1] == "won" for r in results), results
    assert cb.status(CID, db_path=db_path).committed_eur == Decimal("1.00")
    assert cb.remaining_eur(CID, db_path=db_path) == Decimal("9.00")


# --------------------------------------------------------------------------- #
# 5. Failover (distinct request IDs) consumes fresh budget                      #
# --------------------------------------------------------------------------- #
def test_multiprocess_failover_distinct_ids_consume_fresh_budget(tmp_path):
    from youtab_runtime import campaign_budget as cb

    db_path = str(tmp_path / "campaign_budget.db")
    _open(db_path)

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    result_q = ctx.Queue()
    specs = [("t:1", "6.00"), ("t:2", "3.00")]
    procs = [
        ctx.Process(target=_reserve_worker, args=(i, db_path, rid, amt, barrier, result_q))
        for i, (rid, amt) in enumerate(specs)
    ]
    for p in procs:
        p.start()
    results = [result_q.get(timeout=120) for _ in range(2)]
    for p in procs:
        p.join(timeout=30)

    assert all(r[1] == "won" for r in results), results
    # 6 + 3 both land; a failover call never resets the campaign total.
    assert cb.remaining_eur(CID, db_path=db_path) == Decimal("1.00")


# --------------------------------------------------------------------------- #
# 6. Cross-process reconcile is consistent and durable                         #
# --------------------------------------------------------------------------- #
def test_multiprocess_reconcile_visible_across_processes(tmp_path):
    from youtab_runtime import campaign_budget as cb

    db_path = str(tmp_path / "campaign_budget.db")
    _open(db_path)
    cb.reserve(CID, api_request_id="t:1", amount_eur="3.00", db_path=db_path)
    assert cb.remaining_eur(CID, db_path=db_path) == Decimal("7.00")

    ctx = mp.get_context("spawn")
    result_q = ctx.Queue()
    proc = ctx.Process(target=_reconcile_worker, args=(db_path, "t:1", "1.50", result_q))
    proc.start()
    status, _pid = result_q.get(timeout=120)
    proc.join(timeout=30)

    assert status == "ok", status
    # The child's reconcile is visible to the parent's fresh connection.
    assert cb.remaining_eur(CID, db_path=db_path) == Decimal("8.50")


# --------------------------------------------------------------------------- #
# 7/8. Killed-after-reserve preserves the reservation; reopen preserves totals #
# --------------------------------------------------------------------------- #
@pytest.mark.live_system_guard_bypass
def test_multiprocess_kill_after_reserve_preserves_ledger(tmp_path):
    from youtab_runtime import campaign_budget as cb

    db_path = str(tmp_path / "campaign_budget.db")
    _open(db_path)

    ctx = mp.get_context("spawn")
    ready_q = ctx.Queue()
    proc = ctx.Process(
        target=_reserve_then_block_worker, args=(db_path, "t:kill", "4.00", ready_q)
    )
    proc.start()
    msg, child_pid = ready_q.get(timeout=60)
    assert msg == "reserved"
    assert child_pid == proc.pid

    # Hard-kill the reserver after its BEGIN IMMEDIATE committed.
    proc.kill()
    proc.join(timeout=30)
    assert not proc.is_alive(), "child must be dead before we re-read the ledger"

    # A fresh connection in THIS process (db reopened) still sees the €4
    # conservative reservation — not corrupted, not auto-released.
    assert cb.status(CID, db_path=db_path).committed_eur == Decimal("4.00")
    assert cb.remaining_eur(CID, db_path=db_path) == Decimal("6.00")


# --------------------------------------------------------------------------- #
# 9. Bounded lock timeout fails CLOSED (no row written)                        #
# --------------------------------------------------------------------------- #
def test_multiprocess_lock_contention_fails_closed(tmp_path):
    """A held write lock forces a contending process to wait up to busy_timeout
    and then FAIL CLOSED (OperationalError 'database is locked') — never a partial
    or lost reservation."""
    from youtab_runtime import campaign_budget as cb

    db_path = str(tmp_path / "campaign_budget.db")
    _open(db_path)
    before = cb.status(CID, db_path=db_path).committed_eur

    # Parent grabs and HOLDS the SQLite write lock on the same file.
    blocker = sqlite3.connect(db_path, timeout=0)
    blocker.execute("PRAGMA busy_timeout=0")
    blocker.isolation_level = None
    blocker.execute("BEGIN IMMEDIATE")
    ctx = mp.get_context("spawn")
    result_q = ctx.Queue()
    proc = ctx.Process(
        target=_reserve_worker,
        args=(0, db_path, "blocked", "1.00", ctx.Barrier(1), result_q),
    )
    t0 = time.monotonic()
    try:
        proc.start()
        worker_id, outcome, _pid = result_q.get(timeout=60)
        waited = time.monotonic() - t0
    finally:
        blocker.rollback()
        blocker.close()
        proc.join(timeout=30)
        if proc.is_alive():
            proc.terminate()
            proc.join()

    # The child waited (busy_timeout ~10s) then failed closed with a lock error.
    assert str(outcome).startswith("error:"), outcome
    assert "locked" in str(outcome).lower(), outcome
    assert waited >= 5.0, f"expected the child to wait on the lock, waited {waited:.1f}s"
    # Nothing was written: the ledger total is unchanged (fail-closed).
    assert cb.status(CID, db_path=db_path).committed_eur == before
