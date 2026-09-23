"""SEC-9 #9: the ``_unblock_task_locked`` refactor that makes the runtime resume
event + blocked->ready requeue atomic.

Proves the extracted locked core behaves identically to the public
``unblock_task`` for external callers, and that a caller can append an event and
requeue in ONE ``write_txn`` such that a failure rolls BOTH back (so a run can
never be left with ``run_resume`` recorded while still blocked).
"""

from __future__ import annotations

import pytest

from youtab_agent_cli import kanban_db as kb


def _make_blocked_task(conn, title="t"):
    tid = kb.create_task(conn, title=title, created_by="u", tenant="t")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    return tid


def _status(conn, tid):
    return conn.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()["status"]


def _kinds(conn, tid):
    return [getattr(e, "kind", None) for e in kb.list_events(conn, tid)]


def test_public_unblock_task_flips_and_appends(tmp_path):
    with kb.connect(tmp_path / "k.db") as conn:
        tid = _make_blocked_task(conn)
        assert kb.unblock_task(conn, tid) is True
        assert _status(conn, tid) == "ready"
        assert "unblocked" in _kinds(conn, tid)


def test_locked_core_matches_public_inside_caller_txn(tmp_path):
    with kb.connect(tmp_path / "k.db") as conn:
        tid = _make_blocked_task(conn)
        with kb.write_txn(conn):
            ok = kb._unblock_task_locked(conn, tid)
        assert ok is True
        assert _status(conn, tid) == "ready"
        assert "unblocked" in _kinds(conn, tid)


def test_locked_core_returns_false_when_not_blocked(tmp_path):
    with kb.connect(tmp_path / "k.db") as conn:
        tid = kb.create_task(conn, title="t", created_by="u", tenant="t")
        # not blocked -> the flip cannot happen
        with kb.write_txn(conn):
            ok = kb._unblock_task_locked(conn, tid)
        assert ok is False


def test_event_and_requeue_are_atomic_on_failure(tmp_path):
    # Append run_resume + requeue in ONE txn, then raise: both roll back.
    with kb.connect(tmp_path / "k.db") as conn:
        tid = _make_blocked_task(conn)
        before = len(list(kb.list_events(conn, tid)))
        with pytest.raises(RuntimeError):
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "run_resume", {"by": "u"})
                assert kb._unblock_task_locked(conn, tid) is True
                raise RuntimeError("boom after requeue")
        # Rolled back: still blocked, no run_resume, no unblocked event.
        assert _status(conn, tid) == "blocked"
        kinds = _kinds(conn, tid)
        assert "run_resume" not in kinds
        assert "unblocked" not in kinds
        assert len(list(kb.list_events(conn, tid))) == before


def test_event_and_requeue_commit_together_on_success(tmp_path):
    with kb.connect(tmp_path / "k.db") as conn:
        tid = _make_blocked_task(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "run_resume", {"by": "u"})
            assert kb._unblock_task_locked(conn, tid) is True
        assert _status(conn, tid) == "ready"
        kinds = _kinds(conn, tid)
        assert "run_resume" in kinds and "unblocked" in kinds
