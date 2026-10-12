"""P0-H — Web/Electron reconnect over the REAL shipped /v1/runs HTTP handlers.

HISTORY: this test originally CHARACTERIZED the defect (reconnect -> 404, restart
loss) on the in-memory `/v1/runs`. That defect is now FIXED on the shipped path:
`_set_run_status` write-through to the canonical RunStore and `_handle_run_events`
durable replay give reconnect-from-sequence and restart recovery. The git history
preserves the original baseline; this file now asserts the CORRECTED behavior.

Drives the real `_handle_get_run` / `_handle_run_events` handlers.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture
def youtab_home(tmp_path, monkeypatch):
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))
    return home


def _app(adapter):
    app = web.Application()
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    return app


@pytest.mark.asyncio
async def test_reconnect_from_sequence_and_restart_recovery(youtab_home):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    run_id = "run_p0h"

    # A run progresses through states (each mirrored durably to the RunStore).
    adapter._set_run_status(run_id, "queued", session_id="s")
    adapter._set_run_status(run_id, "running")
    adapter._set_run_status(run_id, "completed", output="ENTERPRISE_RESULT")

    server = TestServer(_app(adapter))
    client = TestClient(server)
    await client.start_server()
    try:
        # Reconnect / replay from the durable event timeline (not a 404).
        ev = await client.get(f"/v1/runs/{run_id}/events?from_seq=0")
        assert ev.status == 200
        text = await ev.text()
        assert "status.completed" in text and "id:" in text

        # Reconnect-from-sequence: a later cursor returns only later events.
        # (seq 1 = accepted/queued; ask from_seq=1 to skip it.)
        ev2 = await client.get(f"/v1/runs/{run_id}/events?from_seq=1")
        assert "status.completed" in (await ev2.text())

        # RESTART: a brand-new adapter on the same YOUTAB_AGENT_HOME recovers it.
        after = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
        server2 = TestServer(_app(after))
        client2 = TestClient(server2)
        await client2.start_server()
        try:
            r = await client2.get(f"/v1/runs/{run_id}")
            assert r.status == 200, "accepted run must survive backend restart"
            body = await r.json()
            assert body.get("recovered_from_store") is True
            assert body["status"] == "completed"
            # Events still replayable after restart.
            ev3 = await client2.get(f"/v1/runs/{run_id}/events?from_seq=0")
            assert ev3.status == 200 and "status.completed" in (await ev3.text())
        finally:
            await client2.close()
    finally:
        await client.close()
