"""WAVE-26 effect ledger crash-window safety.

Simulates the dangerous window: a worker marks an effect ``in_progress``, does
(or partially does) the side effect, then the process dies before it can record
``committed``. On restart the ledger must:

  * mark the stranded effect ``unknown`` ("whether the side effect ran is
    unknown") rather than assume success OR blindly retry it; and
  * ensure a retry that recomputes the SAME ``effect_id`` sees a non-executable
    state, so the side effect is not re-run and can never double-commit.
"""

from __future__ import annotations

import pytest

from youtab_runtime import effect_ledger as el
from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_states import EffectState


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "run_journal.db"


@pytest.fixture()
def principal():
    return Principal("tenant-a", "user-a")


def _strand_as_foreign_dead_owner(monkeypatch):
    """Make effects begin under a *foreign*, provably-dead owner so that
    recover_interrupted treats them as abandoned (as if this were a fresh
    process after the original owner crashed)."""
    monkeypatch.setattr(
        el, "_capture_owner",
        lambda: {"process_id": "dead-owner-proc", "pid": 424242,
                 "process_started_at": 999999},
    )
    # The foreign pid is not the live current process; force "dead".
    monkeypatch.setattr(el, "_owner_is_live", lambda pid, started: False)


def test_crash_after_in_progress_recovers_to_unknown(db_path, principal, monkeypatch):
    _strand_as_foreign_dead_owner(monkeypatch)

    r = el.begin_effect("run-1", principal, "net.post", "https://x/hook",
                        db_path=db_path)
    r = el.mark_in_progress(r.effect_id, principal, db_path=db_path)
    assert r.state == EffectState.IN_PROGRESS
    # <-- crash here: no mark_committed reached -->

    changed = el.recover_interrupted(db_path=db_path)
    assert changed == 1

    recovered = el.get_effect(r.effect_id, principal, db_path=db_path)
    assert recovered.state == EffectState.UNKNOWN
    assert "unknown" in recovered.detail.get("recovery", "").lower()

    # It was NOT auto-committed and NOT auto-failed: outcome is honestly unknown.
    events = list_events("run-1", principal, category="effect", db_path=db_path)
    kinds = [e.kind for e in events]
    assert EffectState.COMMITTED.value not in kinds
    assert kinds[-1] == EffectState.UNKNOWN.value


def test_retry_with_same_effect_id_is_not_re_executed(db_path, principal, monkeypatch):
    _strand_as_foreign_dead_owner(monkeypatch)

    r1 = el.begin_effect("run-1", principal, "fs.write", "/data/out.txt",
                         db_path=db_path)
    el.mark_in_progress(r1.effect_id, principal, db_path=db_path)
    el.recover_interrupted(db_path=db_path)  # -> unknown

    # A requeue re-derives the SAME effect_id and re-registers the effect.
    r2 = el.begin_effect("run-1", principal, "fs.write", "/data/out.txt",
                         db_path=db_path)
    assert r2.effect_id == r1.effect_id
    assert r2.state == EffectState.UNKNOWN
    # The guard says: do NOT execute an unknown effect (no blind retry).
    assert el.should_execute(r2) is False


def test_recover_leaves_live_owner_untouched(db_path, principal, monkeypatch):
    # Owner is the current live process -> recovery must not rewrite it.
    monkeypatch.setattr(el, "_owner_is_live", lambda pid, started: True)
    r = el.begin_effect("run-1", principal, "net.post", "https://x/y",
                        db_path=db_path)
    el.mark_in_progress(r.effect_id, principal, db_path=db_path)
    changed = el.recover_interrupted(db_path=db_path)
    assert changed == 0
    assert el.get_effect(r.effect_id, principal, db_path=db_path).state \
        == EffectState.IN_PROGRESS


def test_reconcile_after_unknown_commits_at_most_once(db_path, principal, monkeypatch):
    _strand_as_foreign_dead_owner(monkeypatch)
    r = el.begin_effect("run-1", principal, "net.post", "https://x/y",
                        db_path=db_path)
    el.mark_in_progress(r.effect_id, principal, db_path=db_path)
    el.recover_interrupted(db_path=db_path)  # -> unknown

    # Out-of-band reconciliation proves the effect actually committed once.
    committed = el.mark_committed(r.effect_id, principal, db_path=db_path)
    assert committed.state == EffectState.COMMITTED
    # And a duplicate reconciliation is a no-op: never double-commit.
    again = el.mark_committed(r.effect_id, principal, db_path=db_path)
    assert again.last_update_seq == committed.last_update_seq

    events = list_events("run-1", principal, category="effect", db_path=db_path)
    committed_events = [e for e in events if e.kind == EffectState.COMMITTED.value]
    assert len(committed_events) == 1


def test_recover_no_effects_returns_zero(db_path):
    assert el.recover_interrupted(db_path=db_path) == 0
