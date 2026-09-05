"""Tests for the WAVE-26 durable per-run event journal (contracts 1-4)."""

from __future__ import annotations

import threading

import pytest

from youtab_runtime.run_journal import (
    CATEGORIES,
    Principal,
    RunJournalError,
    append_event,
    latest_seq,
    list_events,
    list_events_by_category,
)


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "run_journal.db"


P = Principal("tenant-a", "user-1")
Q = Principal("tenant-a", "user-2")  # same tenant, different user
R = Principal("tenant-b", "user-1")  # different tenant


def test_append_and_read_roundtrip(db):
    ev = append_event("run1", P, "lifecycle", "run_created",
                      {"reason": "test"}, correlation_id="corr-1", db_path=db)
    assert ev.seq == 1
    assert ev.event_id == "run1:1"
    assert ev.run_id == "run1"
    assert ev.tenant == "tenant-a" and ev.user == "user-1"
    assert ev.correlation_id == "corr-1"
    assert ev.payload == {"reason": "test"}

    events = list_events("run1", P, db_path=db)
    assert len(events) == 1
    assert events[0].kind == "run_created"


def test_seq_is_monotonic_per_run_and_independent_across_runs(db):
    append_event("runA", P, "tool_call", "k", {}, db_path=db)
    append_event("runA", P, "tool_result", "k", {}, db_path=db)
    a3 = append_event("runA", P, "usage", "model_call", {}, db_path=db)
    b1 = append_event("runB", P, "lifecycle", "run_created", {}, db_path=db)
    assert a3.seq == 3
    assert b1.seq == 1
    assert latest_seq("runA", db_path=db) == 3
    assert latest_seq("runB", db_path=db) == 1


def test_principal_isolation_no_leak(db):
    append_event("run1", P, "lifecycle", "run_created", {}, db_path=db)
    # Same tenant, different user cannot read.
    assert list_events("run1", Q, db_path=db) == []
    # Different tenant cannot read.
    assert list_events("run1", R, db_path=db) == []
    # Owner can.
    assert len(list_events("run1", P, db_path=db)) == 1


def test_idempotent_append_dedupes_on_key(db):
    first = append_event("run1", P, "usage", "model_call",
                         {"input_tokens": 10}, dedupe_key="turn1:api:0", db_path=db)
    second = append_event("run1", P, "usage", "model_call",
                          {"input_tokens": 10}, dedupe_key="turn1:api:0", db_path=db)
    # Same row returned; not double-counted.
    assert first.seq == second.seq == 1
    assert len(list_events("run1", P, db_path=db)) == 1


def test_dedupe_key_cross_principal_rejected(db):
    append_event("run1", P, "effect", "committed",
                 {"effect_id": "e1"}, dedupe_key="e1:committed", db_path=db)
    with pytest.raises(RunJournalError):
        append_event("run1", Q, "effect", "committed",
                     {"effect_id": "e1"}, dedupe_key="e1:committed", db_path=db)


def test_usage_unknown_is_preserved_never_zeroed(db):
    # Contract: missing usage is null + usage_status=unknown, never 0.
    ev = append_event(
        "run1", P, "usage", "model_call",
        {"input_tokens": None, "output_tokens": None, "usage_status": "unknown",
         "cost": {"amount_usd": None, "status": "unknown"}},
        dedupe_key="turn1:api:0", db_path=db,
    )
    assert ev.payload["input_tokens"] is None
    assert ev.payload["usage_status"] == "unknown"
    assert ev.payload["cost"]["amount_usd"] is None


def test_after_seq_cursor(db):
    for i in range(5):
        append_event("run1", P, "tool_call", f"k{i}", {"i": i}, db_path=db)
    tail = list_events("run1", P, after_seq=3, db_path=db)
    assert [e.seq for e in tail] == [4, 5]


def test_category_filter(db):
    append_event("run1", P, "tool_call", "k", {}, db_path=db)
    append_event("run1", P, "egress", "denied", {}, db_path=db)
    egress = list_events("run1", P, category="egress", db_path=db)
    assert len(egress) == 1
    assert egress[0].category == "egress"


@pytest.mark.parametrize("bad", [
    lambda db: append_event("", P, "usage", "k", {}, db_path=db),
    lambda db: append_event("r", P, "not_a_category", "k", {}, db_path=db),
    lambda db: append_event("r", P, "usage", "", {}, db_path=db),
])
def test_fail_closed_on_bad_input(db, bad):
    with pytest.raises(RunJournalError):
        bad(db)


def test_blank_principal_is_fail_closed():
    with pytest.raises(RunJournalError):
        Principal("", "user")
    with pytest.raises(RunJournalError):
        Principal("tenant", "   ")


def test_all_categories_accepted(db):
    for i, cat in enumerate(sorted(CATEGORIES)):
        ev = append_event("run1", P, cat, "k", {"c": cat}, db_path=db)
        assert ev.category == cat
    assert latest_seq("run1", db_path=db) == len(CATEGORIES)


def test_list_events_by_category_spans_run_ids_and_is_principal_scoped(db):
    # process events are keyed by launch token (a fresh "run_id" per launch);
    # a category sweep must find them across tokens for the owning principal only.
    append_event("token-1", P, "process", "spawned", {"launch_token": "token-1"}, db_path=db)
    append_event("token-1", P, "process", "killed", {}, db_path=db)
    append_event("token-2", P, "process", "recovered", {}, db_path=db)
    append_event("run-x", P, "lifecycle", "run_completed", {}, db_path=db)
    # A different principal's process event must never appear.
    append_event("token-3", R, "process", "spawned", {}, db_path=db)

    kinds = [e.kind for e in list_events_by_category(P, "process", db_path=db)]
    assert kinds == ["spawned", "killed", "recovered"]  # across token-1 + token-2
    # Different principal is isolated.
    assert [e.kind for e in list_events_by_category(R, "process", db_path=db)] == ["spawned"]
    # Category filter excludes lifecycle.
    assert not any(e.category != "process"
                   for e in list_events_by_category(P, "process", db_path=db))


def test_concurrent_appends_get_distinct_monotonic_seqs(db):
    # Cross-thread appends must never collide on seq (BEGIN IMMEDIATE + PK).
    errors: list = []

    def worker(n):
        try:
            for _ in range(20):
                append_event("run1", P, "tool_call", "k", {"n": n}, db_path=db)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    events = list_events("run1", P, limit=5000, db_path=db)
    seqs = [e.seq for e in events]
    assert len(seqs) == 80
    assert sorted(seqs) == list(range(1, 81))  # dense, unique, monotonic
