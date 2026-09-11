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
    token_auth.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service", capability="runtime")

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

    # Real startup ordering (WAVE-22): the seam serves only from a VERIFIED
    # generation. This bare test app has no lifespan to freeze/verify the
    # registry, so drive the isolated registry through the real transition —
    # declare ownership, freeze, verify → VERIFIED — exactly as the dashboard
    # lifespan would before accepting traffic.
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


def _create_run(client, tenant="tenantA", user="userA", task="add 2 and 2", nonce=None):
    import json
    body = json.dumps({"agent": "default", "task": task}).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers(tenant, user)
    headers.update(_sign("POST", path, tenant, user, body, nonce=nonce))
    headers["Content-Type"] = "application/json"
    return client.post(path, content=body, headers=headers)


def _create_run_body(client, body_obj, tenant="tenantA", user="userA", nonce=None):
    import json

    body = json.dumps(body_obj).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers(tenant, user)
    headers.update(_sign("POST", path, tenant, user, body, nonce=nonce))
    headers["Content-Type"] = "application/json"
    return client.post(path, content=body, headers=headers)


def test_create_run_persists_clamped_limits_event(client):
    """WAVE-30B §8: create-run records an authoritative, server-clamped
    runtime_limits event. Over-ceiling values are clamped down."""
    r = _create_run_body(
        client,
        {
            "agent": "default",
            "task": "bounded run",
            "limits": {
                "max_iterations": 9999,   # clamped to RUN_CEILINGS
                "max_requests": 40,
                "max_total_tokens": 250000,
                "max_cost_eur": "2.00",
            },
        },
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    headers = _identity_headers()
    ev = client.get(
        f"/api/runtime/v1/runs/{run_id}/events?after=0", headers=headers
    ).json()["events"]
    limits_events = [e for e in ev if e["kind"] == "runtime_limits"]
    assert len(limits_events) == 1, ev
    payload = limits_events[0]["payload"]
    from youtab_runtime.run_limits import RUN_CEILINGS

    assert payload["max_iterations"] == RUN_CEILINGS["max_iterations"]
    assert payload["max_requests"] == 40
    assert payload["max_total_tokens"] == 250000
    assert payload["max_cost_eur"] == "2.00"


def test_create_run_rejects_invalid_limits(client):
    r = _create_run_body(
        client,
        {"agent": "default", "task": "bad", "limits": {"max_iterations": -5}},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "invalid_limits"


def _task_after_create(run_id):
    """Read the persisted runtime task row for a created run."""
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        return kb.get_task(conn, run_id)


def _limits_event_payload(client, run_id):
    ev = client.get(
        f"/api/runtime/v1/runs/{run_id}/events?after=0", headers=_identity_headers()
    ).json()["events"]
    evs = [e for e in ev if e["kind"] == "runtime_limits"]
    assert len(evs) == 1, ev
    return evs[0]["payload"]


def test_create_run_canary_retries_bound_worker_attempts_to_one(client):
    """WAVE-30D dispatcher-attempt fix: the canary's ``max_retries=0`` (no retry)
    must persist ``tasks.max_retries=1`` so the dispatcher circuit breaker trips
    on the FIRST worker failure — one attempt, no silent respawn (which would let
    a second worker issue a second model request even under ``max_requests=1``)."""
    r = _create_run_body(
        client,
        {
            "agent": "default",
            "task": "canary",
            "limits": {
                "max_iterations": 1,
                "max_requests": 1,
                "max_retries": 0,
                "max_total_tokens": 4608,
                "max_runtime_seconds": 60,
                "failure_threshold": 1,
            },
        },
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    task = _task_after_create(run_id)
    # 1 + max_retries(0) = 1 total worker attempt (breaker trips on first failure).
    assert task.max_retries == 1

    payload = _limits_event_payload(client, run_id)
    # The run limit is recorded honestly as zero retries...
    assert payload["max_retries"] == 0
    # ...and the derived dispatcher respawn ceiling is exposed explicitly rather
    # than left implicit while the dispatcher silently respawns.
    assert payload["worker_attempt_limit"] == 1


def test_create_run_pilot_retries_allow_one_respawn(client):
    """A run that declares one retry (``max_retries=1``, pilot/full) maps to two
    worker attempts — identical to the prior dispatcher default, so only the
    zero-retry canary changes behaviour."""
    r = _create_run_body(
        client,
        {
            "agent": "default",
            "task": "pilot",
            "limits": {"max_iterations": 4, "max_requests": 40, "max_retries": 1},
        },
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    task = _task_after_create(run_id)
    assert task.max_retries == 2  # 1 + 1
    assert _limits_event_payload(client, run_id)["worker_attempt_limit"] == 2


def test_create_run_retry_budget_is_authoritative_and_ceiling_bounded(client):
    """A run that explicitly requests >=2 retries authoritatively raises its own
    worker-attempt budget ABOVE the dispatcher default (2) — that is intended
    (the run owns its budget) — but never unbounded: max_retries is clamped to
    RUN_CEILINGS (3), so worker attempts are capped at 4."""
    from youtab_runtime.run_limits import RUN_CEILINGS

    r = _create_run_body(
        client,
        {"agent": "default", "task": "explicit-budget",
         "limits": {"max_requests": 40, "max_retries": 99}},  # clamped to 3
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    task = _task_after_create(run_id)
    assert task.max_retries == RUN_CEILINGS["max_retries"] + 1 == 4
    assert _limits_event_payload(client, run_id)["worker_attempt_limit"] == 4


def test_create_run_without_retry_budget_leaves_dispatcher_default(client):
    """Ordinary runs (no retry budget in the request) must NOT pin the per-task
    breaker — ``tasks.max_retries`` stays NULL so the dispatcher's own default /
    config failure limit is preserved (Track B + normal runtime recovery)."""
    r = _create_run_body(
        client,
        {"agent": "default", "task": "ordinary", "limits": {"max_requests": 40}},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    task = _task_after_create(run_id)
    assert task.max_retries is None
    payload = _limits_event_payload(client, run_id)
    assert "worker_attempt_limit" not in payload


def test_preflight_reports_safe_posture_and_leaks_no_secret(client):
    """WAVE-30B §12: authenticated preflight exposes the safety posture (SHA,
    version, redaction, budget, ceilings) and never a secret."""
    r = client.get("/api/runtime/v1/preflight", headers=_identity_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "ok", "service_ready", "engine_version", "contract_version",
        "redaction_enabled", "budget_enforcement_enabled", "live_benchmark_mode",
        "hard_campaign_ceiling_eur", "run_limit_ceilings", "audit_available",
        "no_production_dataset", "provider_credential_source",
    ):
        assert key in body, f"missing {key}: {body}"
    assert body["redaction_enabled"] is True
    # Honest attestation: no campaign configured in this test env => not armed.
    assert body["budget_enforcement_enabled"] is False
    assert body["hard_campaign_ceiling_eur"] == "10.00"
    assert body["run_limit_ceilings"]["max_iterations"] >= 1
    # the service secret must never appear anywhere in the response
    assert SECRET not in r.text


def test_preflight_requires_auth(client):
    assert client.get("/api/runtime/v1/preflight").status_code == 401


def test_preflight_budget_armed_when_campaign_configured(client, tmp_path, monkeypatch):
    """budget_enforcement_enabled reflects REAL state (WAVE-30D M1): armed only
    when the campaign is actually OPEN in the durable ledger — not merely when a
    campaign id is configured (a stale/never-opened id would fail closed at
    reserve, so it must not read green here)."""
    from youtab_runtime import campaign_budget as cb

    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("YOUTAB_AGENT_BENCHMARK_CAMPAIGN_ID", "campaign-xyz")
    cb.open_campaign(
        "campaign-xyz", ceiling_eur="10.00", fx_usd_to_eur="0.92",
        fx_source="test-fixture", fx_asof="2026-09-06",
    )
    body = client.get("/api/runtime/v1/preflight", headers=_identity_headers()).json()
    assert body["budget_enforcement_enabled"] is True
    assert body["budget_enforcement_source"] == "campaign_ledger"
    assert body["campaign_id"] == "campaign-xyz"
    assert body["remaining_eur"] is not None


def test_preflight_configured_but_unopened_campaign_is_not_armed(client, tmp_path, monkeypatch):
    """A configured-but-never-opened campaign must NOT read armed (WAVE-30D M1)."""
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("YOUTAB_AGENT_BENCHMARK_CAMPAIGN_ID", "never-opened")
    body = client.get("/api/runtime/v1/preflight", headers=_identity_headers()).json()
    assert body["remaining_eur"] is None
    assert body["budget_enforcement_enabled"] is False
    assert body["budget_enforcement_source"] == "none"


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

    # and its bytes are downloadable by the owner, 404 cross-tenant
    aid = arts[0]["id"]
    dl = client.get(f"/api/runtime/v1/runs/{run_id}/artifacts/{aid}", headers=headers)
    assert dl.status_code == 200
    assert b"real worker output" in dl.content
    other = _identity_headers(tenant="tenantQ", user="userA")
    assert client.get(
        f"/api/runtime/v1/runs/{run_id}/artifacts/{aid}", headers=other
    ).status_code == 404

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
# correlation binding (CORRELATION_CONTRACT v1)
# --------------------------------------------------------------------------


def _identity_headers_corr(correlation, tenant="tenantA", user="userA"):
    h = _identity_headers(tenant, user)
    h["X-Youtab-Correlation-Id"] = correlation
    return h


def test_create_stamps_correlation_on_task_event_and_dto(client):
    r = _create_run(client)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    headers = _identity_headers()

    # DTO surfaces the correlation the caller sent ("cid-test").
    detail = client.get(f"/api/runtime/v1/runs/{run_id}", headers=headers).json()
    assert detail["correlation_id"] == "cid-test"

    # Persisted on the dedicated column (legacy session_id overload preserved).
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        task = kb.get_task(conn, run_id)
        assert task.correlation_id == "cid-test"
        assert task.session_id == "cid-test"

    # Authoritative dispatch (mode) event carries the correlation.
    events = client.get(
        f"/api/runtime/v1/runs/{run_id}/events", headers=headers
    ).json()["events"]
    mode_evt = next(e for e in events if e["kind"] == "runtime_execution_mode")
    assert mode_evt["payload"]["correlation_id"] == "cid-test"
    assert mode_evt["correlation_id"] == "cid-test"


def test_missing_correlation_on_signed_create_is_401(client):
    import json
    body = json.dumps({"agent": "default", "task": "x"}).encode()
    path = "/api/runtime/v1/runs"
    # Sign WITHOUT a correlation and omit the header entirely.
    headers = _identity_headers()
    headers.pop("X-Youtab-Correlation-Id", None)
    headers.update(_sign("POST", path, "tenantA", "userA", body, correlation=""))
    headers["Content-Type"] = "application/json"
    r = client.post(path, content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "missing_correlation"


def test_malformed_correlation_on_signed_create_is_400(client):
    import json
    body = json.dumps({"agent": "default", "task": "x"}).encode()
    path = "/api/runtime/v1/runs"
    bad = "bad id!"
    headers = _identity_headers_corr(bad)
    headers.update(_sign("POST", path, "tenantA", "userA", body, correlation=bad))
    headers["Content-Type"] = "application/json"
    r = client.post(path, content=body, headers=headers)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_correlation"


def test_tampered_correlation_header_is_bad_signature(client):
    import json
    body = json.dumps({"agent": "default", "task": "x"}).encode()
    path = "/api/runtime/v1/runs"
    # Sign for "cid-test" but present a DIFFERENT valid correlation on the wire.
    headers = _identity_headers_corr("cid-tampered-000000")
    headers.update(_sign("POST", path, "tenantA", "userA", body, correlation="cid-test"))
    headers["Content-Type"] = "application/json"
    r = client.post(path, content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "bad_signature"


def test_retry_preserves_correlation_lineage(client):
    # Original run signed with correlation "cid-test".
    run_id = _create_run(client).json()["run_id"]
    _wait_terminal(client, run_id, _identity_headers())

    # Retry with a DIFFERENT inbound correlation; lineage must inherit the
    # ORIGINAL's correlation, not the retry request's.
    path = f"/api/runtime/v1/runs/{run_id}/retry"
    retry_corr = "cid-retry-different-01"
    headers = _identity_headers_corr(retry_corr)
    headers.update(_sign("POST", path, "tenantA", "userA", b"", correlation=retry_corr))
    r = client.post(path, headers=headers)
    assert r.status_code == 200, r.text
    new_id = r.json()["run_id"]
    assert new_id != run_id

    # New task inherits the original correlation engine-side.
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        new_task = kb.get_task(conn, new_id)
        assert new_task.correlation_id == "cid-test"

    # An authoritative retried_from event records original run + correlation.
    events = client.get(
        f"/api/runtime/v1/runs/{new_id}/events", headers=_identity_headers()
    ).json()["events"]
    lineage = next(e for e in events if e["kind"] == "runtime_retried_from")
    assert lineage["payload"]["original_run_id"] == run_id
    assert lineage["payload"]["correlation_id"] == "cid-test"


# --------------------------------------------------------------------------
# idempotent create — create-time events must be exactly-once
# --------------------------------------------------------------------------


def _create_run_idem(
    client,
    idem_key,
    *,
    nonce,
    engine=None,
    tenant="tenantA",
    user="userA",
    correlation="cid-test",
):
    """POST a signed create carrying an ``Idempotency-Key`` header.

    Each call uses a DISTINCT signed-command nonce (so it is not rejected as a
    replay) but the SAME idempotency key (so create_task returns the existing
    run on the retry). ``Idempotency-Key`` is a header, not part of the signed
    canonical string, so reusing it across two independently-signed commands is
    legitimate.
    """
    import json
    payload = {"agent": "default", "task": "idempotent create"}
    if engine is not None:
        payload["engine"] = engine
    body = json.dumps(payload).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers_corr(correlation, tenant, user)
    headers.update(
        _sign("POST", path, tenant, user, body, nonce=nonce, correlation=correlation)
    )
    headers["Content-Type"] = "application/json"
    headers["Idempotency-Key"] = idem_key
    return client.post(path, content=body, headers=headers)


def _event_kind_counts(client, run_id):
    events = client.get(
        f"/api/runtime/v1/runs/{run_id}/events", headers=_identity_headers()
    ).json()["events"]
    mode = [e for e in events if e["kind"] == runtime._MODE_EVENT]
    engine = [e for e in events if e["kind"] == runtime._ENGINE_EVENT]
    return len(mode), len(engine)


def test_idempotent_retry_does_not_duplicate_mode_and_engine_events(client, monkeypatch):
    # A real, resolvable engine, forced UNBOUND so the run records the engine
    # selection event without triggering the local-server split-brain guard.
    engine_id = "alpha.v06"
    monkeypatch.setattr(
        runtime.agent_identity, "engine_binding_for_profile", lambda pid: None
    )

    idem = "idem-key-fixed-001"
    r1 = _create_run_idem(client, idem, nonce="n-idem-1", engine=engine_id)
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["run_id"]

    # First (new) create records EXACTLY ONE of each create-time event.
    mode_n, engine_n = _event_kind_counts(client, run_id)
    assert mode_n == 1, f"new create must record one mode event, got {mode_n}"
    assert engine_n == 1, f"new create must record one engine event, got {engine_n}"

    # Idempotent retry: same key, fresh nonce. Returns the SAME run (semantics
    # preserved) and MUST NOT append a second mode/engine event.
    r2 = _create_run_idem(client, idem, nonce="n-idem-2", engine=engine_id)
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == run_id, "idempotent retry must return the same run"

    mode_n2, engine_n2 = _event_kind_counts(client, run_id)
    assert mode_n2 == 1, f"idempotent retry duplicated the mode event: got {mode_n2}"
    assert engine_n2 == 1, f"idempotent retry duplicated the engine event: got {engine_n2}"


def test_normal_new_create_records_one_mode_event(client):
    # A plain create (no engine) still records exactly one mode event and, with
    # no engine selected, zero engine events.
    run_id = _create_run(client).json()["run_id"]
    mode_n, engine_n = _event_kind_counts(client, run_id)
    assert mode_n == 1
    assert engine_n == 0


def test_idempotency_key_does_not_cross_tenant_boundary(client):
    """A colliding Idempotency-Key from a DIFFERENT tenant must NOT return the
    first tenant's run (no disclosure, no suppression).

    Idempotency-Key is caller-controlled; the dedupe lookup is scoped to the
    same (tenant, user) that owns the run — the boundary _load_owned_task
    enforces on every read. So tenant B reusing tenant A's key gets its OWN new
    run, and B never sees A's tenant_id/correlation_id.
    """
    import json

    key = "shared-idem-key-cross-tenant"

    # Tenant A creates run RA with a secret correlation.
    ra = _create_run_idem(
        client, key, nonce="n-a", tenant="tenantA", user="userA",
        correlation="cid-a-secret",
    )
    assert ra.status_code == 200, ra.text
    run_a = ra.json()["run_id"]

    # Tenant B submits the SAME key -> must get a DISTINCT, newly-created run.
    rb = _create_run_idem(
        client, key, nonce="n-b", tenant="tenantB", user="userB",
        correlation="cid-b-own",
    )
    assert rb.status_code == 200, rb.text
    run_b = rb.json()["run_id"]
    assert run_b != run_a, "cross-tenant key collision leaked/suppressed a run"

    # B's own run is B's: its detail carries B's tenant + correlation, never A's.
    b_headers = _identity_headers_corr("cid-b-own", tenant="tenantB", user="userB")
    detail_b = client.get(
        f"/api/runtime/v1/runs/{run_b}", headers=b_headers
    ).json()
    assert detail_b["tenant_id"] == "tenantB"
    assert detail_b["correlation_id"] == "cid-b-own"
    serialized_b = json.dumps(detail_b)
    assert "tenantA" not in serialized_b
    assert "cid-a-secret" not in serialized_b

    # And B cannot read A's run at all (existence never leaks cross-tenant).
    assert client.get(
        f"/api/runtime/v1/runs/{run_a}", headers=b_headers
    ).status_code == 404


def test_idempotency_key_does_not_cross_user_within_tenant(client):
    """Within one tenant, a colliding key from a DIFFERENT user is also a new
    run — the ownership boundary is (tenant, user), matching _load_owned_task."""
    key = "shared-idem-key-cross-user"

    ra = _create_run_idem(client, key, nonce="n-u1", tenant="tenantA", user="userA")
    assert ra.status_code == 200, ra.text
    run_a = ra.json()["run_id"]

    rb = _create_run_idem(client, key, nonce="n-u2", tenant="tenantA", user="userB")
    assert rb.status_code == 200, rb.text
    run_b = rb.json()["run_id"]
    assert run_b != run_a, "same-tenant different-user key collision was not isolated"

    # Same tenant + SAME user + same key still dedupes to the one run.
    ra2 = _create_run_idem(client, key, nonce="n-u1b", tenant="tenantA", user="userA")
    assert ra2.status_code == 200, ra2.text
    assert ra2.json()["run_id"] == run_a


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


# --------------------------------------------------------------------------
# R8 (WAVE-30H): the admission HMAC-verify stage emits a causally-valid span
# --------------------------------------------------------------------------
def test_admission_verify_emits_stage_span(client):
    """The signed-command verification on a real create-run is measured as an
    ``admission.verify_signature`` span, carrying the request correlation id and
    NO secret/body content."""
    from youtab_runtime import stage_trace as st

    sink = st.MemorySink()
    st.set_sink(sink)
    try:
        r = _create_run(client)
        assert r.status_code in (200, 201, 202)
    finally:
        st.set_sink(None)

    spans = [s for s in sink.records if s["stage"] == st.Stage.ADMISSION_VERIFY]
    assert spans, "expected an admission.verify_signature span"
    sp = spans[0]
    assert sp["ok"] is True
    assert sp["clock"] == "monotonic"
    assert sp["duration_ns"] is not None and sp["duration_ns"] >= 0
    assert sp["ctx"].get("correlation_id") == "cid-test"
    # no body/secret ever rides in a span (attrs is empty or safe-only)
    assert all(k in st.SAFE_STR_KEYS or not isinstance(v, str)
               for k, v in (sp.get("attrs") or {}).items())


def test_admission_verify_span_records_failure_on_tampered_body(client):
    """A tampered (401) request still emits a span, marked ok=False — a failed
    stage is data, not hidden."""
    import json

    from youtab_runtime import stage_trace as st

    sink = st.MemorySink()
    st.set_sink(sink)
    try:
        signed_body = json.dumps({"agent": "default", "task": "original"}).encode()
        tampered_body = json.dumps({"agent": "default", "task": "TAMPERED"}).encode()
        path = "/api/runtime/v1/runs"
        headers = _identity_headers()
        headers.update(_sign("POST", path, "tenantA", "userA", signed_body))
        headers["Content-Type"] = "application/json"
        r = client.post(path, content=tampered_body, headers=headers)
        assert r.status_code == 401
    finally:
        st.set_sink(None)

    spans = [s for s in sink.records if s["stage"] == st.Stage.ADMISSION_VERIFY]
    assert spans and spans[0]["ok"] is False
    assert "reason_code" in (spans[0].get("attrs") or {})


# --------------------------------------------------------------------------
# WAVE-30H — a retry inherits the ORIGINAL run's COMPLETE execution binding
# (Greptile blocker #2). Row-level provider/model pin + engine selection +
# cost policy are reproduced; a corrupt binding or a non-atomic persist fails
# closed — a retry is NEVER silently downgraded to a default model.
# --------------------------------------------------------------------------


def test_create_run_validates_expected_binding_digest(client, monkeypatch):
    # WAVE-30H: atomic preflight->create. A matching substrate digest creates the
    # run; a mismatch fails closed (412, no enqueue); a malformed digest is 422.
    from youtab_agent_cli import effective_binding as eb
    from youtab_agent_cli.web_routers import runtime as R

    monkeypatch.setattr(
        R, "_profile_default_identity",
        lambda agent: ("openai", "gpt-x", "https://api.openai.com"),
    )
    good = eb.binding_digest(eb.build_effective_binding(
        provider="openai", model="gpt-x", endpoint="https://api.openai.com"))

    r = _create_run_body(
        client, {"agent": "default", "task": "t", "expected_binding_digest": good},
        nonce="digest-ok-00000000")
    assert r.status_code in (200, 201), r.text

    r = _create_run_body(
        client, {"agent": "default", "task": "t", "expected_binding_digest": "a" * 64},
        nonce="digest-mismatch-000")
    assert r.status_code == 412, r.text
    assert r.json()["detail"]["error"] == "binding_digest_mismatch"

    r = _create_run_body(
        client, {"agent": "default", "task": "t", "expected_binding_digest": "not-hex"},
        nonce="digest-malformed-0")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "malformed_binding_digest"


def test_engine_bound_preflight_binding_carries_endpoint_fingerprint(client, monkeypatch):
    # WAVE-30H #1: the engine-bound preflight binding must carry endpoint_fingerprint
    # so its binding_digest equals what create derives for the SAME engine (else the
    # run self-rejects 412). resolve_connection is the shared source for BOTH preflight
    # (_engine_attestation) and create, so a matching endpoint => matching digest.
    import types

    from youtab_agent_cli import effective_binding as eb
    from youtab_agent_cli.web_routers import runtime as R

    conn = types.SimpleNamespace(
        provider="ollama", model="qwen:test", model_ref="ollama/qwen:test",
        endpoint="http://127.0.0.1:11434", is_local_server=lambda: True,
    )
    monkeypatch.setattr(R.engine_connection, "resolve_connection", lambda e: conn)
    monkeypatch.setattr(R.engine_connection, "endpoint_is_authorized", lambda ep: True)
    monkeypatch.setattr(R, "_ollama_model_digest", lambda ep, m: (None, "not_applicable"))

    # A non-ECO resolvable engine so the ECO placeholder-model branch does not fire.
    resp = client.get("/api/runtime/v1/preflight?engine=alpha.v06", headers=_identity_headers())
    assert resp.status_code == 200, resp.text
    pf = resp.json()["effective_binding"]
    assert pf.get("endpoint_fingerprint"), "preflight binding must carry endpoint_fingerprint"
    # create computes the substrate binding from the SAME resolved connection.
    create_binding = eb.build_effective_binding(
        provider="ollama", model="qwen:test", endpoint="http://127.0.0.1:11434")
    assert eb.binding_digest(pf) == eb.binding_digest(create_binding)


def test_create_run_validates_expected_model_digest(client, monkeypatch):
    # WAVE-30H #7: a valid attested digest on a probe-eligible ollama-local substrate
    # is pinned (digest_status="attested"); malformed is 422; a cloud substrate is
    # 422 model_digest_not_applicable.
    from youtab_agent_cli.web_routers import runtime as R

    monkeypatch.setattr(
        R, "_profile_default_identity",
        lambda agent: ("ollama", "qwen:test", "http://127.0.0.1:11434"),
    )
    r = _create_run_body(
        client, {"agent": "default", "task": "t", "expected_model_digest": "a" * 64},
        nonce="mdigest-ok-000000")
    assert r.status_code in (200, 201), r.text
    rid = r.json()["run_id"]
    events = client.get(
        f"/api/runtime/v1/runs/{rid}/events", headers=_identity_headers()
    ).json()["events"]
    binding = next(e["payload"] for e in events if e["kind"] == runtime.eb.BINDING_EVENT)
    assert binding["digest_status"] == "attested"
    assert binding["model_digest"] == "a" * 64
    assert runtime.eb.verify_binding(binding)

    r = _create_run_body(
        client, {"agent": "default", "task": "t", "expected_model_digest": "nothex"},
        nonce="mdigest-bad-00000")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "malformed_model_digest"

    # Cloud substrate -> not probe-eligible -> pinning a digest is refused.
    monkeypatch.setattr(
        R, "_profile_default_identity",
        lambda agent: ("openai", "gpt-x", "https://api.openai.com"),
    )
    r = _create_run_body(
        client, {"agent": "default", "task": "t", "expected_model_digest": "a" * 64},
        nonce="mdigest-cloud-000")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "model_digest_not_applicable"


def test_idempotent_replay_binding_digest_mismatch_fails_closed(client, monkeypatch):
    # WAVE-30H #8: a replayed Idempotency-Key that pins a DIFFERENT expected digest
    # than the existing run's persisted binding must 412 — never silently return a run
    # bound to a different substrate. A matching replay returns the same run (200).
    import json

    from youtab_agent_cli import effective_binding as eb
    from youtab_agent_cli.web_routers import runtime as R

    monkeypatch.setattr(
        R, "_profile_default_identity",
        lambda agent: ("openai", "gpt-x", "https://api.openai.com"),
    )
    d_match = eb.binding_digest(eb.build_effective_binding(
        provider="openai", model="gpt-x", endpoint="https://api.openai.com"))

    def _create(digest, nonce):
        body = json.dumps({"agent": "default", "task": "t",
                           "expected_binding_digest": digest}).encode()
        path = "/api/runtime/v1/runs"
        headers = _identity_headers()
        headers.update(_sign("POST", path, "tenantA", "userA", body, nonce=nonce))
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = "idem-digest-8"
        return client.post(path, content=body, headers=headers)

    r1 = _create(d_match, "idem8-a")
    assert r1.status_code in (200, 201), r1.text
    rid = r1.json()["run_id"]
    # Matching replay -> same run.
    r2 = _create(d_match, "idem8-b")
    assert r2.status_code == 200 and r2.json()["run_id"] == rid, r2.text
    # Mismatching replay -> fail closed (the run is NOT re-bound / re-returned clean).
    r3 = _create("a" * 64, "idem8-c")
    assert r3.status_code == 412, r3.text
    assert r3.json()["detail"]["error"] == "binding_digest_mismatch"


def test_retry_refuses_tampered_parent_binding(client):
    # WAVE-30H #4: a tampered-at-rest parent binding must NOT be laundered into a
    # fresh valid child binding on retry — fail closed, create no child.
    oid = _seed_bound_original()
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        events = kb.list_events(conn, oid)
        parent = next(e.payload for e in events if e.kind == runtime.eb.BINDING_EVENT)
        parent = dict(parent)
        parent["model"] = "ollama/EVIL"  # tamper substrate, keep the stale hash
        parent["binding_version"] = 2    # higher version -> this is now the CURRENT binding
        assert not runtime.eb.verify_binding(parent)
        with kb.write_txn(conn):
            kb._append_event(conn, oid, runtime.eb.BINDING_EVENT, parent)
    before = _count_retry_children()
    r = _post_retry(client, oid)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "retry_binding_invalid"
    assert _count_retry_children() == before  # no laundered child created


def _seed_bound_original(*, model="ollama/qwen-test", provider="ollama",
                         profile_id="eco.v01", limits=None, engine_payload=None):
    """Create an original run owned by (tenantA, userA) carrying a full binding.

    Seeded directly through kanban so the test does not depend on a live
    engine-model configuration; the retry endpoint reads the same durable row +
    events regardless of how they were recorded.
    """
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        oid = kb.create_task(
            conn, title="bound original", body="do work",
            assignee="default", created_by="userA", tenant="tenantA",
            model_override=model, provider_override=provider,
            board=runtime.RUNTIME_BOARD, correlation_id="cid-test",
        )
        with kb.write_txn(conn):
            kb._append_event(conn, oid, runtime._MODE_EVENT,
                             {"mode": "model", "correlation_id": "cid-test"})
            if engine_payload is not None:
                kb._append_event(conn, oid, runtime._ENGINE_EVENT, engine_payload)
            elif profile_id is not None:
                kb._append_event(conn, oid, runtime._ENGINE_EVENT,
                                 {"profile_id": profile_id, "public_label": "ECO"})
            if limits is not None:
                kb._append_event(conn, oid, runtime._LIMITS_EVENT, limits)
            # WAVE-30H: an attested run carries a canonical effective binding (real
            # create_run always persists one); seed it so retry inherits it.
            kb._append_event(
                conn, oid, runtime.eb.BINDING_EVENT,
                runtime.eb.build_effective_binding(
                    provider=provider, model=model,
                    endpoint="http://127.0.0.1:11434",
                    run_id=oid, root_run_id=oid, tenant="tenantA",
                ),
            )
    return oid


def _post_retry(client, run_id, correlation="cid-retry-x"):
    path = f"/api/runtime/v1/runs/{run_id}/retry"
    headers = _identity_headers_corr(correlation)
    headers.update(_sign("POST", path, "tenantA", "userA", b"", correlation=correlation))
    return client.post(path, headers=headers)


def test_retry_inherits_full_execution_binding(client):
    oid = _seed_bound_original(
        limits={"max_cost_eur": 3, "worker_attempt_limit": 1})
    r = _post_retry(client, oid)
    assert r.status_code == 200, r.text
    new_id = r.json()["run_id"]
    assert new_id != oid

    # Row-level provider/model pin inherited (the anti-default guarantee).
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        child = kb.get_task(conn, new_id)
        assert child.model_override == "ollama/qwen-test"
        assert child.provider_override == "ollama"

    # Engine selection + cost policy re-recorded on the child's own stream.
    events = client.get(
        f"/api/runtime/v1/runs/{new_id}/events", headers=_identity_headers()
    ).json()["events"]
    eng = next(e for e in events if e["kind"] == "runtime_engine_selection")
    assert eng["payload"]["profile_id"] == "eco.v01"
    lim = next(e for e in events if e["kind"] == "runtime_limits")
    assert lim["payload"]["max_cost_eur"] == 3

    # WAVE-30H run-scope: the child binding is RE-SCOPED to the child's own run —
    # same SUBSTRATE (binding_digest invariant, proving no drift) but run_id ==
    # new_id (so the worker run-scope gate admits it, not rejects it as cross-run),
    # root_run_id == the original (lineage preserved), and it self-verifies.
    child_binding = next(
        e["payload"] for e in events if e["kind"] == runtime.eb.BINDING_EVENT
    )
    parent_binding = runtime.eb.build_effective_binding(
        provider="ollama", model="ollama/qwen-test",
        endpoint="http://127.0.0.1:11434", run_id=oid, root_run_id=oid,
        tenant="tenantA",
    )
    assert child_binding["run_id"] == new_id
    assert child_binding["run_id"] != oid
    assert child_binding["root_run_id"] == oid
    assert runtime.eb.verify_binding(child_binding)
    assert runtime.eb.binding_digest(child_binding) == runtime.eb.binding_digest(
        parent_binding
    )


def test_retry_of_unbound_original_inherits_no_pin(client):
    # A legitimately unbound original (no engine, no override) is reproduced
    # faithfully — the child carries no override, which is the ORIGINAL binding,
    # not a new default substitution.
    oid = _seed_bound_original(model=None, provider=None, profile_id=None)
    r = _post_retry(client, oid)
    assert r.status_code == 200, r.text
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        child = kb.get_task(conn, r.json()["run_id"])
        assert child.model_override is None
        assert child.provider_override is None


def test_retry_fails_closed_on_corrupt_engine_binding(client):
    # A recorded engine selection whose profile_id is empty is a corrupt binding:
    # refuse (422), and create NO child.
    oid = _seed_bound_original(engine_payload={"profile_id": "", "public_label": "ECO"})
    before = _count_retry_children()
    r = _post_retry(client, oid)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "retry_binding_invalid"
    assert _count_retry_children() == before  # no child leaked


def _count_retry_children():
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE title LIKE '%(retry)' AND status != 'archived'"
        ).fetchone()
        return rows["n"]


def test_retry_fails_closed_when_binding_cannot_be_persisted(client, monkeypatch):
    # If persisting the engine-selection event fails, the child must be ARCHIVED
    # (never dispatched on a partial binding) and the caller gets a 500 — never a
    # child that would run on a default model.
    oid = _seed_bound_original()
    real_append = kb._append_event

    def boom(conn, task_id, kind, payload, **kw):
        if kind == runtime._ENGINE_EVENT:
            raise RuntimeError("simulated binding-persist failure")
        return real_append(conn, task_id, kind, payload, **kw)

    monkeypatch.setattr(kb, "_append_event", boom)
    r = _post_retry(client, oid)
    assert r.status_code == 500, r.text
    assert r.json()["detail"]["error"] == "retry_binding_not_persisted"

    # No dispatchable retry child survives; any created child is archived.
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        leaked = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE title LIKE '%(retry)' AND status != 'archived'"
        ).fetchone()["n"]
    assert leaked == 0
