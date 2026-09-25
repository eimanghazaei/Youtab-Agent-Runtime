"""WAVE-27 multi-process stress + real-crash recovery for the effect ledger.

The existing coverage proves atomicity only *within one process*:
``test_try_claim_is_atomic_under_threads`` races 12 threads, but they all share
the module-level ``run_journal._lock`` (an RLock), so SQLite's ``BEGIN
IMMEDIATE`` write lock is never actually contended cross-process — the RLock
serialises them first. And ``test_effect_idempotency_crashwindow`` *simulates* a
dead owner by monkeypatching ``_owner_is_live``; no real process is ever killed.

This module closes both gaps with real OS processes (``multiprocessing`` with the
``spawn`` context, which is the only start method on Windows and the honest one
to assert cross-process locking with):

  1. ``test_multiprocess_single_winner`` — N real processes all ``try_claim`` the
     SAME effect_id against one shared ``run_journal.db``, released together by a
     ``multiprocessing.Barrier`` so the contention is real. Exactly one wins; the
     final row is ``in_progress`` with ``attempts == 1`` and exactly one
     ``in_progress`` journal event.
  2. ``test_real_crash_mid_effect_recovers_to_unknown`` — a child process wins the
     claim (durably ``in_progress``), is then KILLED before it can commit, and the
     parent's ``recover_interrupted`` (fail-safe ``(pid, start_time)`` liveness)
     transitions the provably-dead owner's effect to ``unknown`` — never a blind
     retry: ``should_execute`` is ``False`` afterwards.

Mirrors ``tests/stress/test_concurrency.py`` (shared DB, spawn workers, barrier
for real contention), but targets ``youtab_runtime.effect_ledger`` directly.

Both tests deliver real signals to their OWN multiprocessing children on POSIX
(``Process.terminate()``/``.kill()`` route through ``os.kill``). That is exactly
the sanctioned use of the conftest live-system guard opt-out — the guard defends
against a test reaching a real ``kill_gateway``/``cmd_update`` path, not against a
test reaping the children it spawned — so both are marked
``live_system_guard_bypass`` (and the guard's psutil parents() walk can
transiently raise under -j CI load and block a legitimate own-child kill; the
marker makes signal delivery deterministic without weakening any assertion).
On Windows ``.kill()`` routes through ``TerminateProcess``, not ``os.kill``, so
the marker is a harmless no-op there.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

WT = str(Path(__file__).resolve().parents[2])

RUN_ID = "run-mp-1"
TENANT = "tenant-mp"
USER = "user-mp"
NUM_PROCS = 6


# --------------------------------------------------------------------------- #
# Worker bodies (module-level so they pickle under the spawn start method)     #
# --------------------------------------------------------------------------- #
def _claim_worker(worker_id, db_path_str, effect_id, barrier, result_q):
    """Race to claim ``effect_id`` the instant the barrier releases."""
    sys.path.insert(0, WT)
    from youtab_runtime import effect_ledger as el
    from youtab_runtime.run_journal import Principal

    principal = Principal(TENANT, USER)
    try:
        barrier.wait(timeout=60)  # everyone hits try_claim together
    except Exception:  # noqa: BLE001 — a lagging worker still races, just later
        pass
    try:
        won, rec = el.try_claim(effect_id, principal, db_path=Path(db_path_str))
        result_q.put((worker_id, "ok", bool(won), rec.state.value, os.getpid()))
    except BaseException as exc:  # noqa: BLE001 — report, never hang the parent
        result_q.put((worker_id, "error", repr(exc), None, os.getpid()))


def _crash_worker(db_path_str, ready_q):
    """Win the claim (durable ``in_progress``), announce it, then block for the
    kill. Deliberately never commits — models a crash mid-effect."""
    sys.path.insert(0, WT)
    from youtab_runtime import effect_ledger as el
    from youtab_runtime.run_journal import Principal

    principal = Principal(TENANT, USER)
    rec = el.begin_effect(
        RUN_ID, principal, "net.post", "https://x/y", db_path=Path(db_path_str)
    )
    won, rec = el.try_claim(rec.effect_id, principal, db_path=Path(db_path_str))
    # Publish only AFTER try_claim's BEGIN IMMEDIATE txn has committed, so the
    # parent is guaranteed to observe a durable in_progress row.
    ready_q.put((rec.effect_id, bool(won), rec.state.value, os.getpid()))
    while True:  # wait to be killed
        time.sleep(1)


# --------------------------------------------------------------------------- #
# 1. Multi-process single-winner                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.live_system_guard_bypass
def test_multiprocess_single_winner(tmp_path):
    from youtab_runtime import effect_ledger as el
    from youtab_runtime.run_journal import Principal, list_events
    from youtab_runtime.run_states import EffectState

    db_path = tmp_path / "runtime" / "run_journal.db"
    principal = Principal(TENANT, USER)

    # Parent registers the effect (authorized, attempts=0) BEFORE forking so
    # every worker observes the same committed starting row.
    eff = el.begin_effect(
        RUN_ID, principal, "runtime.retry", "shared-key", db_path=db_path
    )

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(NUM_PROCS)
    result_q = ctx.Queue()
    procs = [
        ctx.Process(
            target=_claim_worker,
            args=(i, str(db_path), eff.effect_id, barrier, result_q),
        )
        for i in range(NUM_PROCS)
    ]
    for p in procs:
        p.start()

    # Drain results before joining so a full pipe can never deadlock the join.
    results = [result_q.get(timeout=90) for _ in range(NUM_PROCS)]
    for p in procs:
        p.join(timeout=30)
        if p.is_alive():
            p.terminate()
            p.join()

    errors = [r for r in results if r[1] == "error"]
    assert not errors, f"workers raised: {errors}"

    wins = [r for r in results if r[2] is True]
    losses = [r for r in results if r[2] is False]
    assert len(wins) == 1, f"expected exactly one winner, got {results}"
    assert len(losses) == NUM_PROCS - 1
    # Every worker (winner and losers) observes in_progress — losers because the
    # winner already flipped it inside the single-writer transaction.
    assert all(r[3] == EffectState.IN_PROGRESS.value for r in results)

    # Durable ledger state: claimed exactly once.
    final = el.get_effect(eff.effect_id, principal, db_path=db_path)
    assert final is not None
    assert final.state == EffectState.IN_PROGRESS
    assert final.attempts == 1, "only the single winner may bump attempts"
    assert el.should_execute(final) is False  # in_progress is not executable

    # Exactly one in_progress event in the journal (no double claim recorded).
    events = list_events(RUN_ID, principal, category="effect", db_path=db_path)
    in_progress = [e for e in events if e.kind == EffectState.IN_PROGRESS.value]
    assert len(in_progress) == 1, [e.kind for e in events]


# --------------------------------------------------------------------------- #
# 3. Real crash mid-effect -> restart recovery marks it unknown              #
# --------------------------------------------------------------------------- #
@pytest.mark.live_system_guard_bypass
def test_real_crash_mid_effect_recovers_to_unknown(tmp_path):
    from youtab_runtime import effect_ledger as el
    from youtab_runtime.run_journal import Principal
    from youtab_runtime.run_states import EffectState

    db_path = tmp_path / "runtime" / "run_journal.db"
    principal = Principal(TENANT, USER)

    ctx = mp.get_context("spawn")
    ready_q = ctx.Queue()
    proc = ctx.Process(target=_crash_worker, args=(str(db_path), ready_q))
    proc.start()

    effect_id, won, state, child_pid = ready_q.get(timeout=60)
    assert won is True
    assert state == EffectState.IN_PROGRESS.value
    assert child_pid == proc.pid

    # Before the kill: durable in_progress owned by the (still live) child.
    mid = el.get_effect(effect_id, principal, db_path=db_path)
    assert mid is not None and mid.state == EffectState.IN_PROGRESS

    # Hard-kill the real owner mid-effect (no commit, no mark_unknown ran).
    proc.kill()
    proc.join(timeout=30)
    assert not proc.is_alive(), "child must be dead before recovery runs"

    # Restart recovery in THIS (different) process: the owner is provably gone,
    # so the effect becomes unknown — and is never blindly retried.
    changed = el.recover_interrupted(run_id=RUN_ID, principal=principal,
                                     db_path=db_path)
    assert changed == 1, "the one crash-stranded effect must be recovered"

    recovered = el.get_effect(effect_id, principal, db_path=db_path)
    assert recovered is not None
    assert recovered.state == EffectState.UNKNOWN
    assert el.should_execute(recovered) is False  # never blind-retry an unknown
    assert recovered.detail.get("recovery")  # carries the "outcome unknown" note

    # Idempotent: a second recovery sweep finds nothing left to move.
    assert el.recover_interrupted(run_id=RUN_ID, principal=principal,
                                  db_path=db_path) == 0
