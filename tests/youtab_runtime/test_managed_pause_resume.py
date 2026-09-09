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


def _events(tid):
    with kb.connect_closing() as conn:
        return kb.list_events(conn, tid)


def test_unserializable_conversation_fails_CLOSED(managed_task):
    # An accepted pause whose conversation cannot be serialized must FAIL CLOSED:
    # STOP (return True), emit run_pause_failed, block, persist NO checkpoint, and
    # refuse resume. It must NOT continue executing (return False was the bug).
    _append(managed_task, rc.PAUSE, {"by": "u"})
    bad = [{"role": "user", "content": {1, 2, 3}}]  # a set -> not JSON-serializable
    assert _maybe_checkpoint_and_pause(bad, 1) is True   # STOP, do not continue
    kinds = _kinds(managed_task)
    assert rc.CHECKPOINT not in kinds
    assert rc.PAUSE_FAILED_EVENT in kinds
    assert _status(managed_task) == "blocked"            # halted, non-terminal
    evs = _events(managed_task)
    assert rc.interactive_status(evs) == rc.PAUSE_FAILED
    assert rc.has_valid_checkpoint_for_resume(evs) is False
    assert _maybe_restore_conversation() is None         # nothing valid to resume


def test_oversized_conversation_fails_CLOSED(managed_task):
    _append(managed_task, rc.PAUSE, {"by": "u"})
    big = [{"role": "user", "content": "x" * 5_000_000}]  # exceeds the 4MB cap
    assert rc.conversation_checkpoint_state(big) is None   # cap rejects it
    assert _maybe_checkpoint_and_pause(big, 1) is True      # fail-closed STOP
    kinds = _kinds(managed_task)
    assert rc.CHECKPOINT not in kinds and rc.PAUSE_FAILED_EVENT in kinds
    assert _status(managed_task) == "blocked"


def test_corrupt_checkpoint_state_fails_CLOSED(managed_task, monkeypatch):
    # A checkpoint state that does not round-trip (integrity failure) must be
    # rejected -> pause_failed, never persisted as a valid checkpoint.
    _append(managed_task, rc.PAUSE, {"by": "u"})
    monkeypatch.setattr(rc, "conversation_checkpoint_state",
                        lambda msgs, iteration=None: {"v": 1, "messages_json": "{not-json"})
    assert _maybe_checkpoint_and_pause(CONV, 1) is True
    kinds = _kinds(managed_task)
    assert rc.CHECKPOINT not in kinds and rc.PAUSE_FAILED_EVENT in kinds
    assert _status(managed_task) == "blocked"


def test_db_write_failure_still_fails_CLOSED(managed_task, monkeypatch):
    # If persisting the pause outcome raises, the worker must STILL stop (never
    # continue model/tool execution after an accepted pause).
    _append(managed_task, rc.PAUSE, {"by": "u"})
    def boom(*a, **k):
        raise RuntimeError("db down")

    blocked = {"called": False}
    monkeypatch.setattr(kb, "_append_event", boom)  # every outcome write fails
    monkeypatch.setattr(kb, "block_task",
                        lambda *a, **k: blocked.__setitem__("called", True))
    # Must STILL stop (return True) and still ATTEMPT to block — never continue.
    assert _maybe_checkpoint_and_pause(CONV, 1) is True
    assert blocked["called"] is True


def test_no_execution_continues_after_any_accepted_pause_failure(managed_task, monkeypatch):
    # The load-bearing guarantee: for EVERY failure mode, once a pause is accepted
    # the helper returns True (the loop returns -> no further model/tool calls).
    _append(managed_task, rc.PAUSE, {"by": "u"})
    monkeypatch.setattr(rc, "conversation_checkpoint_state",
                        lambda msgs, iteration=None: None)  # force failure
    assert _maybe_checkpoint_and_pause(CONV, 7) is True     # never False after accepted pause


def test_side_effect_completed_before_pause_is_preserved_no_reexecution(managed_task):
    # Cooperative-boundary property: the pause is observed AFTER a tool result is
    # already in the conversation, so a valid checkpoint captures it and resume
    # replays it -> the side effect is neither lost nor re-executed.
    _append(managed_task, rc.PAUSE, {"by": "u"})
    assert _maybe_checkpoint_and_pause(CONV, 3) is True
    restored = _maybe_restore_conversation()
    assert restored == CONV
    assert any(m.get("role") == "tool" for m in restored)  # completed tool result present


def test_checkpoint_conversation_withheld_from_consumer_projection():
    # A run_checkpoint's messages_json (full conversation: tool args/results +
    # A9-scrubbed answer text) must NOT surface through the consumer event stream.
    from youtab_agent_cli.web_routers.runtime import _redact_control_payload
    import json as _json

    secret = "sk-super-secret-key-leaked-in-a-tool-result"
    payload = {"state": {"v": 1, "iteration": 2,
                         "messages_json": _json.dumps([{"role": "tool", "content": secret}])}}
    red = _redact_control_payload(rc.CHECKPOINT, payload)
    blob = _json.dumps(red)
    assert secret not in blob                              # conversation withheld
    assert "redacted" in red["state"]["messages_json"]
    assert red["state"]["iteration"] == 2                 # non-sensitive metadata kept
    # the raw durable event is unchanged (the worker restores from the log)
    assert secret in payload["state"]["messages_json"]


def test_not_managed_run_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_TASK", raising=False)
    assert _maybe_checkpoint_and_pause(CONV, 1) is False
    assert _maybe_restore_conversation() is None
