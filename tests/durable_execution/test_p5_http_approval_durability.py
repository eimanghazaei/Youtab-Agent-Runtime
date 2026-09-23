"""HTTP approval may release a dangerous-command waiter only after durable CAS."""

import asyncio
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools import approval as approvals
from youtab_runtime.durable_run_store import RunIdentity, RunState, SqliteRunStore

BINDING = {"kind": "terminal_command", "arguments_digest": "a" * 64}
APPROVAL_ID = "A." + BINDING["arguments_digest"]


def _running(store, run_id):
    store.create_run(RunIdentity(
        task_id=run_id, run_id=run_id, tenant_id="local",
        organization_id="local", workspace_id="local",
        principal_id="local", agent_id="agent",
    ), initial_state=RunState.RUNNING)


@pytest_asyncio.fixture
async def approval_http(monkeypatch, tmp_path):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    adapter._run_store = store
    adapter._run_authority_held = lambda: True
    run_id = "run_http_approval"
    _running(store, run_id)
    adapter._run_statuses[run_id] = {"run_id": run_id, "status": "running"}
    adapter._run_approval_sessions[run_id] = run_id
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    app = web.Application()
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    async with TestClient(TestServer(app)) as client:
        yield adapter, store, run_id, client
    with approvals._lock:
        approvals._gateway_queues.pop(run_id, None)


def _enqueue(store, run_id, approval_id=APPROVAL_ID):
    store.open_approval(run_id, approval_id=approval_id,
                        payload={"effect_binding": BINDING})
    entry = approvals._ApprovalEntry({"approval_id": approval_id,
                                      "effect_binding": BINDING, "command": "danger"})
    with approvals._lock:
        approvals._gateway_queues[run_id] = [entry]
    return entry


@pytest.mark.asyncio
async def test_http_approval_commits_before_waiter_release(approval_http, monkeypatch):
    _, store, run_id, client = approval_http
    entry = _enqueue(store, run_id)
    original = store.decide_open_approval

    def observed(*args, **kwargs):
        assert not entry.event.is_set()
        row = original(*args, **kwargs)
        assert row["state"] == "RUNNING"
        assert not entry.event.is_set()
        return row

    monkeypatch.setattr(store, "decide_open_approval", observed)
    response = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": APPROVAL_ID, "effect_binding": BINDING})
    assert response.status == 200
    assert entry.event.is_set() and entry.result == "once"
    assert store.get_run(run_id)["state"] == "RUNNING"
    assert len([e for e in store.get_events(run_id) if e["kind"] == "approval_approved"]) == 1
    duplicate = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": APPROVAL_ID, "effect_binding": BINDING})
    assert duplicate.status == 409


@pytest.mark.asyncio
async def test_wrong_id_or_resolve_all_never_decides_or_releases(approval_http):
    _, store, run_id, client = approval_http
    entry = _enqueue(store, run_id)
    wrong = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": "B." + BINDING["arguments_digest"], "effect_binding": BINDING})
    bulk = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": APPROVAL_ID, "effect_binding": BINDING, "resolve_all": True})
    assert wrong.status == 409 and bulk.status == 400
    assert store.get_run(run_id)["state"] == "WAITING_APPROVAL"
    assert not entry.event.is_set()


@pytest.mark.asyncio
async def test_wrong_effect_or_standing_scope_never_releases(approval_http):
    _, store, run_id, client = approval_http
    entry = _enqueue(store, run_id)
    wrong_binding = {"kind": "terminal_command", "arguments_digest": "b" * 64}
    mismatch = await client.post(f"/v1/runs/{run_id}/approval", json={
        "choice": "once", "approval_id": APPROVAL_ID, "effect_binding": wrong_binding,
    })
    session = await client.post(f"/v1/runs/{run_id}/approval", json={
        "choice": "session", "approval_id": APPROVAL_ID, "effect_binding": BINDING,
    })
    always = await client.post(f"/v1/runs/{run_id}/approval", json={
        "choice": "always", "approval_id": APPROVAL_ID, "effect_binding": BINDING,
    })
    assert mismatch.status == 400
    assert session.status == 400 and always.status == 400
    assert store.get_run(run_id)["state"] == "WAITING_APPROVAL"
    assert not entry.event.is_set()


@pytest.mark.asyncio
async def test_store_failure_or_restarted_process_cannot_release_waiter(approval_http, monkeypatch):
    adapter, store, run_id, client = approval_http
    entry = _enqueue(store, run_id)

    def unavailable(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "decide_open_approval", unavailable)
    failed = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": APPROVAL_ID, "effect_binding": BINDING})
    assert failed.status == 503
    assert not entry.event.is_set() and store.get_run(run_id)["state"] == "WAITING_APPROVAL"
    # A new process can read the persisted pending approval but has no
    # in-memory waiter to authorize; it must not acknowledge a replay.
    adapter._run_approval_sessions.pop(run_id)
    restarted = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": APPROVAL_ID, "effect_binding": BINDING})
    assert restarted.status == 409
    assert store.get_run(run_id)["state"] == "WAITING_APPROVAL"


def test_late_mirror_cannot_replace_durable_terminal_result(monkeypatch, tmp_path):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    store = SqliteRunStore(str(tmp_path / "terminal.db"))
    adapter._run_store = store
    _running(store, "run_terminal")
    store.set_state("run_terminal", RunState.SUCCEEDED, strict=False,
                    result_ref="committed result")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")

    observed = adapter._set_run_status("run_terminal", "running")
    assert observed["status"] == "completed"
    assert observed["output"] == "committed result"
    late_terminal = adapter._set_run_status("run_terminal", "completed",
                                            output="uncommitted result")
    assert late_terminal["output"] == "committed result"
    assert store.get_run("run_terminal")["result_ref"] == "committed result"


def test_exact_effect_binding_uses_raw_input_and_context():
    # Display redaction cannot make two distinct effects share a consent.
    first = approvals._exact_effect_binding("terminal_command", "echo token=ONE", "local:False")
    second = approvals._exact_effect_binding("terminal_command", "echo token=TWO", "local:False")
    other_context = approvals._exact_effect_binding("terminal_command", "echo token=ONE", "host:True")
    assert first != second and first != other_context
    assert len(first["arguments_digest"]) == 64


def test_durable_mode_ignores_preexisting_permanent_command_grant(monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    monkeypatch.setattr(approvals, "_get_approval_mode", lambda: "ask")
    monkeypatch.setattr(approvals, "_command_matches_permanent_allowlist", lambda command: True)
    monkeypatch.setattr(approvals, "detect_dangerous_command",
                        lambda command: (True, "danger", "dangerous"))
    monkeypatch.setattr(approvals, "_run_approval_gate",
                        lambda **kwargs: {"approved": False, "reason": "fresh consent required"})
    result = approvals.check_dangerous_command("echo old-grant", "local")
    assert result == {"approved": False, "reason": "fresh consent required"}


@pytest.mark.asyncio
async def test_terminal_sse_uses_committed_result_after_losing_mirror_race(monkeypatch, tmp_path):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    store = SqliteRunStore(str(tmp_path / "sse.db"))
    adapter._run_store = store
    adapter._run_authority_held = lambda: True
    adapter._admit_durable_or_fail = lambda *args, **kwargs: None
    mock_agent = MagicMock()

    def finish_after_competing_commit(*, task_id, **kwargs):
        store.set_state(task_id, RunState.SUCCEEDED, strict=False,
                        result_ref="committed answer")
        return {"final_response": "losing local answer"}

    mock_agent.run_conversation.side_effect = finish_after_competing_commit
    adapter._create_agent = lambda **kwargs: mock_agent
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    async with TestClient(TestServer(app)) as client:
        accepted = await client.post("/v1/runs", json={"input": "test"})
        assert accepted.status == 202
        run_id = (await accepted.json())["run_id"]
        task = adapter._active_run_tasks[run_id]
        await asyncio.wait_for(task, timeout=5)
        q = adapter._run_streams[run_id]
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        terminals = [e for e in events if e and e.get("event") == "run.completed"]
        assert len(terminals) == 1
        assert terminals[0]["output"] == "committed answer"
        assert "losing local answer" not in repr(events)


@pytest.mark.asyncio
async def test_live_run_opens_exact_effect_before_notification(monkeypatch, tmp_path):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    store = SqliteRunStore(str(tmp_path / "live.db"))
    adapter._run_store = store
    adapter._run_authority_held = lambda: True
    adapter._admit_durable_or_fail = lambda *args, **kwargs: None
    agent = MagicMock()
    binding = approvals._exact_effect_binding("terminal_command", "delete exact target", "local:False")

    def run_with_approval(*, task_id, **kwargs):
        decision = approvals._await_gateway_decision(
            task_id, approvals._gateway_notify_cbs[task_id],
            {"command": "delete exact target", "description": "sensitive effect",
             "effect_binding": binding, "pattern_key": "danger", "pattern_keys": ["danger"]},
        )
        return {"final_response": f"choice={decision['choice']}"}

    agent.run_conversation.side_effect = run_with_approval
    adapter._create_agent = lambda **kwargs: agent
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    async with TestClient(TestServer(app)) as client:
        accepted = await client.post("/v1/runs", json={"input": "test"})
        assert accepted.status == 202
        run_id = (await accepted.json())["run_id"]
        q = adapter._run_streams[run_id]
        event = None
        for _ in range(100):
            if not q.empty():
                candidate = q.get_nowait()
                if candidate and candidate.get("event") == "approval.request":
                    event = candidate
                    break
            await asyncio.sleep(0.01)
        assert event is not None
        assert event["effect_binding"] == binding
        assert event["choices"] == ["once", "deny"]
        assert store.get_run(run_id)["state"] == "WAITING_APPROVAL"
        opened = [e for e in store.get_events(run_id) if e["kind"] == "approval_request"]
        assert len(opened) == 1
        assert opened[0]["payload"]["effect_binding"] == binding
        assert opened[0]["payload"]["approval_id"] == event["approval_id"]

        run_task = adapter._active_run_tasks[run_id]
        decision = await client.post(f"/v1/runs/{run_id}/approval", json={
            "choice": "once", "approval_id": event["approval_id"],
            "effect_binding": binding,
        })
        assert decision.status == 200
        await asyncio.wait_for(run_task, timeout=5)
        assert store.get_run(run_id)["state"] == "SUCCEEDED"


async def _exercise_live_gate(monkeypatch, tmp_path, gate):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    store = SqliteRunStore(str(tmp_path / "gated.db"))
    adapter._run_store = store
    adapter._run_authority_held = lambda: True
    adapter._admit_durable_or_fail = lambda *args, **kwargs: None
    agent = MagicMock()
    agent.run_conversation.side_effect = lambda *, task_id, **kwargs: {
        "final_response": str(gate(task_id)),
    }
    adapter._create_agent = lambda **kwargs: agent
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    async with TestClient(TestServer(app)) as client:
        accepted = await client.post("/v1/runs", json={"input": "test"})
        assert accepted.status == 202
        run_id = (await accepted.json())["run_id"]
        q = adapter._run_streams[run_id]
        event = None
        for _ in range(100):
            if not q.empty():
                candidate = q.get_nowait()
                if candidate and candidate.get("event") == "approval.request":
                    event = candidate
                    break
            await asyncio.sleep(0.01)
        assert event is not None
        assert store.get_run(run_id)["state"] == "WAITING_APPROVAL"
        run_task = adapter._active_run_tasks[run_id]
        response = await client.post(f"/v1/runs/{run_id}/approval", json={
            "choice": "once", "approval_id": event["approval_id"],
            "effect_binding": event["effect_binding"],
        })
        assert response.status == 200
        await asyncio.wait_for(run_task, timeout=5)
        assert store.get_run(run_id)["state"] == "SUCCEEDED"
        return event, store.get_run(run_id)["result_ref"]


@pytest.mark.asyncio
async def test_live_plugin_approval_binds_tool_arguments(monkeypatch, tmp_path):
    import youtab_agent_cli.plugins as plugins

    monkeypatch.setattr(plugins, "invoke_hook", lambda hook_name, **kwargs: [
        {"action": "approve", "message": "sensitive write", "rule_key": "write-file"}
    ])

    def gate(run_id):
        return plugins.resolve_pre_tool_block(
            "write_file", {"path": "/tmp/target", "content": "exact bytes"},
            task_id=run_id, tool_call_id="call-write-1",
        )

    event, result = await _exercise_live_gate(monkeypatch, tmp_path, gate)
    assert event["effect_binding"] == approvals._exact_effect_binding(
        "plugin_tool_call",
        {"tool_name": "write_file", "arguments": {"path": "/tmp/target", "content": "exact bytes"}},
        {"task_id": event["run_id"], "session_id": "", "tool_call_id": "call-write-1",
         "turn_id": "", "api_request_id": "", "approval_rule_key": "write-file",
         "approval_reason": "sensitive write"},
    )
    assert result == "None"  # None means the plugin allowed exactly this call.


@pytest.mark.asyncio
async def test_live_mcp_elicitation_binds_server_message_and_schema(monkeypatch, tmp_path):
    schema = {"properties": {"approved": {"type": "boolean"}}}

    def gate(run_id):
        return approvals.request_elicitation_consent(
            "authorize payment", "approval requested",
            surface="mcp-elicitation/pay", server_name="pay",
            requested_schema=schema,
        )

    event, result = await _exercise_live_gate(monkeypatch, tmp_path, gate)
    assert event["effect_binding"]["kind"] == "mcp_elicitation"
    assert event["effect_binding"] == approvals._exact_effect_binding(
        "mcp_elicitation",
        {"server_name": "pay", "message": "authorize payment", "requested_schema": schema},
        {"surface": "mcp-elicitation/pay", "session_key": event["run_id"]},
    )
    assert result == "accept"


def test_durable_plugin_approval_without_exact_arguments_fails_closed(monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    assert approvals.request_tool_approval("write_file", "sensitive")["approved"] is False
