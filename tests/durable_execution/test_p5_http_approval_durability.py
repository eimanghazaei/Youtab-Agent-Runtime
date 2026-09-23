"""HTTP approval may release a dangerous-command waiter only after durable CAS."""

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools import approval as approvals
from youtab_runtime.durable_run_store import RunIdentity, RunState, SqliteRunStore


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


def _enqueue(store, run_id, approval_id="A"):
    store.open_approval(run_id, approval_id=approval_id)
    entry = approvals._ApprovalEntry({"approval_id": approval_id, "command": "danger"})
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
    response = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": "A"})
    assert response.status == 200
    assert entry.event.is_set() and entry.result == "once"
    assert store.get_run(run_id)["state"] == "RUNNING"
    assert len([e for e in store.get_events(run_id) if e["kind"] == "approval_approved"]) == 1
    duplicate = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": "A"})
    assert duplicate.status == 409


@pytest.mark.asyncio
async def test_wrong_id_or_resolve_all_never_decides_or_releases(approval_http):
    _, store, run_id, client = approval_http
    entry = _enqueue(store, run_id)
    wrong = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": "B"})
    bulk = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": "A", "resolve_all": True})
    assert wrong.status == 409 and bulk.status == 400
    assert store.get_run(run_id)["state"] == "WAITING_APPROVAL"
    assert not entry.event.is_set()


@pytest.mark.asyncio
async def test_store_failure_or_restarted_process_cannot_release_waiter(approval_http, monkeypatch):
    adapter, store, run_id, client = approval_http
    entry = _enqueue(store, run_id)

    def unavailable(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "decide_open_approval", unavailable)
    failed = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": "A"})
    assert failed.status == 503
    assert not entry.event.is_set() and store.get_run(run_id)["state"] == "WAITING_APPROVAL"
    # A new process can read the persisted pending approval but has no
    # in-memory waiter to authorize; it must not acknowledge a replay.
    adapter._run_approval_sessions.pop(run_id)
    restarted = await client.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "approval_id": "A"})
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
