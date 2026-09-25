"""WAVE-26 effect ledger cross-principal isolation (contract 2/5).

One principal must never be able to read, mutate, or replay another principal's
effect. The composite ``(tenant, user)`` guard is enforced on every accessor and
folded into the ``effect_id`` derivation itself, so principal B cannot even
address principal A's effect.
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
def alice():
    return Principal("tenant-a", "user-a")


@pytest.fixture()
def bob():
    return Principal("tenant-b", "user-b")


def test_effect_id_differs_across_principals(alice, bob):
    a = el.compute_effect_id("run-1", alice, "fs.write", "/data/out.txt")
    b = el.compute_effect_id("run-1", bob, "fs.write", "/data/out.txt")
    assert a != b  # same run/action/target, different principal -> different id


def test_b_cannot_read_a_effect(db_path, alice, bob):
    r = el.begin_effect("run-1", alice, "fs.write", "/p", db_path=db_path)
    assert el.get_effect(r.effect_id, alice, db_path=db_path) is not None
    # B addresses A's exact effect_id -> invisible (no existence leak).
    assert el.get_effect(r.effect_id, bob, db_path=db_path) is None


def test_b_cannot_mutate_a_effect(db_path, alice, bob):
    r = el.begin_effect("run-1", alice, "net.post", "https://x/y", db_path=db_path)
    el.mark_in_progress(r.effect_id, alice, db_path=db_path)
    with pytest.raises(el.EffectLedgerError):
        el.mark_committed(r.effect_id, bob, db_path=db_path)
    with pytest.raises(el.EffectLedgerError):
        el.mark_cancelled(r.effect_id, bob, db_path=db_path)
    # A's effect is unchanged by B's failed attempts.
    assert el.get_effect(r.effect_id, alice, db_path=db_path).state \
        == EffectState.IN_PROGRESS


def test_b_cannot_replay_a_effect(db_path, alice, bob):
    r = el.begin_effect("run-1", alice, "message.send", "telegram:123",
                        db_path=db_path)
    el.mark_in_progress(r.effect_id, alice, db_path=db_path)
    el.mark_committed(r.effect_id, alice, db_path=db_path)
    # B "retrying" the same logical action gets its OWN, separate effect id.
    rb = el.begin_effect("run-1", bob, "message.send", "telegram:123",
                         db_path=db_path)
    assert rb.effect_id != r.effect_id
    assert rb.state == EffectState.AUTHORIZED  # fresh, not A's committed state


def test_list_effects_is_principal_scoped(db_path, alice, bob):
    el.begin_effect("run-1", alice, "fs.write", "/a", db_path=db_path)
    el.begin_effect("run-1", bob, "fs.write", "/b", db_path=db_path)
    assert len(el.list_effects("run-1", alice, db_path=db_path)) == 1
    assert len(el.list_effects("run-1", bob, db_path=db_path)) == 1


def test_recover_scoped_to_principal(db_path, alice, bob, monkeypatch):
    monkeypatch.setattr(
        el, "_capture_owner",
        lambda: {"process_id": "dead", "pid": 424242, "process_started_at": 1},
    )
    monkeypatch.setattr(el, "_owner_is_live", lambda pid, started: False)
    ra = el.begin_effect("run-1", alice, "fs.write", "/a", db_path=db_path)
    rb = el.begin_effect("run-1", bob, "fs.write", "/b", db_path=db_path)
    el.mark_in_progress(ra.effect_id, alice, db_path=db_path)
    el.mark_in_progress(rb.effect_id, bob, db_path=db_path)

    changed = el.recover_interrupted(principal=alice, db_path=db_path)
    assert changed == 1
    assert el.get_effect(ra.effect_id, alice, db_path=db_path).state \
        == EffectState.UNKNOWN
    # Bob's effect is untouched by a recovery scoped to Alice.
    assert el.get_effect(rb.effect_id, bob, db_path=db_path).state \
        == EffectState.IN_PROGRESS


def test_journal_events_are_principal_scoped(db_path, alice, bob):
    r = el.begin_effect("run-1", alice, "fs.write", "/a", db_path=db_path)
    el.mark_in_progress(r.effect_id, alice, db_path=db_path)
    # Bob cannot read Alice's run's effect events.
    assert list_events("run-1", bob, category="effect", db_path=db_path) == []
    assert list_events("run-1", alice, category="effect", db_path=db_path)
