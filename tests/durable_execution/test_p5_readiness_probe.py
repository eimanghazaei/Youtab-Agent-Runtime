"""P5 — durable-store readiness probe (/health/ready) is store-aware, fail-closed.

/health is a static liveness OK and MUST NOT be treated as durable-store
readiness. /health/ready actually round-trips the RunStore: it returns 200 only
when the store answers, 503 (ready:false) when the store is unreachable (e.g.
PostgreSQL lost while the API is up), and never leaks the DSN/credentials through
the probe body.
"""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _adapter():
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _app(adapter):
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_get("/health/ready", adapter._handle_ready)
    return app


class _OkStore:
    def get_run(self, run_id):
        return None  # missing id -> None, but a real round-trip succeeded


class _DeadStore:
    def __init__(self, secret):
        self._secret = secret

    def get_run(self, run_id):
        # Emulate psycopg raising with the DSN/password in the message — the
        # probe must NOT surface this.
        raise RuntimeError(
            f"connection failed: host=db password={self._secret} could not connect"
        )


@pytest.mark.asyncio
async def test_ready_ok_when_store_answers(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = _OkStore()
    async with TestClient(TestServer(_app(adapter))) as cli:
        r = await cli.get("/health/ready")
        assert r.status == 200
        body = await r.json()
        assert body["ready"] is True
        assert body["durable_store"] == "ok"
        assert body["backend"] == "postgres"


@pytest.mark.asyncio
async def test_ready_503_and_no_secret_leak_when_store_dead(monkeypatch):
    secret = "sup3r-s3cret-pw"
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = _DeadStore(secret)
    async with TestClient(TestServer(_app(adapter))) as cli:
        r = await cli.get("/health/ready")
        assert r.status == 503
        raw = await r.text()
        body = await r.json()
        assert body["ready"] is False
        assert body["durable_store"] == "unavailable"
        # The probe must never leak the DSN/password.
        assert secret not in raw
        assert "password" not in raw.lower()


@pytest.mark.asyncio
async def test_liveness_health_is_static_and_not_store_aware(monkeypatch):
    # /health stays 200 even with a dead store: it is liveness, not readiness.
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "postgres")
    adapter._run_store = _DeadStore("x")
    async with TestClient(TestServer(_app(adapter))) as cli:
        r = await cli.get("/health")
        assert r.status == 200
        body = await r.json()
        assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_reports_none_when_no_store(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", raising=False)
    adapter = _adapter()
    adapter._run_store = None
    async with TestClient(TestServer(_app(adapter))) as cli:
        r = await cli.get("/health/ready")
        assert r.status == 200
        body = await r.json()
        assert body["durable_store"] == "none"
