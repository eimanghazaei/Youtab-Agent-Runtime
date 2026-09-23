"""Behavior tests for the turn-completion durability/acknowledgement boundary
(_finalize_turn_ack): the durable transcript commit MUST happen-before the
client-observable ``message.complete`` ack, for BOTH success and failure, and
the on-disk crash-recovery marker is retired ONLY when the commit actually
succeeded — for EVERY status.

Regression for the failure path: if the session-transcript commit fails, the
turn must NOT be acknowledged as a successful completion, the crash-recovery
marker must NOT be retired (so ``session.resume`` re-runs it after a process
death — the in-memory retained turn is not durable), and the turn must be
retained recoverable. A retry commits exactly once (no duplicate rows).

These test the helper's contract + return value; the caller's ``finally`` gate
(retire only when the helper reports ``committed``) and its real frozen-backend
behaviour across a process relaunch are exercised by the packaged e2e
(packaged-attachment-resume.spec.ts, the injected-fault prompt-close case).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from tui_gateway import server as gw


def _session(tmp_path, *, session_key="sess-key-001"):
    return {
        "history_lock": threading.Lock(),
        "inflight_turn": {"user": "hello", "assistant": "hi", "started_at": 0.0},
        "session_key": session_key,
        "profile_home": str(tmp_path),
    }


def _agent(*, persist_raises: bool):
    agent = MagicMock()
    agent._session_messages = [{"role": "user", "content": "hello"}]
    agent._persist_session = MagicMock(
        side_effect=RuntimeError("disk full") if persist_raises else None
    )
    return agent


def test_success_commits_then_acks_and_retires_marker(tmp_path):
    session = _session(tmp_path)
    agent = _agent(persist_raises=False)
    payload = {"text": "hi", "status": "complete"}
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained, committed = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", payload, {"final_response": "hi"}, "hi"
        )
    agent._persist_session.assert_called_once_with(agent._session_messages)
    assert status == "complete"
    assert retained is False
    assert committed is True
    assert "error" not in payload and "recoverable" not in payload
    assert session["inflight_turn"] is None
    assert clear_marker.called  # marker retired only after a durable commit


def test_persist_failure_downgrades_keeps_marker_and_reports_uncommitted(tmp_path):
    session = _session(tmp_path)
    agent = _agent(persist_raises=True)
    payload = {"text": "hi", "status": "complete"}
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained, committed = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", payload, {"final_response": "hi"}, "hi"
        )
    agent._persist_session.assert_called_once()
    assert status == "error"          # not acked as success
    assert retained is True
    assert committed is False         # caller's finally must NOT retire the marker
    assert payload["status"] == "error"
    assert payload["recoverable"] is True
    assert "could not be saved" in payload["error"]
    assert isinstance(session["inflight_turn"], dict)
    assert session["inflight_turn"]["status"] == "error"
    assert not clear_marker.called    # marker preserved for reconnect/resume


def test_persist_returns_false_downgrades_keeps_marker(tmp_path):
    """Silent partial write: ``_persist_session`` RETURNS False (the DB flush
    swallowed a per-row append error to keep the turn alive) rather than
    raising. This MUST be treated identically to a raise — not durably
    committed, downgraded to a recoverable error, marker preserved — or a
    dropped transcript row would be falsely acked as durable and lose its
    recovery marker."""
    session = _session(tmp_path)
    agent = MagicMock()
    agent._session_messages = [{"role": "user", "content": "hello"}]
    agent._persist_session = MagicMock(return_value=False)
    payload = {"text": "hi", "status": "complete"}
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained, committed = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", payload,
            {"final_response": "hi"}, "hi",
        )
    assert status == "error"
    assert committed is False
    assert retained is True
    assert payload["status"] == "error"
    assert payload["recoverable"] is True
    assert "could not be saved" in payload["error"]
    assert not clear_marker.called


def test_persist_returns_true_commits_and_retires_marker(tmp_path):
    """A truthful full-commit report (True) acks + retires the marker."""
    session = _session(tmp_path)
    agent = MagicMock()
    agent._session_messages = [{"role": "user", "content": "hello"}]
    agent._persist_session = MagicMock(return_value=True)
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained, committed = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", {"text": "hi", "status": "complete"},
            {"final_response": "hi"}, "hi",
        )
    assert status == "complete"
    assert committed is True
    assert clear_marker.called


def test_already_error_turn_persist_failure_keeps_marker(tmp_path):
    """An already-error turn whose best-effort persist ALSO fails must keep its
    marker too (Codex): the transcript is not durable, so recovery must remain."""
    session = _session(tmp_path)
    agent = _agent(persist_raises=True)
    payload = {"text": "", "status": "error"}
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained, committed = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "error", payload,
            {"error": "provider 4xx", "failed": True}, "",
        )
    assert status == "error"
    assert retained is True
    assert committed is False         # commit failed => marker NOT retired
    assert payload["recoverable"] is True
    assert payload["error"] == "provider 4xx"   # its own error, not a persist msg
    assert not clear_marker.called


def test_already_error_turn_persist_success_retires_marker(tmp_path):
    session = _session(tmp_path)
    agent = _agent(persist_raises=False)
    payload = {"text": "", "status": "error"}
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained, committed = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "error", payload,
            {"error": "provider 4xx", "failed": True}, "",
        )
    assert status == "error"
    assert retained is True
    assert committed is True          # partial state durable => marker retired
    assert clear_marker.called


def test_retry_after_failure_commits_once_and_then_acks(tmp_path):
    session = _session(tmp_path)
    agent = MagicMock()
    agent._session_messages = [{"role": "user", "content": "hello"}]
    agent._persist_session = MagicMock(side_effect=[RuntimeError("transient"), None])
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        _, _, committed1 = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", {"text": "hi", "status": "complete"},
            {"final_response": "hi"}, "hi",
        )
        assert committed1 is False
        assert not clear_marker.called
        status2, retained2, committed2 = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", {"text": "hi", "status": "complete"},
            {"final_response": "hi"}, "hi",
        )
    assert status2 == "complete"
    assert retained2 is False
    assert committed2 is True
    assert agent._persist_session.call_count == 2   # marker-dedup => at-most-once rows
    assert session["inflight_turn"] is None
    assert clear_marker.called


def test_no_session_messages_is_committed_and_retires_marker(tmp_path):
    session = _session(tmp_path)
    agent = MagicMock()
    agent._session_messages = None
    with patch.object(gw, "clear_turn_marker") as clear_marker:
        status, retained, committed = gw._finalize_turn_ack(
            session, agent, "sess-key-001", "complete", {"status": "complete"},
            {"final_response": "hi"}, "hi",
        )
    agent._persist_session.assert_not_called()
    assert status == "complete"
    assert retained is False
    assert committed is True          # nothing to persist counts as durable
    assert clear_marker.called


def test_terminal_turn_error_preserves_marker(tmp_path):
    """Exception path (Codex): the terminal-error frame must NOT retire the
    crash-recovery marker. A turn that died by exception (e.g. a transcript
    commit that raised) never durably committed, so the marker must survive for
    ``session.resume`` to auto-continue it — marker ownership follows durability,
    not the emission of a terminal frame. The caller's ``finally`` (gated on
    ``turn_durably_committed``, never set on this path) is the sole retirer."""
    session = _session(tmp_path)
    session["agent"] = None
    session["cols"] = 80
    with patch.object(gw, "clear_turn_marker") as clear_marker, patch.object(
        gw, "_emit"
    ) as emit:
        gw._emit_terminal_turn_error("sid-1", session, RuntimeError("disk full"))
    assert not clear_marker.called   # marker preserved on the exception path
    # A terminal, recoverable message.complete frame is still emitted.
    frames = [c.args[0] for c in emit.call_args_list if c.args]
    assert "message.complete" in frames
    assert isinstance(session["inflight_turn"], dict)  # retained for resume
