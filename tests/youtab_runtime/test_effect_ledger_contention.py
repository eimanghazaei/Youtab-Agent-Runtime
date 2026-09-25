"""WAVE-27 effect-ledger contention, reconciliation, and masking regressions.

State-based assertions (never mock-call assertions) over
``youtab_runtime.effect_ledger`` against a throwaway ``run_journal.db``:

  2. ``test_barrier_synchronized_single_winner`` — many threads enter ``try_claim``
     released simultaneously by a ``threading.Barrier``; exactly one wins.
  4. ``test_double_submit_loser_observes_committed_child`` — the runtime_retry_run
     contract at the ledger level: the winner commits with a child result, the
     concurrent loser sees ``committed`` and reads back the same child id.
  5. ``test_concurrent_principals_no_cross_visibility`` — many principals each
     drive their own effect on one shared DB; each commits exactly once and no
     principal can see, list, or read another's effect.
  6. ``test_mark_unknown_error_does_not_mask_original_write_error`` — regression
     for the integrator's guard in ``utils.atomic_write_text``: when the durable
     write fails AND the secondary ``mark_unknown`` also fails, the ORIGINAL
     write exception must still propagate (the ledger error is swallowed + logged),
     leaving the effect for a later ``recover_interrupted``.
"""

from __future__ import annotations

import logging
import threading

import pytest

from youtab_runtime import effect_ledger as el
from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_states import EffectState


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "runtime" / "run_journal.db"


@pytest.fixture()
def principal():
    return Principal("tenant-a", "user-a")


# --------------------------------------------------------------------------- #
# 2. Barrier-synchronized in-process contention                              #
# --------------------------------------------------------------------------- #
def test_barrier_synchronized_single_winner(db_path, principal):
    """Unlike the staggered thread test, every thread blocks on one Barrier and
    is released at the same instant, so they all reach ``try_claim`` together."""
    eff = el.begin_effect("run-barrier", principal, "net.post", "https://x/y",
                          db_path=db_path)

    n = 16
    barrier = threading.Barrier(n)
    wins: list[bool] = []
    states: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=30)
            won, rec = el.try_claim(eff.effect_id, principal, db_path=db_path)
            with lock:
                wins.append(won)
                states.append(rec.state.value)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"workers raised: {errors!r}"
    assert wins.count(True) == 1, wins
    assert wins.count(False) == n - 1
    # Winner and losers all observe in_progress.
    assert all(s == EffectState.IN_PROGRESS.value for s in states), states

    final = el.get_effect(eff.effect_id, principal, db_path=db_path)
    assert final.state == EffectState.IN_PROGRESS
    assert final.attempts == 1  # only the winner bumped attempts
    events = list_events("run-barrier", principal, category="effect",
                         db_path=db_path)
    assert sum(1 for e in events if e.kind == EffectState.IN_PROGRESS.value) == 1


# --------------------------------------------------------------------------- #
# 4. Concurrent double-submit reconciliation (runtime_retry_run contract)     #
# --------------------------------------------------------------------------- #
def test_double_submit_loser_observes_committed_child(db_path, principal):
    """Winner commits with a child run id; the concurrent loser must observe
    ``committed`` and read back the SAME child, so a double submit reconciles to
    one result instead of spawning a second retry."""
    eff = el.begin_effect("run-retry", principal, "runtime.retry", "retry-key",
                          db_path=db_path)

    won_a, rec_a = el.try_claim(eff.effect_id, principal, db_path=db_path)
    assert won_a is True and rec_a.state == EffectState.IN_PROGRESS

    committed = el.mark_committed(
        eff.effect_id, principal,
        detail={"child_run_id": "child-abc-123"}, db_path=db_path,
    )
    assert committed.state == EffectState.COMMITTED
    assert committed.detail.get("child_run_id") == "child-abc-123"

    # The second submitter loses and reconciles to the winner's committed child.
    won_b, rec_b = el.try_claim(eff.effect_id, principal, db_path=db_path)
    assert won_b is False
    assert rec_b.state == EffectState.COMMITTED
    assert rec_b.detail.get("child_run_id") == "child-abc-123"

    # Exactly one committed event — no second effect was ever performed.
    events = list_events("run-retry", principal, category="effect",
                         db_path=db_path)
    assert sum(1 for e in events if e.kind == EffectState.COMMITTED.value) == 1


# --------------------------------------------------------------------------- #
# 5. Concurrent principals under load — zero cross-principal visibility        #
# --------------------------------------------------------------------------- #
def test_concurrent_principals_no_cross_visibility(db_path):
    """Many principals concurrently drive their own effect on ONE shared DB
    (same run_id, distinct principals). Each commits exactly once, and no
    principal can address, list, or read another principal's effect."""
    n = 12
    run_id = "run-shared"
    principals = [Principal(f"tenant-{i}", f"user-{i}") for i in range(n)]

    barrier = threading.Barrier(n)
    effect_ids: dict[int, str] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(idx: int):
        p = principals[idx]
        try:
            eff = el.begin_effect(run_id, p, "fs.write", f"/w/out-{idx}.txt",
                                  db_path=db_path)
            barrier.wait(timeout=30)
            won, _ = el.try_claim(eff.effect_id, p, db_path=db_path)
            assert won is True  # each principal's effect is uncontested
            el.mark_committed(eff.effect_id, p, db_path=db_path)
            with lock:
                effect_ids[idx] = eff.effect_id
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"workers raised: {errors!r}"
    assert len(effect_ids) == n

    for i, p in enumerate(principals):
        # Each principal sees exactly its own single effect, committed once.
        own = el.list_effects(run_id, p, db_path=db_path)
        assert [r.effect_id for r in own] == [effect_ids[i]]
        assert own[0].state == EffectState.COMMITTED

        events = list_events(run_id, p, category="effect", db_path=db_path)
        assert sum(1 for e in events
                   if e.kind == EffectState.COMMITTED.value) == 1

        # Cross-principal: another principal's effect_id is invisible (no leak).
        foreign_id = effect_ids[(i + 1) % n]
        assert el.get_effect(foreign_id, p, db_path=db_path) is None
        # ...and try_claim on a foreign effect fails closed rather than leaking.
        with pytest.raises(el.EffectLedgerError):
            el.try_claim(foreign_id, p, db_path=db_path)


# --------------------------------------------------------------------------- #
# 6. mark_unknown-masking regression (utils.atomic_write_text guard)          #
# --------------------------------------------------------------------------- #
def test_mark_unknown_error_does_not_mask_original_write_error(
    tmp_path, monkeypatch, caplog
):
    """When the durable rename fails AND the secondary ledger ``mark_unknown``
    also fails, ``atomic_write_text`` must raise the ORIGINAL write error, not
    the ledger error. The effect is left non-terminal for restart recovery.

    The ledger writes here go to the per-test isolated YOUTAB_AGENT_HOME journal
    (set by the autouse hermetic fixture), so no real journal is touched.
    """
    import utils

    principal = Principal("tenant-w", "user-w")
    target = tmp_path / "payload.txt"

    original_error = OSError("ORIGINAL durable-write failure the caller needs")

    def _boom_replace(_tmp, _target):
        raise original_error

    def _boom_mark_unknown(*_args, **_kwargs):
        raise RuntimeError("SECONDARY ledger error must never surface")

    # Force the durable commit point to fail...
    monkeypatch.setattr(utils, "atomic_replace", _boom_replace)
    # ...and the fail-closed ledger cleanup to also fail.
    monkeypatch.setattr(el, "mark_unknown", _boom_mark_unknown)

    caplog.set_level(logging.WARNING, logger="utils")

    with pytest.raises(OSError) as excinfo:
        utils.atomic_write_text(
            target, "some data", effect=("run-mask", principal)
        )

    # The ORIGINAL write error propagates, not the secondary ledger RuntimeError.
    assert excinfo.value is original_error
    assert "SECONDARY ledger error" not in str(excinfo.value)

    # The swallowed ledger failure was logged, not lost.
    assert any(
        "could not mark effect" in rec.getMessage()
        for rec in caplog.records
    ), caplog.text

    # The effect is left non-terminal (in_progress) for a later restart's
    # recover_interrupted to reclaim — it was NOT silently committed.
    eff_id = el.compute_effect_id(
        "run-mask", principal, "fs.write", str(target)
    )
    rec = el.get_effect(eff_id, principal)
    assert rec is not None
    assert rec.state == EffectState.IN_PROGRESS
    assert el.should_execute(rec) is False
