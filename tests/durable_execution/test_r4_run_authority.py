"""R4 — the durable /v1/runs server is a store-enforced singleton.

Server durable mode acquires an exclusive run authority before readiness
(PostgreSQL session advisory lock on a supervised connection; SQLite OS file
lock), stamps runs ``inst:<instance_id>:<epoch>`` instead of a numeric PID, and
fences every durable write on that authority. These tests prove the contract
behaviorally: a competing instance cannot start or touch the holder's runs, a
prior instance's runs become UNKNOWN only once exclusivity is proven (even when
the PID was reused), and after authority loss there is no admission, no
readiness and no durable terminal success, only fail-stop and an explicit
unresolved state for the next holder. PostgreSQL cases run against a real
server and are skipped as ENV-UNAVAILABLE (not a pass) when none is reachable.
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from youtab_runtime.durable_run_authority import AuthorityHeld, AuthorityLost
from youtab_runtime.durable_run_store import RunIdentity, RunState, SqliteRunStore

_BACKEND = "YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND"


def _ident(run_id, operation="run"):
    return RunIdentity(task_id=run_id, run_id=run_id, tenant_id="local",
                       organization_id="local", workspace_id="local",
                       principal_id="local", agent_id="agent", operation=operation)


def _app(adapter):
    app = web.Application()
    app.router.add_get("/health/ready", adapter._handle_ready)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/result", adapter._handle_run_result)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    return app


def _sqlite_adapter(monkeypatch, store):
    """Construct in local mode, then enter server durable mode on ``store``."""
    monkeypatch.delenv(_BACKEND, raising=False)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    monkeypatch.setenv(_BACKEND, "sqlite")
    adapter._run_store = store
    adapter._reconcile_orphaned_runs_on_startup()
    return adapter


# --------------------------------------------------------------------------- #
# SQLite (always runs)
# --------------------------------------------------------------------------- #
def test_sqlite_competing_instance_refused_and_epoch_fences(tmp_path):
    db = tmp_path / "runs.db"
    first = SqliteRunStore(db)
    a = first.acquire_instance_authority()
    second = SqliteRunStore(db)
    with pytest.raises(AuthorityHeld):
        second.acquire_instance_authority()

    first.admit(_ident("r1"), owner=a.owner, initial_state=RunState.RUNNING)
    a.release()
    with pytest.raises(AuthorityLost):  # released authority can no longer write
        first.set_state("r1", RunState.SUCCEEDED, strict=False, result_ref="late")

    b = second.acquire_instance_authority()
    try:
        assert b.epoch == a.epoch + 1 and b.owner != a.owner
        assert second.reconcile_prior_instances(b) == ["r1"]
        assert second.get_run("r1")["state"] == "UNKNOWN"
    finally:
        b.release()


def test_sqlite_reconcile_leaves_ownerless_terminal_and_other_operations(tmp_path):
    store = SqliteRunStore(tmp_path / "runs.db")
    store.create_run(_ident("queued_ownerless"))
    store.admit(_ident("done"), owner=f"pid:{os.getpid()}", initial_state=RunState.RUNNING)
    store.set_state("done", RunState.SUCCEEDED, strict=False, result_ref="COMMITTED")
    store.admit(_ident("child", operation="delegate"), owner="pid:1")
    store.admit(_ident("orphan"), owner=f"pid:{os.getpid()}", initial_state=RunState.QUEUED)

    authority = store.acquire_instance_authority()
    try:
        assert store.reconcile_prior_instances(authority) == ["orphan"]
        assert store.get_run("queued_ownerless")["state"] == "QUEUED"
        done = store.get_run("done")
        assert done["state"] == "SUCCEEDED" and done["result_ref"] == "COMMITTED"
        assert store.get_run("child")["state"] == "RUNNING"
        assert store.get_run("orphan")["state"] == "UNKNOWN"
        kinds = [e["kind"] for e in store.get_events("orphan")]
        assert kinds[-1] == "state.unknown"
    finally:
        authority.release()


@pytest.mark.asyncio
async def test_sqlite_superseded_authority_fail_stops_and_commits_nothing(
        monkeypatch, tmp_path, authority_fail_stops):
    db = tmp_path / "runs.db"
    adapter = _sqlite_adapter(monkeypatch, SqliteRunStore(db))
    store = adapter._run_store
    assert adapter._admit_durable_or_fail("r_live", session_id="s", model="m") is None
    adapter._set_run_status("r_live", "queued", session_id="s", model="m")
    adapter._set_run_status("r_live", "running")
    assert store.get_run("r_live")["lease_owner"] == adapter._run_authority.owner

    # Another writer bumped the epoch: this instance is no longer the authority.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE runtime_authority SET epoch=epoch+1, instance_id='other'")

    final = adapter._set_run_status("r_live", "completed", output="MUST_NOT_COMMIT")
    assert final["status"] == "reconciliation_required"
    assert final.get("output") is None
    assert store.get_run("r_live")["state"] == "RUNNING"  # nothing terminal committed
    assert authority_fail_stops == [True]  # fail-stop fired exactly once

    denied = adapter._admit_durable_or_fail("r_next", session_id="s", model="m")
    assert denied is not None and denied.status == 503
    assert store.get_run("r_next") is None
    async with TestClient(TestServer(_app(adapter))) as cli:
        r = await cli.get("/health/ready")
        assert r.status == 503 and (await r.json())["durable_store"] == "no_run_authority"


# --------------------------------------------------------------------------- #
# PostgreSQL (real server)
# --------------------------------------------------------------------------- #
psycopg = pytest.importorskip("psycopg")

_ADMIN = os.environ.get("YOUTAB_TEST_PG_DSN",
                        "postgresql://youtab:devpass@127.0.0.1:55432/durable")


def _pg_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


_needs_pg = pytest.mark.skipif(not _pg_reachable(),
                               reason="no reachable PostgreSQL (ENV-UNAVAILABLE)")


@pytest.fixture
def pg_dsn(monkeypatch):
    dbname = "r4_" + uuid.uuid4().hex[:12]
    with psycopg.connect(_ADMIN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    dsn = f'{_ADMIN.rsplit("/", 1)[0]}/{dbname}'
    monkeypatch.setenv(_BACKEND, "postgres")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_PG_DSN", dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(_ADMIN, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                         (dbname,))
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


def _pg_adapter():
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _kill_authority_session(dsn, adapter):
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("SELECT pg_terminate_backend(%s)",
                            (adapter._run_authority.backend_pid,)).fetchone()[0]


def _wait(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@_needs_pg
@pytest.mark.asyncio
async def test_pg_second_instance_cannot_start_or_touch_holder_runs(pg_dsn):
    first = _pg_adapter()
    assert first._admit_durable_or_fail("run_a", session_id="s", model="m") is None
    first._set_run_status("run_a", "queued", session_id="s", model="m")
    first._set_run_status("run_a", "running")

    with pytest.raises(AuthorityHeld):
        _pg_adapter()

    row = first._run_store.get_run("run_a")
    assert row["state"] == "RUNNING" and row["lease_owner"] == first._run_authority.owner
    async with TestClient(TestServer(_app(first))) as cli:
        assert (await cli.get("/health/ready")).status == 200
    first._release_run_authority()


@_needs_pg
def test_pg_same_pid_prior_instance_recovered_after_exclusive_authority(pg_dsn):
    from youtab_runtime.durable_run_store_pg import PostgresRunStore

    seed = PostgresRunStore(pg_dsn)  # unfenced: models rows left by prior processes
    seed.admit(_ident("same_pid"), owner=f"pid:{os.getpid()}", initial_state=RunState.RUNNING)
    seed.admit(_ident("prior_inst"), owner="inst:" + "a" * 32 + ":4", initial_state=RunState.RUNNING)
    seed.create_run(_ident("ownerless"))
    seed.admit(_ident("done"), owner=f"pid:{os.getpid()}", initial_state=RunState.RUNNING)
    seed.set_state("done", RunState.SUCCEEDED, strict=False, result_ref="COMMITTED")

    adapter = _pg_adapter()
    store = adapter._run_store
    try:
        assert store.get_run("same_pid")["state"] == "UNKNOWN"
        assert store.get_run("prior_inst")["state"] == "UNKNOWN"
        assert store.get_run("ownerless")["state"] == "QUEUED"
        done = store.get_run("done")
        assert done["state"] == "SUCCEEDED" and done["result_ref"] == "COMMITTED"
    finally:
        adapter._release_run_authority()


@_needs_pg
@pytest.mark.asyncio
async def test_pg_lock_loss_fail_stops_and_next_holder_recovers(pg_dsn, authority_fail_stops):
    first = _pg_adapter()
    assert first._admit_durable_or_fail("run_x", session_id="s", model="m") is None
    first._set_run_status("run_x", "queued", session_id="s", model="m")
    first._set_run_status("run_x", "running")

    _kill_authority_session(pg_dsn, first)
    # Fenced write, before or after the supervisor notices: no terminal commit.
    final = first._set_run_status("run_x", "completed", output="MUST_NOT_COMMIT")
    assert final["status"] == "reconciliation_required" and final.get("output") is None
    assert _wait(lambda: authority_fail_stops == [True])
    assert not first._run_authority_held()

    async with TestClient(TestServer(_app(first))) as cli:
        assert (await cli.get("/health/ready")).status == 503
        with patch.object(first, "_create_agent") as create:
            resp = await cli.post("/v1/runs", json={"input": "after loss"})
            assert resp.status == 503
            create.assert_not_called()

    second = _pg_adapter()  # restart: exclusive authority, next epoch
    try:
        assert second._run_authority.epoch == first._run_authority.epoch + 1
        row = second._run_store.get_run("run_x")
        assert row["state"] == "UNKNOWN" and row["result_ref"] is None
        # A late write from the superseded instance is still refused.
        with pytest.raises(AuthorityLost):
            first._run_store.set_state("run_x", RunState.SUCCEEDED, strict=False,
                                       result_ref="LATE")
        assert second._run_store.get_run("run_x")["state"] == "UNKNOWN"
    finally:
        second._release_run_authority()


def _sse_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


@_needs_pg
@pytest.mark.asyncio
async def test_pg_http_sse_terminal_follows_commit_and_lock_loss_withholds_success(
        pg_dsn, authority_fail_stops):
    adapter = _pg_adapter()
    release = threading.Event()

    def _blocking_run(*_a, **_k):
        release.wait(20)
        return {"final_response": "UNCOMMITTED_OUTPUT"}

    agent = MagicMock()
    agent.run_conversation.side_effect = _blocking_run
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 1
    done_agent = MagicMock()
    done_agent.run_conversation.return_value = {"final_response": "COMMITTED_OUTPUT"}
    done_agent.session_prompt_tokens = done_agent.session_completion_tokens = 1
    done_agent.session_total_tokens = 1

    async with TestClient(TestServer(_app(adapter))) as cli:
        # 1) Healthy run: the terminal SSE event is emitted only after commit.
        with patch.object(adapter, "_create_agent", return_value=done_agent):
            resp = await cli.post("/v1/runs", json={"input": "ok"})
            assert resp.status == 202
            ok_id = (await resp.json())["run_id"]
            events = _sse_events(await (await cli.get(f"/v1/runs/{ok_id}/events")).text())
        assert "run.completed" in [e.get("event") for e in events]
        row = adapter._run_store.get_run(ok_id)
        assert row["state"] == "SUCCEEDED" and row["result_ref"] == "COMMITTED_OUTPUT"

        # 2) In-flight run loses the authority mid-execution.
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await cli.post("/v1/runs", json={"input": "long"})
            assert resp.status == 202
            run_id = (await resp.json())["run_id"]
            events_task = asyncio.create_task(cli.get(f"/v1/runs/{run_id}/events"))
            for _ in range(200):
                if agent.run_conversation.called:
                    break
                await asyncio.sleep(0.05)
            assert agent.run_conversation.called

            _kill_authority_session(pg_dsn, adapter)
            for _ in range(300):
                if authority_fail_stops:
                    break
                await asyncio.sleep(0.05)
            assert authority_fail_stops == [True]
            agent.interrupt.assert_called()  # fail-stop interrupts live agents
            release.set()
            events = _sse_events(await (await events_task).text())

        kinds = [e.get("event") for e in events]
        assert "run.completed" not in kinds
        assert "run.reconciliation_required" in kinds
        result = await (await cli.get(f"/v1/runs/{run_id}/result")).json()
        assert result["terminal"] is False and result["output"] is None
        assert adapter._run_store.get_run(run_id)["state"] == "RUNNING"

    # Restart: the next holder exposes explicit unresolved truth; committed
    # results stay stable. Nothing is resubmitted.
    restarted = _pg_adapter()
    try:
        async with TestClient(TestServer(_app(restarted))) as cli:
            lost = await (await cli.get(f"/v1/runs/{run_id}/result")).json()
            assert lost["status"] == "unknown" and lost["terminal"] is False
            assert lost["output"] is None
            ok = await (await cli.get(f"/v1/runs/{ok_id}/result")).json()
            assert ok["terminal"] is True and ok["output"] == "COMMITTED_OUTPUT"
        assert agent.run_conversation.call_count == 1  # no blind retry
    finally:
        restarted._release_run_authority()
