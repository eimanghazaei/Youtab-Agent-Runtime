"""Unit regression guard for the managed interactive-lifecycle derivation
(ADR-0004 / WAVE-30H Phase-A). Pure, fast, no I/O — pins the state machine that
the HTTP E2E exercises end-to-end."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from youtab_runtime import run_control as rc


@dataclass
class _E:
    id: int
    kind: str
    payload: Any


def _evs(*items):
    return [_E(i + 1, k, p) for i, (k, p) in enumerate(items)]


def test_none_when_no_control_events():
    assert rc.interactive_status(_evs(("worker_started", {}), ("work_step", {"n": 0}))) is None


def test_open_question_is_awaiting_input_then_cleared_by_answer():
    ev = _evs(("run_question", {"question_id": "q1", "prompt": "?"}))
    assert rc.interactive_status(ev) == rc.AWAITING_INPUT
    ev.append(_E(2, "run_answer", {"question_id": "q1", "answer": "x"}))
    assert rc.interactive_status(ev) is None
    assert rc.answer_for(ev, "q1") == "x"
    assert rc.answer_for(ev, "missing") is None


def test_open_approval_is_awaiting_approval_then_cleared_by_decision():
    ev = _evs(("run_approval_request", {"approval_id": "a1", "action": "del"}))
    assert rc.interactive_status(ev) == rc.AWAITING_APPROVAL
    ev.append(_E(2, "run_approval_decision", {"approval_id": "a1", "decision": "deny"}))
    assert rc.interactive_status(ev) is None
    assert rc.decision_for(ev, "a1") == "deny"


def test_bare_pause_is_pause_requested_then_resume_clears():
    # A bare run_pause (worker has not yet acknowledged) is pause_requested, NOT
    # paused — paused requires a valid checkpoint (fail-closed distinction).
    ev = _evs(("work_step", {"n": 0}), ("run_pause", {"by": "u"}))
    assert rc.interactive_status(ev) == rc.PAUSE_REQUESTED
    assert rc.is_paused(ev) is True                       # active pause epoch (worker cue)
    assert rc.has_valid_checkpoint_for_resume(ev) is False  # nothing to resume yet
    ev.append(_E(3, "run_resume", {"by": "u"}))
    assert rc.interactive_status(ev) is None
    assert rc.is_paused(ev) is False


def test_pause_then_checkpoint_is_paused_and_resumable():
    ev = _evs(("run_pause", {"by": "u"}),
              ("run_checkpoint", {"state": {"v": 1, "messages_json": "[]"}}))
    assert rc.interactive_status(ev) == rc.PAUSED
    assert rc.has_valid_checkpoint_for_resume(ev) is True


def test_pause_failed_projects_pause_failed_and_refuses_resume():
    ev = _evs(("run_pause", {"by": "u"}),
              ("run_pause_failed", {"reason": "unserializable"}))
    assert rc.interactive_status(ev) == rc.PAUSE_FAILED
    assert rc.has_valid_checkpoint_for_resume(ev) is False  # NOT resumable — fail-closed
    assert rc.is_paused(ev) is True                          # still an active (halted) pause


def test_pause_failed_after_a_checkpoint_wins_fail_closed():
    # If a later corruption/failure supersedes a checkpoint, the run is pause_failed.
    ev = _evs(("run_pause", {"by": "u"}),
              ("run_checkpoint", {"state": {"v": 1, "messages_json": "[]"}}),
              ("run_pause_failed", {"reason": "integrity"}))
    assert rc.interactive_status(ev) == rc.PAUSE_FAILED
    assert rc.has_valid_checkpoint_for_resume(ev) is False


def test_most_recent_open_signal_wins():
    # a question opens, then a later pause -> pause_requested wins until resumed,
    # then the still-open question is the surviving signal again.
    ev = _evs(
        ("run_question", {"question_id": "q1"}),   # id 1
        ("run_pause", {"by": "u"}),                # id 2  -> most recent open
    )
    assert rc.interactive_status(ev) == rc.PAUSE_REQUESTED
    ev.append(_E(3, "run_resume", {"by": "u"}))    # pause cleared
    assert rc.interactive_status(ev) == rc.AWAITING_INPUT  # question still open


def test_pause_requested_after_and_resume_semantics():
    ev = _evs(("work_step", {"n": 0}), ("run_pause", {"by": "u"}))  # pause id 2
    assert rc.pause_requested_after(ev, since_event_id=0) is True
    assert rc.pause_requested_after(ev, since_event_id=2) is False  # not newer than self
    ev.append(_E(3, "run_resume", {"by": "u"}))
    assert rc.pause_requested_after(ev, since_event_id=0) is False  # resumed


def test_latest_checkpoint_last_wins():
    ev = _evs(
        ("run_checkpoint", {"state": {"step": 2, "acc": [0, 1]}}),
        ("run_checkpoint", {"state": {"step": 5, "acc": [0, 1, 2, 3, 4]}}),
    )
    assert rc.latest_checkpoint(ev) == {"step": 5, "acc": [0, 1, 2, 3, 4]}
    assert rc.latest_checkpoint(_evs(("work_step", {"n": 0}))) is None


def test_decision_rejects_invalid_value():
    ev = _evs(("run_approval_decision", {"approval_id": "a1", "decision": "maybe"}))
    assert rc.decision_for(ev, "a1") is None  # only approve/deny are valid
