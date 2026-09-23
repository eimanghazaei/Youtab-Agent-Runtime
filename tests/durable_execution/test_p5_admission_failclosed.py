"""P5 — persist-before-ack: server durable mode fail-closes /v1/runs admission.

If the required server durable store cannot record a run at admission (e.g.
PostgreSQL lost while the API is up), POST /v1/runs must return 503 and NOT
dispatch an in-memory-only run — otherwise a 202'd run with no durable record is
lost on process death. The default local/sqlite path (no explicit server
backend) keeps its best-effort posture and is NOT blocked.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import MagicMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _adapter():
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _app(adapter):
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    return app


class _DeadStore:
    def create_run(self, *a, **k):
        raise RuntimeError("connection refused: PostgreSQL is down")

    def get_run(self, run_id):
        return None


@pytest.mark.asyncio
async def test_server_mode_admission_failclosed_when_store_dead(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    # Now flip to server durable mode with a dead store.
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = _DeadStore()
    created = []
    async with TestClient(TestServer(_app(adapter))) as cli:
        # If admission ever dispatched, _create_agent would be called; assert it
        # is NOT, and that we get 503 (not 202).
        with patch.object(adapter, "_create_agent",
                          side_effect=lambda *a, **k: created.append(1) or MagicMock()):
            r = await cli.post("/v1/runs", json={"input": "must not run"})
            assert r.status == 503
            body = await r.json()
            assert body["code"] == "durable_store_unavailable"
    # No agent was created — the run was never dispatched.
    assert created == []
    # No in-memory run leaked.
    assert adapter._run_statuses == {} or all(
        v.get("status") not in ("queued", "running")
        for v in adapter._run_statuses.values()
    )


@pytest.mark.asyncio
async def test_local_sqlite_path_not_blocked_by_barrier(monkeypatch, tmp_path):
    # No explicit server backend -> barrier is a no-op; the run is admitted even
    # if the (best-effort) store would error. Proves desktop is never blocked.
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    adapter = _adapter()
    adapter._run_store = _DeadStore()  # even a dead store must not block local
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "LOCAL_OK"}
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 1
    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            r = await cli.post("/v1/runs", json={"input": "hello"})
            assert r.status == 202  # admitted (best-effort persistence), not blocked
