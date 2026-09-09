"""WAVE-30H — production-worker cooperative pause / checkpoint / resume (ADR-0004).

Hermetic tests of the REAL worker-side helpers now wired into the production
conversation loop (`agent.conversation_loop`) — no model, no subprocess. They
prove the production contract:

  * pause at a turn boundary persists a RESTORABLE checkpoint and blocks the task
    (NOT a cancel — the task goes to `blocked`, resumable);
  * resume restores the EXACT saved conversation, which already contains every
    completed tool call + its result, so NO tool is re-executed;
  * an un-serializable / oversized conversation FAILS OPEN (the run keeps going
    rather than blocking un-resumably) and records why;
  * a non-managed run is a complete no-op.
"""
import pytest

from youtab_agent_cli import kanban_db as kb
from youtab_runtime import run_control as rc
from agent.conversation_loop import (
    _maybe_checkpoint_and_pause,
    _maybe_restore_conversation,
)

# A conversation paused AFTER a tool call completed: the tool result is already
# present, so replaying these messages re-executes nothing.
CONV = [
    {"role": "user", "content": "work kanban task X"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "c1", "function": {"name": "kanban_show", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "task details here"},
    {"role": "assistant", "content": "planning the next step"},
]


@pytest.fixture()
def managed_task(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path / "home"))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="work", created_by="u", tenant="t")
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", tid)
    return tid


def _kinds(tid):
    with kb.connect_closing() as conn:
        return [getattr(e, "kind", None) for e in kb.list_events(conn, tid)]


def _status(tid):
    with kb.connect_closing() as conn:
        return kb.get_task(conn, tid).status


def _append(tid, kind, payload):
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind, payload)


# ── pure checkpoint serialization contract ────────────────────────────────────

def test_checkpoint_state_roundtrip():
    st = rc.conversation_checkpoint_state(CONV, iteration=3)
    assert st is not None and st["iteration"] == 3 and st["v"] == rc.CHECKPOINT_SCHEMA

    class _E:
        kind = rc.CHECKPOINT
        payload = {"state": st}

    assert rc.conversation_from_checkpoint([_E()]) == CONV


def test_checkpoint_state_unserializable_returns_none():
    bad = [{"role": "user", "content": {1, 2, 3}}]  # a set is not JSON-serializable
    assert rc.conversation_checkpoint_state(bad) is None


def test_conversation_from_checkpoint_none_when_absent():
    assert rc.conversation_from_checkpoint([]) is None


# ── worker-side production wiring ──────────────────────────────────────────────

def test_no_pause_is_noop(managed_task):
    assert _maybe_checkpoint_and_pause(CONV, 1) is False
    assert rc.CHECKPOINT not in _kinds(managed_task)
    assert _status(managed_task) != "blocked"


def test_pause_checkpoints_blocks_and_restores_exact_state(managed_task):
    _append(managed_task, rc.PAUSE, {"by": "u"})  # as /pause appends
    assert _maybe_checkpoint_and_pause(CONV, 2) is True
    assert rc.CHECKPOINT in _kinds(managed_task)
    assert _status(managed_task) == "blocked"      # blocked (paused), NOT cancelled
    # a fresh worker restores the EXACT conversation -> no tool re-execution
    assert _maybe_restore_conversation() == CONV


def test_resume_signal_clears_pause(managed_task):
    _append(managed_task, rc.PAUSE, {"by": "u"})
    _append(managed_task, rc.RESUME, {"by": "u"})
    # after /resume the pause is no longer pending -> worker keeps running
    assert _maybe_checkpoint_and_pause(CONV, 1) is False


def test_unserializable_conversation_fails_open(managed_task):
    _append(managed_task, rc.PAUSE, {"by": "u"})
    bad = [{"role": "user", "content": {1, 2}}]  # not serializable
    # fail-open: do NOT pause (would strand the run), keep running, record why
    assert _maybe_checkpoint_and_pause(bad, 1) is False
    kinds = _kinds(managed_task)
    assert rc.CHECKPOINT not in kinds
    assert "run_checkpoint_failed" in kinds
    assert _status(managed_task) != "blocked"


def test_not_managed_run_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_TASK", raising=False)
    assert _maybe_checkpoint_and_pause(CONV, 1) is False
    assert _maybe_restore_conversation() is None
