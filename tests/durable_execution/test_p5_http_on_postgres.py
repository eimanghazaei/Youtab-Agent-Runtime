"""P5 — the SHIPPED /v1/runs handlers exercised against PostgreSQL (env-driven).

Proves the real api_server handlers use the canonical RunStore on a PostgreSQL
backend (same run_id/task_id), and that server mode FAILS CLOSED on a
misconfigured PostgreSQL rather than silently falling back to SQLite.
"""

import asyncio
import os
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import MagicMock, patch

psycopg = pytest.importorskip("psycopg")

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.api_server import APIServerAdapter  # noqa: E402

_ADMIN = os.environ.get("YOUTAB_TEST_PG_DSN",
                        "postgresql://youtab:devpass@127.0.0.1:55432/durable")


def _pg_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_reachable(),
                                reason="no reachable PostgreSQL (ENV-UNAVAILABLE)")


@pytest.fixture
def pg_dsn():
    dbname = "de_" + uuid.uuid4().hex[:12]
    with psycopg.connect(_ADMIN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    dsn = f'{_ADMIN.rsplit("/", 1)[0]}/{dbname}'
    try:
        yield dsn
    finally:
        with psycopg.connect(_ADMIN, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (dbname,))
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


def _app(adapter):
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/result", adapter._handle_run_result)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    return app


def _completing_agent():
    a = MagicMock()
    a.run_conversation.return_value = {"final_response": "PG_SHIPPED_OUTPUT"}
    a.session_prompt_tokens = a.session_completion_tokens = a.session_total_tokens = 1
    return a


@pytest.mark.asyncio
async def test_shipped_v1_runs_uses_postgres_and_survives_restart(pg_dsn, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_PG_DSN", pg_dsn)

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    # The shipped store is the PostgreSQL backend, not SQLite.
    assert type(adapter._run_store).__name__ == "PostgresRunStore"

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=_completing_agent()):
            resp = await cli.post("/v1/runs", json={"input": "hello"})
            assert resp.status == 202
            run_id = (await resp.json())["run_id"]
            for _ in range(100):
                s = await (await cli.get(f"/v1/runs/{run_id}")).json()
                if s["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.05)
            assert s["status"] == "completed"

    # The run is durable in PostgreSQL: a fresh adapter (restart) recovers it.
    # A restart means the prior instance is gone; while it held the exclusive
    # run authority a second instance could not start at all.
    await adapter.disconnect()
    adapter2 = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    async with TestClient(TestServer(_app(adapter2))) as cli2:
        g = await cli2.get(f"/v1/runs/{run_id}")
        assert g.status == 200
        body = await g.json()
        assert body.get("recovered_from_store") is True and body["status"] == "completed"
        ev = await cli2.get(f"/v1/runs/{run_id}/events?from_seq=0")
        assert ev.status == 200 and "status." in (await ev.text())

    # Confirm the row physically lives in PostgreSQL.
    with psycopg.connect(pg_dsn) as conn:
        n = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id=%s", (run_id,)).fetchone()[0]
    assert n == 1


def test_server_mode_fails_closed_on_bad_postgres(monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_PG_DSN",
                       "postgresql://nouser:nopass@127.0.0.1:5999/nodb?connect_timeout=2")
    # Explicit server backend + unreachable PostgreSQL -> fail closed (no SQLite fallback).
    with pytest.raises(Exception):
        APIServerAdapter(PlatformConfig(enabled=True, extra={}))
