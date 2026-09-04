"""WAVE-26 effect ledger: identity determinism, state machine, commit-once.

These tests exercise ``youtab_runtime.effect_ledger`` against a throwaway
``run_journal.db`` (``db_path`` fixture), so they never touch a real
``YOUTAB_AGENT_HOME``.
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


# --------------------------------------------------------------------------- #
# effect_id determinism                                                        #
# --------------------------------------------------------------------------- #
def test_effect_id_is_deterministic(principal):
    a = el.compute_effect_id("run-1", principal, "fs.write", "/data/out.txt")
    b = el.compute_effect_id("run-1", principal, "fs.write", "/data/out.txt")
    assert a == b
    assert len(a) == 32


def test_effect_id_varies_by_component(principal):
    base = el.compute_effect_id("run-1", principal, "fs.write", "/data/out.txt")
    other_run = el.compute_effect_id("run-2", principal, "fs.write", "/data/out.txt")
    other_action = el.compute_effect_id("run-1", principal, "fs.delete", "/data/out.txt")
    other_target = el.compute_effect_id("run-1", principal, "fs.write", "/data/other.txt")
    other_tenant = el.compute_effect_id(
        "run-1", Principal("tenant-b", "user-a"), "fs.write", "/data/out.txt"
    )
    other_user = el.compute_effect_id(
        "run-1", Principal("tenant-a", "user-b"), "fs.write", "/data/out.txt"
    )
    assert len({base, other_run, other_action, other_target,
                other_tenant, other_user}) == 6


def test_effect_id_not_from_wall_clock(principal, monkeypatch):
    import time as _time

    first = el.compute_effect_id("run-1", principal, "net.post", "https://x/y")
    monkeypatch.setattr(_time, "time_ns", lambda: 123456789)
    second = el.compute_effect_id("run-1", principal, "net.post", "https://x/y")
    assert first == second  # derivation ignores wall clock entirely


def test_path_normalization_collapses_equivalent_targets(principal):
    a = el.compute_effect_id("run-1", principal, "fs.write", "/data/./sub/../out.txt")
    b = el.compute_effect_id("run-1", principal, "fs.write", "/data/out.txt")
    assert a == b


def test_url_normalization_collapses_equivalent_targets(principal):
    a = el.compute_effect_id(
        "run-1", principal, "net.post", "HTTPS://Example.COM:443/hook?b=2&a=1"
    )
    b = el.compute_effect_id(
        "run-1", principal, "net.post", "https://example.com/hook?a=1&b=2"
    )
    assert a == b


def test_provider_key_derivation():
    eid = "a" * 32
    assert el.derive_provider_idempotency_key(eid, "modal")
    assert el.derive_provider_idempotency_key(eid, "billing")
    assert el.derive_provider_idempotency_key(eid, "browser")
    # Deterministic from effect_id.
    assert (el.derive_provider_idempotency_key(eid, "modal")
            == el.derive_provider_idempotency_key(eid, "modal"))
    # Model inference / unknown provider -> no key (no false exactly-once claim).
    assert el.derive_provider_idempotency_key(eid, "openai") is None
    assert el.derive_provider_idempotency_key(eid, None) is None


# --------------------------------------------------------------------------- #
# state machine                                                                #
# --------------------------------------------------------------------------- #
def test_begin_effect_starts_authorized(db_path, principal):
    rec = el.begin_effect("run-1", principal, "fs.write", "/data/out.txt",
                          db_path=db_path)
    assert rec.state == EffectState.AUTHORIZED
    assert rec.executable is True
    assert rec.attempts == 0
    assert rec.first_seen_seq == rec.last_update_seq >= 1
    assert rec.effect_id == el.compute_effect_id(
        "run-1", principal, "fs.write", "/data/out.txt"
    )


def test_begin_effect_planned_then_authorized(db_path, principal):
    rec = el.begin_effect("run-1", principal, "fs.write", "/p", authorize=False,
                          db_path=db_path)
    assert rec.state == EffectState.PLANNED
    rec2 = el.mark_authorized(rec.effect_id, principal, db_path=db_path)
    assert rec2.state == EffectState.AUTHORIZED


def test_begin_effect_is_idempotent_lookup(db_path, principal):
    r1 = el.begin_effect("run-1", principal, "fs.write", "/p", db_path=db_path)
    r2 = el.begin_effect("run-1", principal, "fs.write", "/p", db_path=db_path)
    assert r1.effect_id == r2.effect_id
    assert len(el.list_effects("run-1", principal, db_path=db_path)) == 1


def test_happy_path_transitions(db_path, principal):
    r = el.begin_effect("run-1", principal, "net.post", "https://x/y",
                        db_path=db_path)
    r = el.mark_in_progress(r.effect_id, principal, db_path=db_path)
    assert r.state == EffectState.IN_PROGRESS
    assert r.attempts == 1
    assert r.executable is False
    r = el.mark_committed(r.effect_id, principal, db_path=db_path)
    assert r.state == EffectState.COMMITTED
    assert r.is_terminal is True


def test_illegal_transition_rejected(db_path, principal):
    r = el.begin_effect("run-1", principal, "fs.write", "/p", db_path=db_path)
    # authorized -> committed skips in_progress and is illegal.
    with pytest.raises(el.EffectStateError):
        el.mark_committed(r.effect_id, principal, db_path=db_path)


def test_terminal_states_are_immutable(db_path, principal):
    r = el.begin_effect("run-1", principal, "fs.write", "/p", db_path=db_path)
    r = el.mark_in_progress(r.effect_id, principal, db_path=db_path)
    r = el.mark_failed(r.effect_id, principal, db_path=db_path)
    assert r.state == EffectState.FAILED
    with pytest.raises(el.EffectStateError):
        el.mark_committed(r.effect_id, principal, db_path=db_path)
    with pytest.raises(el.EffectStateError):
        el.mark_cancelled(r.effect_id, principal, db_path=db_path)


# --------------------------------------------------------------------------- #
# commit-once idempotency                                                      #
# --------------------------------------------------------------------------- #
def test_commit_is_at_most_once(db_path, principal):
    r = el.begin_effect("run-1", principal, "net.post", "https://x/y",
                        db_path=db_path)
    r = el.mark_in_progress(r.effect_id, principal, db_path=db_path)
    first = el.mark_committed(r.effect_id, principal, db_path=db_path)
    # Second commit is a no-op that returns committed (the guarantee).
    second = el.mark_committed(r.effect_id, principal, db_path=db_path)
    assert first.state == second.state == EffectState.COMMITTED
    assert first.last_update_seq == second.last_update_seq  # no new transition

    # Exactly one committed event in the journal (dedupe on (effect_id, state)).
    events = list_events("run-1", principal, category="effect", db_path=db_path)
    committed = [e for e in events if e.kind == EffectState.COMMITTED.value]
    assert len(committed) == 1


def test_transition_emits_journal_events(db_path, principal):
    r = el.begin_effect("run-1", principal, "fs.write", "/p", db_path=db_path)
    el.mark_in_progress(r.effect_id, principal, db_path=db_path)
    el.mark_committed(r.effect_id, principal, db_path=db_path)
    events = list_events("run-1", principal, category="effect", db_path=db_path)
    kinds = [e.kind for e in events]
    assert kinds == [
        EffectState.AUTHORIZED.value,
        EffectState.IN_PROGRESS.value,
        EffectState.COMMITTED.value,
    ]
    # Effect events carry the fixed payload shape, target as digest not raw.
    payload = events[0].payload
    assert payload["effect_id"] == r.effect_id
    assert payload["state"] == EffectState.AUTHORIZED.value
    assert "target_scope_digest" in payload
    assert "/p" not in str(payload)  # raw target never persisted


def test_provider_key_stored_for_supported_provider(db_path, principal):
    r = el.begin_effect("run-1", principal, "net.post", "https://x/y",
                        provider="modal", db_path=db_path)
    assert r.provider_idempotency_key
    assert r.provider_idempotency_key == el.derive_provider_idempotency_key(
        r.effect_id, "modal"
    )
    r2 = el.begin_effect("run-1", principal, "net.post", "https://z/w",
                         provider="openai", db_path=db_path)
    assert r2.provider_idempotency_key is None  # inference: no exactly-once claim
