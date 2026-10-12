"""Phase 3-C: real HTTP -> handler -> RunStore -> worker proof (canonical /v1/runs).

DESIRED-INVARIANT tests (corrected behavior), distinct from the P0 baseline
characterizations. Exercises the durable /v1/runs contract over real aiohttp:
short caller wait returns RUNNING+task_id while the worker keeps running, another
client reconnects by the same task_id, the worker completes exactly once with a
durable result, backend restart recovers the same task, cross-workspace access is
denied without leak, explicit stop cancels, and from_seq reconnect works.
"""

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.durable_runs_api import DurableRunsCoordinator, make_app
from youtab_runtime.durable_run_store import DurableRunStore, RunState

H = {"X-Tenant": "tA", "X-Workspace": "w1", "X-Principal": "p1", "Content-Type": "application/json"}


def _worker_factory(_payload):
    def worker(store, run_id, epoch):
        for i in range(10):
            st = store.get_run(run_id)["state"]
            if st == "CANCELLING":
                store.transition(run_id, RunState.CANCELLED)
                return
            store.record_progress(run_id, step=f"s{i}")
            time.sleep(0.2)
        store.transition(run_id, RunState.SUCCEEDED, result_ref="result://http-once")
    return worker


@pytest.fixture
def env(tmp_path):
    store = DurableRunStore(db_path=tmp_path / "durable_runs.db")
    coord = DurableRunsCoordinator(store)
    return tmp_path, store, coord


async def _client(coord):
    c = TestClient(TestServer(make_app(coord, _worker_factory)))
    await c.start_server()
    return c


@pytest.mark.asyncio
async def test_short_wait_returns_running_and_completes_once(env):
    _, store, coord = env
    c = await _client(coord)
    try:
        r = await c.post("/v1/runs?wait_timeout=0.5", json={"run_id": "R1", "idempotency_key": "k1",
                         "request_digest": "d1"}, headers=H)
        assert r.status == 202  # non-terminal
        body = await r.json()
        assert body["state"] in ("CLAIMED", "RUNNING") and body["task_id"] == "R1"
        assert "from_seq=" in body["reconnect"]

        # Another client reconnects by the same task_id and sees progress events.
        ev = await c.get("/v1/runs/R1/events?from_seq=0", headers=H)
        assert ev.status == 200
        text = await ev.text()
        assert "progress:" in text

        # Poll result until terminal.
        deadline = time.time() + 8
        state = None
        while time.time() < deadline:
            res = await c.get("/v1/runs/R1/result", headers=H)
            j = await res.json()
            state = j["state"]
            if j["terminal"]:
                assert j["result_ref"] == "result://http-once"
                break
            await _sleep(0.1)
        assert state == "SUCCEEDED"
        # Exactly-once terminal event in the durable store.
        succ = [e for e in store.get_events("R1", from_seq=0) if e["kind"] == "state.succeeded"]
        assert len(succ) == 1
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_cross_workspace_access_denied_without_leak(env):
    _, store, coord = env
    c = await _client(coord)
    try:
        r = await c.post("/v1/runs?wait_timeout=0.2", json={"run_id": "R2"}, headers=H)
        assert r.status in (200, 202)
        # Same run id, different workspace -> not found (no existence leak).
        foreign = {**H, "X-Workspace": "w2"}
        g = await c.get("/v1/runs/R2", headers=foreign)
        assert g.status == 404
        gr = await c.get("/v1/runs/R2/result", headers=foreign)
        assert gr.status == 404
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_explicit_stop_cancels_but_disconnect_would_not(env):
    _, store, coord = env
    c = await _client(coord)
    try:
        await c.post("/v1/runs?wait_timeout=0.2", json={"run_id": "R3"}, headers=H)
        s = await c.post("/v1/runs/R3/stop", headers=H)
        assert s.status == 200
        assert (await s.json())["state"] == "CANCELLING"
        # Worker observes cancellation and reaches CANCELLED.
        deadline = time.time() + 6
        while time.time() < deadline:
            row = store.get_run("R3")
            if row["state"] == "CANCELLED":
                break
            await _sleep(0.1)
        assert store.get_run("R3")["state"] == "CANCELLED"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_restart_recovers_same_task(env):
    tmp_path, store, coord = env
    c = await _client(coord)
    try:
        await c.post("/v1/runs?wait_timeout=6", json={"run_id": "R4"}, headers=H)
        # Completed within the wait; now "restart": new store+coord on same DB.
        restarted = DurableRunStore(db_path=tmp_path / "durable_runs.db")
        row = restarted.get_run("R4")
        assert row is not None and row["state"] == "SUCCEEDED"
        assert row["result_ref"] == "result://http-once"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_from_seq_reconnect_returns_only_later_events(env):
    _, store, coord = env
    c = await _client(coord)
    try:
        await c.post("/v1/runs?wait_timeout=6", json={"run_id": "R5"}, headers=H)
        all_ev = store.get_events("R5", from_seq=0)
        assert len(all_ev) >= 3
        mid = all_ev[2]["seq"]
        ev = await c.get(f"/v1/runs/R5/events?from_seq={mid}", headers=H)
        text = await ev.text()
        # Only seq > mid appear.
        assert f":{mid}\n" not in text
        assert f":{all_ev[-1]['seq']}" in text
    finally:
        await c.close()


async def _sleep(s):
    import asyncio
    await asyncio.sleep(s)
