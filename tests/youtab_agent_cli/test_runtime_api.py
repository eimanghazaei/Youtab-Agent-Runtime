"""Integration tests for the /api/runtime/v1 product surface (AR-PROD-01, M1).

Proves, end-to-end and credential-free:

* the security boundary (service bearer via the runtime-service provider +
  gateway-forwarded end-user identity), fail-closed;
* a REAL isolated execution: create-run -> dispatcher claims -> a real worker
  SUBPROCESS runs -> the run reaches ``completed`` with a result, ordered
  events, and a downloadable artifact, read back through the router;
* cross-tenant / cross-user isolation (404, no existence leak);
* signed-command replay (409) and tamper (401) rejection.

The runtime itself is never mocked — only the model "brain" is replaced by a
deterministic local worker subprocess so no external API credentials are
needed (a strong-secret, no-network local proof, which is authorized).
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli import runtime_command_auth as rca
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.token_auth import token_auth_middleware
from youtab_agent_cli.web_routers import runtime

# Repo root (this file is tests/youtab_agent_cli/test_runtime_api.py).
REPO_ROOT = str(Path(__file__).resolve().parents[2])

# A strong secret that clears the runtime-service entropy gate.
SECRET = secrets.token_urlsafe(48)

# A real, deterministic worker subprocess: opens its OWN kanban connection,
# emits ordered events, writes a real artifact, and completes the task. This is
# a genuine external process — the same handoff shape as the real dispatcher's
# worker — with the LLM step replaced by a fixed result.
_WORKER_SRC = '''
import os, sys, time
from pathlib import Path
from youtab_agent_cli import kanban_db as kb

task_id = sys.argv[1]
db_path = Path(sys.argv[2])
time.sleep(0.15)
conn = kb.connect(db_path=db_path)
with kb.write_txn(conn):
    kb._append_event(conn, task_id, "worker_started", {"pid": os.getpid()})
    kb._append_event(conn, task_id, "worker_progress", {"step": "computing"})
kb.store_attachment_bytes(
    conn, task_id, "result.txt", b"real worker output: 2 + 2 = 4",
    content_type="text/plain",
)
kb.complete_task(
    conn, task_id, result="2 + 2 = 4",
    summary="completed by deterministic local worker",
)
conn.close()
'''


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
    # --- isolate all kanban state under tmp_path ---------------------------
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db_path))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)

    # --- register the service-auth provider + token prefix ----------------
    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider
    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix("/api/runtime/v1/")

    # --- one known agent --------------------------------------------------
    monkeypatch.setattr(
        "youtab_agent_cli.profiles.list_profiles", lambda: [_FakeProfile("default")]
    )

    # --- real worker subprocess as the spawn function ---------------------
    worker_py = tmp_path / "proof_worker.py"
    worker_py.write_text(_WORKER_SRC)

    def _spawn(task, workspace, *, board=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, str(worker_py), task.id, str(db_path)], env=env
        )
        return proc.pid

    runtime._spawn_override = _spawn
    runtime._nonce_store = None  # fresh per test

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)

    with TestClient(app) as c:
        yield c

    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None
    auth_registry.clear_providers()
    token_auth.clear_token_routes()


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


def _sign(method, path, tenant, user, body: bytes, nonce=None):
    ts = int(time.time())
    nonce = nonce or f"n-{uuid.uuid4().hex}"
    canonical = rca.canonical_string(
        method=method, path=path, tenant=tenant, user=user,
        timestamp=str(ts), nonce=nonce, body=body,
    )
    sig = rca.compute_signature(SECRET, canonical)
    return {
        rca.SIGNATURE_HEADER: sig,
        rca.TIMESTAMP_HEADER: str(ts),
        rca.NONCE_HEADER: nonce,
    }


def _create_run(client, tenant="tenantA", user="userA", task="add 2 and 2", nonce=None):
    import json
    body = json.dumps({"agent": "default", "task": task}).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers(tenant, user)
    headers.update(_sign("POST", path, tenant, user, body, nonce=nonce))
    headers["Content-Type"] = "application/json"
    return client.post(path, content=body, headers=headers)


def _wait_terminal(client, run_id, headers, timeout=25):
    deadline = time.time() + timeout
    cursor = 0
    seen_kinds = []
    while time.time() < deadline:
        r = client.get(
            f"/api/runtime/v1/runs/{run_id}/events?after={cursor}", headers=headers
        )
        assert r.status_code == 200, r.text
        data = r.json()
        for e in data["events"]:
            seen_kinds.append(e["kind"])
        cursor = data["cursor"]
        if data["terminal"]:
            return data, seen_kinds
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not reach terminal state; kinds={seen_kinds}")


# --------------------------------------------------------------------------
# security boundary
# --------------------------------------------------------------------------


def test_no_bearer_is_401(client):
    r = client.get("/api/runtime/v1/agents")
    assert r.status_code == 401


def test_wrong_bearer_is_401(client):
    r = client.get("/api/runtime/v1/agents", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_missing_identity_headers_is_403(client):
    # Valid service bearer but no tenant/user -> identity unverifiable.
    r = client.get("/api/runtime/v1/agents", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 403


def test_agents_listed_with_full_identity(client):
    r = client.get("/api/runtime/v1/agents", headers=_identity_headers())
    assert r.status_code == 200, r.text
    agents = r.json()["agents"]
    assert any(a["id"] == "default" for a in agents)


# --------------------------------------------------------------------------
# real isolated execution (the Milestone 1 exit condition)
# --------------------------------------------------------------------------


def test_capabilities_reports_honest_flags(client):
    caps = client.get("/api/runtime/v1/capabilities", headers=_identity_headers()).json()
    assert caps["contract_version"] == "1"
    # resume is NOT genuinely supported by the kanban engine -> must be honest
    assert caps["resume_supported"] is False
    assert caps["logs_supported"] is True
    assert "runs.logs" in caps["supported"]


def test_run_logs_endpoint_owned_and_scoped(client):
    run_id = _create_run(client).json()["run_id"]
    headers = _identity_headers()
    # own run: 200 with honest present/logs fields (may be empty early)
    r = client.get(f"/api/runtime/v1/runs/{run_id}/logs", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert "present" in body and "logs" in body
    # cross-tenant: 404
    other = _identity_headers(tenant="tenantZ", user="userA")
    assert client.get(f"/api/runtime/v1/runs/{run_id}/logs", headers=other).status_code == 404


def test_real_run_executes_and_returns_result_events_and_artifact(client):
    r = _create_run(client)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert run_id

    headers = _identity_headers()
    events, kinds = _wait_terminal(client, run_id, headers)
    assert events["status"] == "completed"
    assert "worker_started" in kinds

    # detail carries the real result
    detail = client.get(f"/api/runtime/v1/runs/{run_id}", headers=headers).json()
    assert detail["status"] == "completed"
    assert detail["result"] == "2 + 2 = 4"

    # a real artifact was produced and is listed
    arts = client.get(
        f"/api/runtime/v1/runs/{run_id}/artifacts", headers=headers
    ).json()["artifacts"]
    assert any(a["name"].startswith("result") for a in arts)
    assert arts[0]["size"] > 0

    # the run appears in the caller's own run list
    runs = client.get("/api/runtime/v1/runs", headers=headers).json()["runs"]
    assert any(x["run_id"] == run_id for x in runs)


# --------------------------------------------------------------------------
# tenant / user isolation
# --------------------------------------------------------------------------


def test_cross_tenant_read_is_404(client):
    run_id = _create_run(client, tenant="tenantA", user="userA").json()["run_id"]
    other = _identity_headers(tenant="tenantB", user="userA")
    assert client.get(f"/api/runtime/v1/runs/{run_id}", headers=other).status_code == 404
    assert client.get(
        f"/api/runtime/v1/runs/{run_id}/events", headers=other
    ).status_code == 404
    # and it never appears in tenantB's list
    runs = client.get("/api/runtime/v1/runs", headers=other).json()["runs"]
    assert all(x["run_id"] != run_id for x in runs)


def test_cross_user_same_tenant_read_is_404(client):
    run_id = _create_run(client, tenant="tenantA", user="userA").json()["run_id"]
    other_user = _identity_headers(tenant="tenantA", user="userB")
    assert client.get(
        f"/api/runtime/v1/runs/{run_id}", headers=other_user
    ).status_code == 404


# --------------------------------------------------------------------------
# signed-command replay / tamper
# --------------------------------------------------------------------------


def test_replayed_command_is_409(client):
    import json
    body = json.dumps({"agent": "default", "task": "x"}).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers()
    sig = _sign("POST", path, "tenantA", "userA", body, nonce="fixed-nonce")
    headers.update(sig)
    headers["Content-Type"] = "application/json"
    r1 = client.post(path, content=body, headers=headers)
    assert r1.status_code == 200, r1.text
    r2 = client.post(path, content=body, headers=headers)  # same nonce+sig
    assert r2.status_code == 409


def test_tampered_command_is_401(client):
    import json
    signed_body = json.dumps({"agent": "default", "task": "original"}).encode()
    tampered_body = json.dumps({"agent": "default", "task": "TAMPERED"}).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers()
    headers.update(_sign("POST", path, "tenantA", "userA", signed_body))
    headers["Content-Type"] = "application/json"
    r = client.post(path, content=tampered_body, headers=headers)
    assert r.status_code == 401


def test_unsigned_mutation_is_401(client):
    import json
    body = json.dumps({"agent": "default", "task": "x"}).encode()
    headers = _identity_headers()
    headers["Content-Type"] = "application/json"
    r = client.post("/api/runtime/v1/runs", content=body, headers=headers)
    assert r.status_code == 401
