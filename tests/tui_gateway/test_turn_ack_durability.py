"""Behavior tests for the turn-completion durability/acknowledgement boundary
(_finalize_turn_ack): the durable transcript commit MUST happen-before the
client-observable ``message.complete`` ack, for BOTH success and failure.

Regression for the failure path: if the session-transcript commit fails, the
turn must NOT be acknowledged as a successful completion, the crash-recovery
marker must NOT be retired, and the turn must be retained (recoverable) so a
reconnect/resume can retry — committing exactly once, no duplicate rows.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from tui_gateway import server as gw


def _session(tmp_path, *, session_key="sess-key-001"):
    return {
        "history_lock": threading.Lock(),
        "inflight_turn": {"user": "hello", "assistant": "hi", "started_at": 0.0},
        "session_key": session_key,
        # profile-aware home so _retire_turn_marker resolves a real Path (the
        # actual marker write is patched out below).
        "profile_home": str(tmp_path),
    }


def _agent(*, persist_raises: bool):
    agent = MagicMock()
    agent._session_messages = [{"role": "user", "content": "hello"}]
    if persist_raises:
        agent._persist_session = MagicMock(side_effect=RuntimeError("disk full"))
    else:
        agent._persist_session = MagicMock(return_value=None)
    return agent


def test_success_commits_then_acks_and_retires_marker(tmp_path):
    session = _session(tmp_path)
    agent = _agent(persist_raises=False)
    payload = {"text": "hi", "status": "complete"}
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", payload, {"final_response": "hi"}, "hi"
        )
    # committed before ack, success acknowledged, marker retired, turn cleared
    agent._persist_session.assert_called_once_with(agent._session_messages)
    assert status == "complete"
    assert retained is False
    assert payload["status"] == "complete"
    assert "error" not in payload and "recoverable" not in payload
    assert session["inflight_turn"] is None
    assert clear_marker.called  # marker retired only after a durable commit


def test_persist_failure_downgrades_to_recoverable_and_keeps_marker(tmp_path):
    session = _session(tmp_path)
    agent = _agent(persist_raises=True)
    payload = {"text": "hi", "status": "complete"}
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", payload, {"final_response": "hi"}, "hi"
        )
    # a would-be-success turn that could NOT be committed is NOT acked as success
    agent._persist_session.assert_called_once()
    assert status == "error"
    assert retained is True
    assert payload["status"] == "error"
    assert payload["recoverable"] is True
    assert "could not be saved" in payload["error"]
    # recoverable state remains: turn retained (not cleared), marker NOT retired
    assert isinstance(session["inflight_turn"], dict)
    assert session["inflight_turn"]["status"] == "error"
    assert session["inflight_turn"]["recoverable"] is True
    assert not clear_marker.called  # marker preserved for reconnect/retry


def test_retry_after_failure_commits_once_and_then_acks(tmp_path):
    session = _session(tmp_path)
    # First attempt fails, second attempt (reconnect/resume) succeeds.
    agent = MagicMock()
    agent._session_messages = [{"role": "user", "content": "hello"}]
    agent._persist_session = MagicMock(side_effect=[RuntimeError("transient"), None])

    with patch.object(gw, "clear_turn_marker") as clear_marker:
        payload1 = {"text": "hi", "status": "complete"}
        status1, _ = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", payload1, {"final_response": "hi"}, "hi"
        )
        assert status1 == "error"
        assert not clear_marker.called  # still recoverable, marker kept

        payload2 = {"text": "hi", "status": "complete"}
        status2, retained2 = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", payload2, {"final_response": "hi"}, "hi"
        )
    # retry acknowledges success and retires the marker; commit attempted each
    # time (real _persist_session marker-dedup guarantees at-most-once rows).
    assert status2 == "complete"
    assert retained2 is False
    assert agent._persist_session.call_count == 2
    assert session["inflight_turn"] is None
    assert clear_marker.called


def test_already_error_turn_retained_and_marker_retired(tmp_path):
    session = _session(tmp_path)
    agent = _agent(persist_raises=False)
    payload = {"text": "", "status": "error"}
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "error", payload,
            {"error": "provider 4xx", "failed": True}, "",
        )
    # provider-error turn: retained + recoverable, its OWN error surfaced (not a
    # persist-failure message), and the marker is retired (existing behavior).
    assert status == "error"
    assert retained is True
    assert payload["recoverable"] is True
    assert payload["error"] == "provider 4xx"
    assert session["inflight_turn"]["status"] == "error"
    assert clear_marker.called


def test_no_session_messages_no_downgrade_and_marker_retired(tmp_path):
    session = _session(tmp_path)
    agent = MagicMock()
    agent._session_messages = None  # nothing new to flush this turn
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", {"status": "complete"},
            {"final_response": "hi"}, "hi",
        )
    agent._persist_session.assert_not_called()
    assert status == "complete"
    assert retained is False
    assert session["inflight_turn"] is None
    assert clear_marker.called
