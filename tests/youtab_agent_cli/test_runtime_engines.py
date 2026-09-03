"""Focused tests for the branded product-engine surface (AR engine-connector).

Covers, credential-free and without any live ollama/deepseek dependency:

* ``GET /api/runtime/v1/engines`` returns the branded roster with HONEST
  availability (probe + credential check are patched, never live), and the
  projection NEVER leaks a provider/model/endpoint/secret;
* create-run with a known ``engine`` records the ``runtime_engine_selection``
  event and passes ``model_override``/``provider_override`` into ``create_task``;
  an unknown ``engine`` is 422; an absent ``engine`` is unchanged;
* ``agent_identity.engine_binding_for_profile`` inverts the binding correctly.

Only the availability probe (a network/credential side effect) and the worker
spawn are substituted — the router, auth, and binding resolution run for real.
"""
from __future__ import annotations

import json
import secrets
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from youtab_agent_cli import agent_identity
from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli import runtime_command_auth as rca
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.token_auth import token_auth_middleware
from youtab_agent_cli.web_routers import runtime

REPO_ROOT = str(Path(__file__).resolve().parents[2])
SECRET = secrets.token_urlsafe(48)


class _FakeProfile:
    def __init__(self, name):
        self.name = name
        self.description = "test agent"
        self.model = "local-deterministic"
        self.provider = "local"
        self.skill_count = 3
        self.is_default = name == "default"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db_path))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)

    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider
    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service", capability="runtime")

    monkeypatch.setattr(
        "youtab_agent_cli.profiles.list_profiles", lambda: [_FakeProfile("default")]
    )

    # A harmless spawn so a dispatch tick never launches a real worker.
    runtime._spawn_override = lambda task, workspace, board=None: 999999
    runtime._nonce_store = None
    runtime._engine_avail_cache.clear()

    # Availability side effects are patched OFF by default (nothing configured /
    # nothing reachable) so no test depends on a live ollama/deepseek. Individual
    # tests flip these to prove the online branch.
    monkeypatch.setattr(runtime, "_connection_reachable", lambda conn: False)
    monkeypatch.setattr(runtime, "_external_credential_present", lambda provider: False)

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)

    # Real startup ordering (WAVE-22): serve only from a VERIFIED generation.
    # A bare test app has no lifespan, so drive the isolated registry through
    # the real declare → freeze → verify → VERIFIED transition.
    token_auth.require_route_ownership(
        provider="runtime-service", path="/api/runtime/v1/", is_prefix=True,
        capability="runtime")
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()

    with TestClient(app) as c:
        yield c

    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None
    runtime._engine_avail_cache.clear()
    # Registry is VERIFIED (frozen) — clear_* is refused after freeze; the
    # autouse fresh-registry fixture provides per-test isolation.


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _identity_headers(tenant="tenantA", user="userA", roles="member"):
    return {
        "Authorization": f"Bearer {SECRET}",
        "X-Youtab-Tenant-Id": tenant,
        "X-Youtab-User-Id": user,
        "X-Youtab-Roles": roles,
        "X-Youtab-Correlation-Id": "cid-test",
    }


def _sign(method, path, tenant, user, body: bytes, nonce=None, correlation="cid-test"):
    ts = int(time.time())
    nonce = nonce or f"n-{uuid.uuid4().hex}"
    canonical = rca.canonical_string(
        method=method, path=path, tenant=tenant, user=user,
        timestamp=str(ts), nonce=nonce, body=body, correlation=correlation,
    )
    sig = rca.compute_signature(SECRET, canonical)
    return {
        rca.SIGNATURE_HEADER: sig,
        rca.TIMESTAMP_HEADER: str(ts),
        rca.NONCE_HEADER: nonce,
    }


def _create_run(client, *, task="add 2 and 2", engine=None, tenant="tenantA", user="userA"):
    payload = {"agent": "default", "task": task}
    if engine is not None:
        payload["engine"] = engine
    body = json.dumps(payload).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers(tenant, user)
    headers.update(_sign("POST", path, tenant, user, body))
    headers["Content-Type"] = "application/json"
    return client.post(path, content=body, headers=headers)


# --------------------------------------------------------------------------
# agent_identity.engine_binding_for_profile — inversion
# --------------------------------------------------------------------------


def test_engine_binding_inverts_correctly():
    assert agent_identity.engine_binding_for_profile("eco.v01") == ("ollama", "qwen3.5:9b")
    assert agent_identity.engine_binding_for_profile("amour.v03") == ("deepseek", "deepseek-v4-flash")
    assert agent_identity.engine_binding_for_profile("alpha.v06") == ("deepseek", "deepseek-v4-pro")
    assert agent_identity.engine_binding_for_profile("pirouz.v20") == ("moonshot", "kimi-k3")
    assert agent_identity.engine_binding_for_profile("homa") == ("zai", "glm-4.6v-flash")
    # Unknown / empty -> None (not an error).
    assert agent_identity.engine_binding_for_profile("nope.v9") is None
    assert agent_identity.engine_binding_for_profile("") is None
    assert agent_identity.engine_binding_for_profile(None) is None


# --------------------------------------------------------------------------
# GET /engines
# --------------------------------------------------------------------------


def test_engines_lists_branded_roster(client):
    r = client.get("/api/runtime/v1/engines", headers=_identity_headers())
    assert r.status_code == 200, r.text
    engines = r.json()["engines"]
    ids = {e["profile_id"] for e in engines}
    assert {"alpha.v06", "amour.v03", "eco.v01", "homa", "pirouz.v20"} <= ids
    homa = next(e for e in engines if e["profile_id"] == "homa")
    assert homa["public_label"] == "Homa"
    # vision role -> text + image; everyone else text only.
    assert homa["supported_modalities"] == ["text", "image"]
    alpha = next(e for e in engines if e["profile_id"] == "alpha.v06")
    assert alpha["supported_modalities"] == ["text"]


def test_engines_availability_unavailable_when_unconfigured(client):
    # Fixture default: nothing reachable, no credential installed.
    engines = client.get("/api/runtime/v1/engines", headers=_identity_headers()).json()["engines"]
    assert engines, "roster must not be empty"
    assert all(e["availability"] == "unavailable" for e in engines)


def test_engines_availability_online_only_with_real_check(client, monkeypatch):
    # An external engine goes online ONLY when a credential is present for its
    # provider; a local engine ONLY when its endpoint is reachable.
    monkeypatch.setattr(runtime, "_external_credential_present", lambda provider: provider == "deepseek")
    monkeypatch.setattr(runtime, "_connection_reachable", lambda conn: conn.provider == "ollama")
    runtime._engine_avail_cache.clear()

    engines = {e["profile_id"]: e for e in
               client.get("/api/runtime/v1/engines", headers=_identity_headers()).json()["engines"]}
    # deepseek-backed engines (alpha, amour) online; other externals not.
    assert engines["alpha.v06"]["availability"] == "online"
    assert engines["amour.v03"]["availability"] == "online"
    assert engines["homa"]["availability"] == "unavailable"       # zai, no credential
    assert engines["pirouz.v20"]["availability"] == "unavailable"  # moonshot, no credential
    # local ollama engine reachable -> online.
    assert engines["eco.v01"]["availability"] == "online"


def test_health_probes_the_canonical_remote_endpoint_not_only_loopback(client, monkeypatch):
    """Split-brain fix: a server-configured REMOTE ollama endpoint is probed by
    the availability check (the same endpoint execution would dial), instead of
    being dropped as non-loopback and read as permanently unavailable."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    monkeypatch.delenv("YOUTAB_ECO_MODEL", raising=False)
    probed: list[str] = []

    def _fake_reachable(url, *, timeout=1.0):
        probed.append(url)
        return True

    # The fixture patches _connection_reachable OFF (returns False); restore the
    # REAL probe path by composing the real endpoint-URL builder with the
    # recording _http_reachable, so this test exercises resolve_connection ->
    # _endpoint_probe_urls against the configured remote endpoint.
    monkeypatch.setattr(runtime, "_http_reachable", _fake_reachable)
    monkeypatch.setattr(
        runtime,
        "_connection_reachable",
        lambda conn: any(
            runtime._http_reachable(u)
            for u in runtime._endpoint_probe_urls(conn.provider, conn.endpoint)
        ),
    )
    runtime._engine_avail_cache.clear()

    engines = {e["profile_id"]: e for e in
               client.get("/api/runtime/v1/engines", headers=_identity_headers()).json()["engines"]}
    assert engines["eco.v01"]["availability"] == "online"
    assert any("100.108.46.86:11434" in u for u in probed), probed
    # The remote endpoint is never surfaced to the caller.
    raw = client.get("/api/runtime/v1/engines", headers=_identity_headers()).text
    assert "100.108.46.86" not in raw and "11434" not in raw


def test_availability_cache_ttl_is_bounded(client):
    """Unavailable health must not be cached indefinitely — the probe re-runs
    within a small window so a recovered ECO is picked up automatically."""
    assert 0 < runtime._ENGINE_AVAIL_TTL <= 60


def test_availability_recovers_and_restores_eco(client, monkeypatch):
    """ECO down -> unavailable; ECO recovers + cache expiry -> online again
    (so Auto's ECO-first routing is restored without a restart)."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.108.46.86:11434")
    state = {"up": False}
    monkeypatch.setattr(
        runtime,
        "_connection_reachable",
        lambda conn: state["up"] and conn.provider == "ollama",
    )

    def _avail():
        runtime._engine_avail_cache.clear()  # simulate TTL expiry
        engines = client.get("/api/runtime/v1/engines", headers=_identity_headers()).json()["engines"]
        return {e["profile_id"]: e["availability"] for e in engines}["eco.v01"]

    assert _avail() == "unavailable"
    state["up"] = True
    assert _avail() == "online"       # recovery restores ECO availability


def test_engines_never_leak_provider_model_or_secret(client, monkeypatch):
    monkeypatch.setattr(runtime, "_external_credential_present", lambda provider: True)
    monkeypatch.setattr(runtime, "_connection_reachable", lambda conn: True)
    runtime._engine_avail_cache.clear()
    raw = client.get("/api/runtime/v1/engines", headers=_identity_headers()).text.lower()
    for forbidden in (
        "provider", "deepseek", "ollama", "moonshot", "zai",
        "qwen", "kimi", "glm", "api_key", "base_url", "endpoint", "credential",
    ):
        assert forbidden not in raw, f"leaked {forbidden!r} in engines projection"


def test_engines_requires_service_identity(client):
    assert client.get("/api/runtime/v1/engines").status_code == 401
    assert client.get(
        "/api/runtime/v1/engines", headers={"Authorization": f"Bearer {SECRET}"}
    ).status_code == 403


# --------------------------------------------------------------------------
# create-run: engine selection
# --------------------------------------------------------------------------


def test_create_run_with_known_engine_records_selection_and_overrides(client, monkeypatch):
    captured = {}
    # The create handler now calls create_task_ex (returns (id, created)) so it
    # can append create-time events exactly-once on the NEW create only. Spy on
    # the function the handler actually invokes; assertions are unchanged.
    real_create = kb.create_task_ex

    def _spy(conn, **kwargs):
        captured.update(kwargs)
        return real_create(conn, **kwargs)

    monkeypatch.setattr(kb, "create_task_ex", _spy)

    r = _create_run(client, engine="eco.v01")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # bound engine drives the model/provider override into create_task
    assert captured.get("model_override") == "qwen3.5:9b"
    assert captured.get("provider_override") == "ollama"

    # the consumer-safe selection is recorded and surfaced in detail
    detail = client.get(f"/api/runtime/v1/runs/{run_id}", headers=_identity_headers()).json()
    assert detail["engine_selection"] == {"profile_id": "eco.v01", "public_label": "Eco v.01"}
    # detail must not leak the resolved provider/model behind the selection
    assert "qwen3.5:9b" not in json.dumps(detail["engine_selection"])


def test_create_run_unknown_engine_is_422(client):
    r = _create_run(client, engine="ghost.v99")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_engine"


def test_create_run_without_engine_is_unchanged(client, monkeypatch):
    captured = {}
    # The create handler now calls create_task_ex (returns (id, created)) so it
    # can append create-time events exactly-once on the NEW create only. Spy on
    # the function the handler actually invokes; assertions are unchanged.
    real_create = kb.create_task_ex

    def _spy(conn, **kwargs):
        captured.update(kwargs)
        return real_create(conn, **kwargs)

    monkeypatch.setattr(kb, "create_task_ex", _spy)

    r = _create_run(client)  # no engine
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert captured.get("model_override") is None
    assert captured.get("provider_override") is None
    detail = client.get(f"/api/runtime/v1/runs/{run_id}", headers=_identity_headers()).json()
    assert detail["engine_selection"] is None
