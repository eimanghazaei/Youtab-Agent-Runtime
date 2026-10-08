"""The managed-run progress feed must work on the codex app-server runtime.

`run_conversation` returns into `agent._run_codex_app_server_turn` before the
tool executor is reached, so `agent/tool_executor._emit_managed_progress`
never runs here. A managed worker with `openai_runtime=codex_app_server` had
a heartbeat-only feed: no steps at all.

The emission site is `_fire_tool_completed` in the event bridge, which is the
right seam rather than `item/started` because codex asks for approval AFTER
an item starts -- a step emitted there would claim work for a command the
user may still decline.

These tests drive the real bridge against a real kanban database and assert
on the rows that land, so they prove the gap is closed rather than that a
function was called.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.codex_runtime import make_codex_app_server_event_bridge
from youtab_agent_cli import kanban_db as kb


def _stub_agent() -> SimpleNamespace:
    """Mirrors the stub in test_codex_app_server_event_bridge.py."""
    return SimpleNamespace(
        tool_progress_callback=MagicMock(name="tool_progress_callback"),
        _fire_stream_delta=MagicMock(name="_fire_stream_delta"),
        _fire_reasoning_delta=MagicMock(name="_fire_reasoning_delta"),
        _emit_interim_assistant_message=MagicMock(name="_emit_interim"),
    )


def _completed(item: dict) -> dict:
    return {"method": "item/completed", "params": {"item": item}}


@pytest.fixture
def managed_run(tmp_path, monkeypatch):
    """A real kanban database with a claimed, running task."""
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect_closing() as conn:
        created = kb.create_task(conn, title="codex run", assignee="ops")
    task_id = created if isinstance(created, str) else getattr(created, "id", str(created))
    with kb.connect_closing() as conn:
        kb.claim_task(conn, task_id)
        run_id = kb.get_task(conn, task_id).current_run_id

    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_TASK", task_id)
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_RUN_ID", str(run_id))
    return SimpleNamespace(task_id=task_id, run_id=run_id)


def _steps(task_id: str) -> list[str]:
    with kb.connect_closing() as conn:
        return [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "runtime_step"
        ]


def test_a_successful_codex_command_records_a_step(managed_run):
    """The gap this closes: previously zero steps on this runtime."""
    bridge = make_codex_app_server_event_bridge(_stub_agent())

    bridge(_completed({
        "type": "commandExecution", "id": "cmd-1",
        "command": "pytest -q", "cwd": "/repo",
        "aggregatedOutput": "42 passed", "exitCode": 0,
    }))

    steps = _steps(managed_run.task_id)
    assert len(steps) == 1, steps
    # Same phrase the native `terminal` tool produces -- a managed run should
    # not read differently depending on which runtime executed the tool.
    assert "Running a command" in steps[0], steps[0]


@pytest.mark.parametrize("item,expected,why", [
    ({"type": "commandExecution", "id": "c1", "command": "pytest",
      "aggregatedOutput": "1 failed", "exitCode": 1},
     1, "an exitCode is the process's own result, so the command RAN"),
    ({"type": "commandExecution", "id": "c2", "command": "rm -rf /",
      "aggregatedOutput": ""},
     0, "no exitCode is no evidence it ran"),
    ({"type": "fileChange", "id": "p1", "status": "failed",
      "changes": [{"kind": {"type": "update"}, "path": "a.py"}]},
     1, "codex names a failure in `status`, and a failed patch was attempted"),
    ({"type": "fileChange", "id": "p2", "status": "declined",
      "changes": [{"kind": {"type": "update"}, "path": "a.py"}]},
     0, "a declined patch never ran"),
    ({"type": "mcpToolCall", "id": "m1", "server": "youtab-agent-tools",
      "tool": "web_search", "arguments": {"query": "x"},
      "error": {"message": "provider timed out"}},
     1, "a provider error means the call was made"),
])
def test_a_tool_that_ran_is_traced_even_when_it_failed(
    managed_run, item, expected, why
):
    """Dropping every error completion recreated the native-executor gap.

    The trace vanished exactly when something went wrong, which is when it is
    worth reading, and AGENTS.md's capability posture is explicit that a
    mitigation must preserve the feature -- trace included.

    `is_error` does not enter the decision at all, and that is the point: a
    `commandExecution` with no `exitCode` reports `is_error=False`, so gating
    on `not is_error` let exactly the never-ran case through while still
    dropping the ran-and-failed one. The two questions are asked separately
    instead -- was it refused, and did it run.
    """
    bridge = make_codex_app_server_event_bridge(_stub_agent())

    bridge(_completed(item))

    steps = _steps(managed_run.task_id)
    assert len(steps) == expected, f"{why}: {steps}"


def test_a_codex_file_change_records_the_basename(managed_run):
    """codex's fileChange shape has no `path` of its own.

    It is `{"changes": [{"kind", "path"}, ...]}`, so without a branch for it
    the step would be detail-less where the native `patch` step names the
    file. The basename is taken from the first change.
    """
    bridge = make_codex_app_server_event_bridge(_stub_agent())

    bridge(_completed({
        "type": "fileChange", "id": "patch-1", "status": "completed",
        "changes": [{"kind": {"type": "update"}, "path": "/repo/agent/loop.py"}],
    }))

    steps = _steps(managed_run.task_id)
    assert len(steps) == 1, steps
    assert "Editing a file" in steps[0], steps[0]
    assert "loop.py" in steps[0], steps[0]
    assert "/repo/agent" not in steps[0], "only the basename belongs in the feed"


def test_a_youtab_tool_through_the_internal_mcp_server_keeps_its_own_phrase(
    managed_run,
):
    """Internal-MCP items already carry BARE Youtab tool names.

    `_codex_item_to_tool_name` strips the `mcp.youtab-agent-tools.*`
    namespace by design, "since the user thinks of these as Youtab tools", so
    these match the existing phrase table and detail extractor with no
    translation -- which is why only codex's own built-ins needed new
    vocabulary.

    It is also the case with no double-emission risk to verify: those tools
    run in a separate youtab-agent-tools-mcp-server subprocess that never
    calls `emit_runtime_step`, so this is the only step for the call.
    """
    bridge = make_codex_app_server_event_bridge(_stub_agent())

    bridge(_completed({
        "type": "mcpToolCall", "id": "mcp-1",
        "server": "youtab-agent-tools", "tool": "web_search",
        "arguments": {"query": "sqlite wal reset bug"},
        "result": {"ok": True},
    }))

    steps = _steps(managed_run.task_id)
    assert len(steps) == 1, steps
    assert "Searching the web" in steps[0], steps[0]
    assert "sqlite wal reset bug" in steps[0], steps[0]


REFUSAL_JSON = json.dumps({
    "error": "Blocked: Youtab internal path",
    "authorization": "denied",
})


@pytest.mark.parametrize("result,why", [
    ({"content": [{"type": "text", "text": REFUSAL_JSON}]},
     "FastMCP hands a tool's JSON back as TEXT inside a content list"),
    (json.loads(REFUSAL_JSON),
     "a handler that returns the object directly"),
])
def test_an_mcp_refusal_from_the_internal_server_records_nothing(
    managed_run, result, why
):
    """`is_error` cannot see a handler-level refusal on this path.

    An internal MCP handler returns its payload as an ordinary string, so a
    policy refusal lands INSIDE `mcpToolCall.result` and never reaches the
    item's top-level `error`. `_codex_item_completion_payload` reports
    `is_error=False`, so without inspecting the result a step would be
    persisted -- with the original query in it -- for a search that never
    ran.

    Reachable through `model_tools.handle_function_call` rejecting
    `web_search` via `resolve_pre_tool_block`, and through Youtab's own
    browser and file guards, which return the same marked envelopes here.
    """
    bridge = make_codex_app_server_event_bridge(_stub_agent())

    bridge(_completed({
        "type": "mcpToolCall", "id": "mcp-refused",
        "server": "youtab-agent-tools", "tool": "web_search",
        "arguments": {"query": "internal admin credentials"},
        "result": result,
    }))

    steps = _steps(managed_run.task_id)
    assert steps == [], f"{why}: {steps}"


def test_an_external_mcp_tool_cannot_suppress_its_own_step(managed_run):
    """Tool results are untrusted input, so the marker is channel-scoped.

    A remote MCP tool that fetches or echoes arbitrary JSON could otherwise
    return a nested `{"authorization": "denied"}` and delete its own row from
    the durable trace with content it controls. Only Youtab's own handler
    channel -- the internal MCP server -- may assert a refusal.
    """
    bridge = make_codex_app_server_event_bridge(_stub_agent())

    bridge(_completed({
        "type": "mcpToolCall", "id": "mcp-external",
        "server": "some-remote-server", "tool": "fetch_json",
        "arguments": {"url": "https://example.test/x"},
        "result": {"content": [{"type": "text", "text": REFUSAL_JSON}]},
    }))

    steps = _steps(managed_run.task_id)
    assert len(steps) == 1, (
        "an external tool's content must not suppress the trace: " + str(steps)
    )


def test_nothing_is_recorded_outside_a_managed_run(tmp_path, monkeypatch):
    """`emit_runtime_step` is a no-op without a task, and must stay one.

    The bridge runs on every codex turn, managed or not, so an unguarded
    emission here would try to open a kanban database for ordinary CLI use.
    """
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_TASK", raising=False)
    monkeypatch.delenv("YOUTAB_AGENT_KANBAN_RUN_ID", raising=False)

    agent = _stub_agent()
    bridge = make_codex_app_server_event_bridge(agent)

    bridge(_completed({
        "type": "commandExecution", "id": "cmd-3",
        "command": "ls", "aggregatedOutput": "", "exitCode": 0,
    }))

    # The display callback still fired -- only the feed row is absent.
    assert agent.tool_progress_callback.called


def test_a_raising_emitter_cannot_break_the_turn_loop(managed_run, monkeypatch):
    """Progress reporting is guarded like every other callback in the bridge.

    A buggy emitter must not tear down the codex turn, and the display
    callbacks must still fire.
    """
    import agent.conversation_loop as conversation_loop

    def boom(*_args, **_kwargs):
        raise RuntimeError("emitter exploded")

    monkeypatch.setattr(conversation_loop, "emit_runtime_step", boom)
    agent = _stub_agent()
    bridge = make_codex_app_server_event_bridge(agent)

    bridge(_completed({
        "type": "commandExecution", "id": "cmd-4",
        "command": "ls", "aggregatedOutput": "", "exitCode": 0,
    }))

    assert agent.tool_progress_callback.called
    assert _steps(managed_run.task_id) == []


def test_the_display_callbacks_still_receive_their_original_arguments(managed_run):
    """The emission must not disturb the existing callback contract.

    `_fire_tool_completed` was edited to hoist the args out of the
    `complete_cb` branch so both consumers share them; this pins that the
    display callback still gets exactly what it got before.
    """
    agent = _stub_agent()
    agent.tool_complete_callback = MagicMock(name="tool_complete_callback")
    bridge = make_codex_app_server_event_bridge(agent)

    item = {
        "type": "commandExecution", "id": "cmd-5",
        "command": "ls -la", "cwd": "/repo",
        "aggregatedOutput": "total 0", "exitCode": 0,
    }
    bridge({"method": "item/started", "params": {"item": item}})
    bridge(_completed(item))

    progress = [c for c in agent.tool_progress_callback.call_args_list]
    assert [c.args[0] for c in progress] == ["tool.started", "tool.completed"]

    assert agent.tool_complete_callback.call_count == 1
    _call_id, name, args, result = agent.tool_complete_callback.call_args.args
    assert name == "exec_command"
    assert args == {"command": "ls -la", "cwd": "/repo"}
    assert result == "total 0"
