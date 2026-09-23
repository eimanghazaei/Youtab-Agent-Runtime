"""WAVE-26 agent 2: structured tool_call / tool_result events (contract 3).

Verifies that ``RunObserver.on_post_tool_call`` sinks the ``post_tool_call`` hook
payload into the journal as a ``tool_call`` + ``tool_result`` pair, that args and
results are redacted (never raw), that concurrent calls are distinguished by
``tool_call_id``, and that statuses are normalised to the contract vocabulary.
"""

from __future__ import annotations

import pytest

from youtab_runtime.run_journal import Principal, list_events
from youtab_runtime.run_observer import create_run_observer


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "run_journal.db"


P = Principal("tenant-a", "user-1")
Q = Principal("tenant-a", "user-2")


def _obs(run_id, db):
    return create_run_observer(run_id, P, db_path=db)


def test_tool_call_and_result_pair_emitted(db):
    obs = _obs("run1", db)
    call_ev, result_ev = obs.on_post_tool_call(
        tool_name="write_file",
        args={"path": "/tmp/report.txt", "content": "hello"},
        result='{"ok": true}',
        tool_call_id="tc-1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        duration_ms=42,
        status="ok",
    )
    assert call_ev is not None and result_ev is not None

    assert call_ev.category == "tool_call"
    assert call_ev.payload["tool_call_id"] == "tc-1"
    assert call_ev.payload["tool_name"] == "write_file"
    assert call_ev.payload["turn_id"] == "turn-1"
    assert call_ev.payload["api_request_id"] == "turn-1:api:1"
    # Args are redacted+bounded, not raw.
    assert call_ev.payload["args_redacted"]["tool_name"] == "write_file"
    assert call_ev.payload["args_redacted"]["args"]["path"] == "/tmp/report.txt"

    assert result_ev.category == "tool_result"
    assert result_ev.payload["tool_call_id"] == "tc-1"
    assert result_ev.payload["status"] == "ok"
    assert result_ev.payload["duration_ms"] == 42
    # Result is stored as a digest+summary, never raw verbatim beyond bounds.
    rd = result_ev.payload["result_digest"]
    assert "digest" in rd and len(rd["digest"]) == 64


def test_secret_args_are_redacted(db):
    obs = _obs("run1", db)
    call_ev, _ = obs.on_post_tool_call(
        tool_name="http_post",
        args={"url": "https://api.example.com", "api_key": "sk-super-secret-value"},
        result="ok",
        tool_call_id="tc-secret",
    )
    args = call_ev.payload["args_redacted"]["args"]
    assert args["api_key"] == "[redacted]"


def test_error_status_carries_redacted_error(db):
    obs = _obs("run1", db)
    _, result_ev = obs.on_post_tool_call(
        tool_name="run_query",
        args={},
        result='{"error": "boom"}',
        tool_call_id="tc-err",
        status="error",
        error_type="tool_error",
        error_message="database exploded",
    )
    assert result_ev.payload["status"] == "error"
    assert result_ev.payload["error_type"] == "tool_error"
    assert result_ev.payload["error_message_redacted"] == "database exploded"


@pytest.mark.parametrize(
    "raw_status,error_type,error_message,expected",
    [
        ("ok", None, None, "ok"),
        ("error", "tool_error", "x", "error"),
        ("blocked", "plugin_block", "denied", "error"),
        ("cancelled", None, None, "cancelled"),
        (None, "TimeoutError", "operation timed out", "timeout"),
        ("timeout", None, None, "timeout"),
        (None, None, "hard failure", "error"),
        (None, None, None, "ok"),
    ],
)
def test_status_normalisation(db, raw_status, error_type, error_message, expected):
    obs = _obs("run1", db)
    _, result_ev = obs.on_post_tool_call(
        tool_name="t",
        args={},
        result="",
        tool_call_id=f"tc-{expected}-{raw_status}",
        status=raw_status,
        error_type=error_type,
        error_message=error_message,
    )
    assert result_ev.payload["status"] == expected


def test_concurrent_calls_distinguished_by_tool_call_id(db):
    obs = _obs("run1", db)
    # Interleave two concurrent tool calls.
    obs._emit_tool_call(call_id="tc-A", name="a", args={}, turn_id="t", api_request_id="r")
    obs._emit_tool_call(call_id="tc-B", name="b", args={}, turn_id="t", api_request_id="r")
    obs._emit_tool_result(call_id="tc-B", name="b", result="rb", duration_ms=1,
                          status="ok", error_type=None, error_message=None)
    obs._emit_tool_result(call_id="tc-A", name="a", result="ra", duration_ms=2,
                          status="error", error_type="e", error_message="m")

    calls = list_events("run1", P, db_path=db, category="tool_call")
    results = list_events("run1", P, db_path=db, category="tool_result")
    assert {c.payload["tool_call_id"] for c in calls} == {"tc-A", "tc-B"}
    by_id = {r.payload["tool_call_id"]: r.payload["status"] for r in results}
    assert by_id == {"tc-A": "error", "tc-B": "ok"}
    # seq total-orders the interleaving.
    seqs = [e.seq for e in calls + results]
    assert len(set(seqs)) == 4


def test_tool_events_are_principal_scoped(db):
    _obs("run1", db).on_post_tool_call(
        tool_name="t", args={}, result="r", tool_call_id="tc-1", status="ok",
    )
    assert len(list_events("run1", P, db_path=db, category="tool_call")) == 1
    assert list_events("run1", Q, db_path=db, category="tool_call") == []
