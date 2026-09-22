"""P0-H BASELINE CHARACTERIZATION — Web/Electron reconnect over the REAL HTTP path.

Stronger than P0-A (which was component-level): this drives the real aiohttp
handlers `_handle_get_run` / `_handle_run_events` over a real TestServer/TestClient.

Defects proven:
- [C-WEB-2] `/v1/runs` SSE is single-consumer with NO reconnect-from-last-sequence:
  `_handle_run_events` pops the stream queue in its `finally`
  (`api_server.py:6541`), so a second subscription returns 404 — a reconnecting
  client loses all further events and cannot resume from a cursor / Last-Event-ID.
- [C-WEB-1] restart loss at the transport level: a fresh backend (new adapter)
  returns 404 for the same run id — no durable task/event survival.

Backend-contract ownership: this session may implement the backend durable
task/event/reconnect contract. It must NOT edit frontend/Electron UI files — the
UI is a separate consumer handoff (see the typed consumer contract in
docs/evidence/DURABLE_EXECUTION_LANE_INTERFACE_REQUESTS.md).
"""

import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _app(adapter):
    app = web.Application()
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    return app


@pytest.mark.asyncio
async def test_no_reconnect_from_sequence_and_restart_loss():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    run_id = "run_p0h"

    # Seed an accepted run + a live event stream (as a running turn would).
    adapter._set_run_status(run_id, "running")
    q: "asyncio.Queue" = asyncio.Queue()
    adapter._run_streams[run_id] = q
    adapter._run_streams_created[run_id] = time.time()
    await q.put({"type": "message.delta", "seq": 1, "text": "hello"})
    await q.put({"type": "message.delta", "seq": 2, "text": "world"})
    await q.put(None)  # terminal sentinel -> server closes the stream

    server = TestServer(_app(adapter))
    client = TestClient(server)
    await client.start_server()
    try:
        # Durable-id status retrieval works while in-memory.
        r = await client.get(f"/v1/runs/{run_id}")
        assert r.status == 200
        body = await r.json()
        assert body["status"] == "running"

        # First (only) consumer drains the SSE stream to completion.
        r1 = await client.get(f"/v1/runs/{run_id}/events")
        assert r1.status == 200
        text = await r1.text()
        assert '"seq": 1' in text and '"seq": 2' in text

        # RECONNECT: the stream queue was popped in the handler's finally, so a
        # second subscription cannot resume — it 404s. No Last-Event-ID / cursor.
        r2 = await client.get(f"/v1/runs/{run_id}/events")
        assert r2.status == 404, (
            "DEFECT [C-WEB-2]: reconnect returns 404 — no reconnect-from-sequence"
        )

        # RESTART: a fresh backend has no knowledge of the run id.
        fresh = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
        fresh_server = TestServer(_app(fresh))
        fresh_client = TestClient(fresh_server)
        await fresh_client.start_server()
        try:
            r3 = await fresh_client.get(f"/v1/runs/{run_id}")
            assert r3.status == 404, (
                "DEFECT [C-WEB-1]: accepted run is lost after backend restart"
            )
        finally:
            await fresh_client.close()
    finally:
        await client.close()
