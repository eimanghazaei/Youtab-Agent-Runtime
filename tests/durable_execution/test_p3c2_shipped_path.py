"""Phase 3-C: durability through the SHIPPED api_server.py /v1/runs handlers.

Exercises the REAL handlers (_handle_runs/_handle_get_run/_handle_run_result/
_handle_run_events/_handle_stop_run) wired to the canonical RunStore — NOT the
separate durable test app. Proves: a run started via the real POST /v1/runs
survives a backend "restart" (a fresh adapter on the same YOUTAB_AGENT_HOME still
serves it from the durable store), its result and event timeline are retrievable
after restart, and an explicit stop records a durable authorized-cancel intent.
"""

import asyncio
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import MagicMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture
def youtab_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    return home


def _adapter():
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _app(adapter):
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/result", adapter._handle_run_result)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _completing_agent():
    a = MagicMock()
    a.run_conversation.return_value = {"final_response": "SHIPPED_OUTPUT"}
    a.session_prompt_tokens = 1
    a.session_completion_tokens = 1
    a.session_total_tokens = 2
    return a


@pytest.mark.asyncio
async def test_run_survives_restart_and_result_events_are_durable(youtab_home):
    adapter1 = _adapter()
    async with TestClient(TestServer(_app(adapter1))) as cli:
        with patch.object(adapter1, "_create_agent", return_value=_completing_agent()):
            resp = await cli.post("/v1/runs", json={"input": "hello"})
            assert resp.status == 202
            run_id = (await resp.json())["run_id"]
            # Wait for the background run to complete.
            for _ in range(100):
                s = await (await cli.get(f"/v1/runs/{run_id}")).json()
                if s["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.05)
            assert s["status"] == "completed"

    # "Backend restart": a brand-new adapter on the SAME YOUTAB_AGENT_HOME.
    adapter2 = _adapter()
    async with TestClient(TestServer(_app(adapter2))) as cli2:
        # The in-memory cache is empty on adapter2; it must recover from the store.
        g = await cli2.get(f"/v1/runs/{run_id}")
        assert g.status == 200, "accepted run must survive backend restart"
        body = await g.json()
        assert body.get("recovered_from_store") is True
        assert body["status"] == "completed"

        # Durable result by the same run id after restart.
        r = await cli2.get(f"/v1/runs/{run_id}/result")
        assert r.status == 200
        assert (await r.json())["terminal"] is True

        # Durable event replay after restart — NOT a 404, monotonic seq timeline.
        ev = await cli2.get(f"/v1/runs/{run_id}/events?from_seq=0")
        assert ev.status == 200
        text = await ev.text()
        assert "status." in text and "id:" in text


@pytest.mark.asyncio
async def test_explicit_stop_records_durable_cancel_intent(youtab_home):
    adapter = _adapter()
    release = threading.Event()

    def _blocking_agent():
        a = MagicMock()

        def _run(*args, **kwargs):
            release.wait(5.0)
            return {"final_response": "stopped"}

        a.run_conversation.side_effect = _run
        a.interrupt.side_effect = lambda *a_, **k_: release.set()
        a.session_prompt_tokens = a.session_completion_tokens = a.session_total_tokens = 0
        return a

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=_blocking_agent()):
            run_id = (await (await cli.post("/v1/runs", json={"input": "hi"})).json())["run_id"]
            await asyncio.sleep(0.2)
            stop = await cli.post(f"/v1/runs/{run_id}/stop")
            assert stop.status == 200
            # Durable authorized-cancel intent is recorded in the store.
            row = adapter._run_store.get_run(run_id)
            assert row is not None and row["cancel_requested_at"] is not None
            release.set()
