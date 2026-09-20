"""CXR-02 (Simorgh execution-grant enforcement) + CXR-04 (atomic nonce replay)
at the REAL ``POST /api/runtime/v1/runs`` HTTP ingress boundary.

CXR-02 — in MANAGED trust mode BOTH the transport HMAC (identity) and a valid
Simorgh Ed25519 grant (execution authority) are mandatory. A request whose grant
is missing / invalid / expired / spent(single-use burned) / wrong-tenant /
wrong-user must FAIL BEFORE run creation — no task row, no effect. In
LOCAL-STANDALONE mode a managed grant is REFUSED (standalone never silently
claims managed authority). These reuse the lineage's ``worker_admission`` /
``managed_execution`` admission (``_admit_execution_grant``) — no second grant
framework is built here.

CXR-04 — the create path admits transport commands via the existing ATOMIC
``claim(nonce, ts)`` (never ``seen`` then ``record``): a replayed transport nonce
is rejected 409, and a nonce-store failure FAILS CLOSED (503, no run). The store
is a durable, file-backed ``SqliteNonceStore`` so the Master Integrator can drive
the concurrency/restart cases across real OS processes.

The model "brain" is a no-op spawn (returns the live pid) — the admission gate,
not execution, is under test. Ed25519 grants are REAL (no mocks).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli import runtime_command_auth as rca
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.token_auth import token_auth_middleware
from youtab_agent_cli.web_routers import runtime
from youtab_runtime import managed_execution as mx
from youtab_runtime.contracts import BrainCommandEnvelopeV2

SECRET = secrets.token_urlsafe(48)
KEY_ID = "brain-ed25519-cxr"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([53]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS_JSON = json.dumps({KEY_ID: _PUB})

TENANT = "tenantA"
USER = "userA"


def _no_op_spawn(task, workspace, *, board=None):
    return os.getpid()


class _FakeProfile:
    name = "default"
    description = "test agent"
    model = "local-deterministic"
    provider = "local"
    skill_count = 1
    is_default = True


def _wire_common(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db_path))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)

    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider
    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix(
        "/api/runtime/v1/", provider="runtime-service", capability="runtime")

    monkeypatch.setattr(
        "youtab_agent_cli.profiles.list_profiles", lambda: [_FakeProfile()]
    )
    # A managed model-run must resolve a concrete substrate at create; resolve the
    # fake profile's identity so a POSITIVE managed create is fully-resolved (and
    # fail-closed would otherwise refuse it).
    monkeypatch.setattr(
        "youtab_agent_cli.web_routers.runtime._profile_default_identity",
        lambda agent: ("local", "local-deterministic", "http://127.0.0.1:11434"),
    )

    runtime._spawn_override = _no_op_spawn
    runtime._nonce_store = None
    runtime._grant_boundary = None

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)
    token_auth.require_route_ownership(
        provider="runtime-service", path="/api/runtime/v1/", is_prefix=True,
        capability="runtime")
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()
    return app, db_path


@pytest.fixture()
def managed(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    monkeypatch.setenv("YOUTAB_BRAIN_PUBLIC_KEYS", _KEYS_JSON)
    app, db_path = _wire_common(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c._db_path = db_path  # type: ignore[attr-defined]
        yield c
    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None
    runtime._grant_boundary = None


@pytest.fixture()
def standalone(tmp_path, monkeypatch):
    # Default (local-standalone) trust mode: no YOUTAB_RUNTIME_TRUST_MODE.
    monkeypatch.delenv("YOUTAB_RUNTIME_TRUST_MODE", raising=False)
    app, db_path = _wire_common(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c._db_path = db_path  # type: ignore[attr-defined]
        yield c
    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None
    runtime._grant_boundary = None


# ── grant minting + request helpers ───────────────────────────────────────────


def _mint_grant(*, signer=_SIGNER, tenant=TENANT, user=USER, workspace="-",
                toolsets=("*",), now=None, nonce=None, **over):
    now = now or datetime.now(UTC).replace(microsecond=0)
    fields = dict(
        schema_version="youtab.agent-command.v2",
        issuer="youtab-one-brain",
        audience="youtab-agent-runtime",
        protocol_version="youtab.runtime-sig.v2",
        command_id=f"cmd-{uuid.uuid4().hex[:12]}",
        task_id=f"task-{uuid.uuid4().hex[:12]}",
        root_run_id=f"run-{uuid.uuid4().hex[:12]}",
        parent_task_id=None,
        attempt=1,
        tenant_id=tenant,
        workspace_id=workspace,
        user_id=user,
        membership_generation=1,
        authorization_epoch=1,
        agent_id="agent-default",
        engine_id="engine-local",
        trace_id=f"trace-{uuid.uuid4().hex[:8]}",
        nonce=nonce or f"grant-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
        objective="add two and two",
        allowed_toolsets=toolsets,
        allowed_memory_scopes=("*",),
        allowed_artifact_scopes=(),
        effect_proposal_scopes=(),
        reasoning={
            "max_iterations": 4, "max_spawn_depth": 1, "max_concurrent_agents": 1,
            "max_total_tokens": 2000, "max_cost_micros": 0, "max_retries": 0,
            "deadline_at": now + timedelta(minutes=20),
        },
        issued_at=now,
        expires_at=now + timedelta(minutes=30),
        key_id=KEY_ID,
        signature="0" * 88,
    )
    fields.update(over)
    env = BrainCommandEnvelopeV2(**fields)
    sig = base64.b64encode(signer.sign(env.canonical_payload())).decode()
    grant = env.model_dump(mode="json")
    grant["signature"] = sig
    header = base64.b64encode(json.dumps(grant, separators=(",", ":")).encode()).decode()
    return env, header


def _headers(tenant=TENANT, user=USER, correlation=None, workspace=None):
    h = {
        "Authorization": f"Bearer {SECRET}",
        "X-Youtab-Tenant-Id": tenant,
        "X-Youtab-User-Id": user,
        "X-Youtab-Roles": "member",
        "X-Youtab-Correlation-Id": correlation or f"cid-{uuid.uuid4().hex[:8]}",
    }
    if workspace is not None:
        # Canonical Runtime-identity workspace. In managed mode this MUST equal the
        # grant envelope's workspace_id (``_check_binding``) or admission refuses it.
        h[rca.WORKSPACE_HEADER] = workspace
    return h


def _sign(method, path, tenant, user, body, correlation, nonce=None):
    ts = int(time.time())
    nonce = nonce or f"n-{uuid.uuid4().hex}"
    canon = rca.canonical_string(
        method=method, path=path, tenant=tenant, user=user,
        timestamp=str(ts), nonce=nonce, body=body, correlation=correlation,
    )
    return {rca.SIGNATURE_HEADER: rca.compute_signature(SECRET, canon),
            rca.TIMESTAMP_HEADER: str(ts), rca.NONCE_HEADER: nonce}


def _create(client, *, tenant=TENANT, user=USER, grant_header=None,
            task="add 2 and 2", nonce=None, workspace=None, idem_key=None):
    body = json.dumps({"agent": "default", "task": task}).encode()
    path = "/api/runtime/v1/runs"
    h = _headers(tenant, user, workspace=workspace)
    h.update(_sign("POST", path, tenant, user, body, h["X-Youtab-Correlation-Id"], nonce=nonce))
    h["Content-Type"] = "application/json"
    if grant_header is not None:
        h[mx.GRANT_HEADER] = grant_header
    if idem_key is not None:
        h["Idempotency-Key"] = idem_key
    return client.post(path, content=body, headers=h)


def _task_count(db_path):
    """Total durable task rows on disk (fixtures are isolated per test).

    A refusal BEFORE creation never touches the run store, so the DB (and its
    ``tasks`` table) may not exist yet — that IS zero rows / zero effect.
    """
    if not os.path.exists(str(db_path)):
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if not exists:
            return 0
        return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    finally:
        conn.close()


# ── CXR-02 managed: grant mandatory, fail BEFORE creation ─────────────────────


def test_managed_missing_grant_refused_before_creation(managed):
    r = _create(managed, grant_header=None)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_required"
    # Fail BEFORE creation: no task row, no effect.
    assert _task_count(managed._db_path) == 0


def test_managed_forged_grant_refused_before_creation(managed):
    forged = Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32)
    _, header = _mint_grant(signer=forged)
    r = _create(managed, grant_header=header)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_rejected"
    assert _task_count(managed._db_path) == 0


def test_managed_expired_grant_refused_before_creation(managed):
    past = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    _, header = _mint_grant(now=past)
    r = _create(managed, grant_header=header)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_rejected"
    assert _task_count(managed._db_path) == 0


def test_managed_wrong_tenant_grant_refused_before_creation(managed):
    # Grant is for tenant-beta but the caller identity is tenantA -> identity
    # mismatch, fail-closed at ingress (grant is never widened onto tenantA).
    _, header = _mint_grant(tenant="tenant-beta")
    r = _create(managed, grant_header=header)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_identity_mismatch"
    assert _task_count(managed._db_path) == 0


def test_managed_wrong_user_grant_refused_before_creation(managed):
    # Same tenant, DIFFERENT user: the grant binds (tenant, user); a valid grant
    # for another user in the same tenant must not be honoured for userA.
    _, header = _mint_grant(user="user-other")
    r = _create(managed, grant_header=header)
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_identity_mismatch"
    assert _task_count(managed._db_path) == 0


def test_managed_spent_grant_refused_before_creation(managed):
    # A grant is single-use: once its nonce is burned it can never authorize a
    # second run (the runtime's effective revocation of a spent credential). The
    # SECOND use is refused and creates NO new row.
    _, header = _mint_grant(nonce="grant-spent-fixed-0123456789abcdef0123456789ab")
    first = _create(managed, grant_header=header)
    assert first.status_code == 200, first.text
    after_first = _task_count(managed._db_path)
    assert after_first == 1

    second = _create(managed, grant_header=header)  # replay the SAME grant
    assert second.status_code in (401, 403), second.text
    assert second.json()["detail"]["error"] == "grant_rejected"
    # No NEW effect from the refused replay.
    assert _task_count(managed._db_path) == after_first


def test_managed_positive_create_admits_and_persists_grant(managed):
    # Sanity control: the gate is not merely always-refusing — a valid grant is
    # admitted, the run is created, and the grant is persisted for worker re-admit.
    env, header = _mint_grant()
    r = _create(managed, grant_header=header)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert _task_count(managed._db_path) == 1
    ev = managed.get(
        f"/api/runtime/v1/runs/{run_id}/events?after=0", headers=_headers()
    ).json()["events"]
    grant_events = [e for e in ev if e["kind"] == "runtime_execution_grant"]
    assert len(grant_events) == 1, ev
    persisted = mx.decode_grant_header(grant_events[0]["payload"]["grant"])
    assert persisted.tenant_id == TENANT and persisted.user_id == USER


def _run_workspace(db_path, run_id):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT workspace FROM tasks WHERE id = ?", (run_id,)
        ).fetchone()["workspace"]
    finally:
        conn.close()


def _rows_for_key(db_path, key):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()["n"]
    finally:
        conn.close()


# ── C3: managed admission workspace flows into the idempotency scope ───────────


def test_managed_grant_workspace_scopes_idempotency(managed):
    # A grant minted for a CONCRETE workspace_id + the matching
    # ``x-youtab-workspace-id`` header is admitted (``_check_binding`` requires
    # header == grant workspace_id), and the admitted workspace is PERSISTED as the
    # run's idempotency scope. Two DIFFERENT authorised workspaces reusing the SAME
    # Idempotency-Key mint INDEPENDENT runs (each single-use grant carries its own
    # nonce, so this is not a transport replay) — proving idempotency is scoped by
    # (tenant, user, workspace, key) at the managed boundary too.
    key = "idem-managed-ws-001"

    _, header_a = _mint_grant(workspace="ws-alpha")
    ra = _create(managed, grant_header=header_a, workspace="ws-alpha", idem_key=key)
    assert ra.status_code == 200, ra.text
    run_a = ra.json()["run_id"]
    assert _run_workspace(managed._db_path, run_a) == "ws-alpha"

    _, header_b = _mint_grant(workspace="ws-beta")
    rb = _create(managed, grant_header=header_b, workspace="ws-beta", idem_key=key)
    assert rb.status_code == 200, rb.text
    run_b = rb.json()["run_id"]
    assert run_b != run_a, "same key in a different authorised workspace -> new run"
    assert _run_workspace(managed._db_path, run_b) == "ws-beta"

    # Exactly two durable rows under the shared key: one per workspace.
    assert _rows_for_key(managed._db_path, key) == 2


def test_managed_grant_workspace_mismatch_refused_before_creation(managed):
    # Defence-in-depth control: a grant for ws-alpha but a request header naming
    # ws-beta is refused at admission (never widened), so no run is created and the
    # workspace scope can never be spoofed past the grant.
    _, header = _mint_grant(workspace="ws-alpha")
    r = _create(managed, grant_header=header, workspace="ws-beta")
    assert r.status_code in (401, 403), r.text
    assert r.json()["detail"]["error"] == "grant_workspace_mismatch"
    assert _task_count(managed._db_path) == 0


# ── CXR-02 standalone: a managed grant is refused, never silently honoured ─────


def test_standalone_refuses_managed_grant(standalone):
    _, header = _mint_grant()
    r = _create(standalone, grant_header=header)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "grant_not_accepted_in_standalone"
    # Refused before creation: no run.
    assert _task_count(standalone._db_path) == 0


def test_standalone_without_grant_creates_run(standalone):
    # Control: standalone without a grant is the approved local trust path — it
    # creates a run normally (no managed authority is claimed or required).
    r = _create(standalone, grant_header=None)
    assert r.status_code == 200, r.text
    assert _task_count(standalone._db_path) == 1


# ── CXR-04 atomic nonce at the create boundary ────────────────────────────────


def test_create_transport_nonce_replay_rejected(standalone):
    # Two fully-valid signed creates presenting the SAME transport nonce: the
    # first is admitted (atomic claim wins), the replay is rejected 409 and mints
    # no second run. This is the create path exercising the ATOMIC single-use
    # claim (never seen-then-record).
    fixed_nonce = f"n-fixed-{uuid.uuid4().hex}"
    body = json.dumps({"agent": "default", "task": "add 2 and 2"}).encode()
    path = "/api/runtime/v1/runs"

    # Sign ONCE and replay the exact same signed envelope twice.
    corr = "cid-replay"
    h = _headers(correlation=corr)
    h.update(_sign("POST", path, TENANT, USER, body, corr, nonce=fixed_nonce))
    h["Content-Type"] = "application/json"

    first = standalone.post(path, content=body, headers=h)
    assert first.status_code == 200, first.text
    second = standalone.post(path, content=body, headers=h)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "replayed"
    assert _task_count(standalone._db_path) == 1  # replay created no second run


def test_create_nonce_store_is_durable_sqlite(standalone):
    # CXR-04 durability: the create path's transport-nonce store is a durable,
    # file-backed SqliteNonceStore (not the process-local memory store), so a
    # burned nonce survives restart and is visible cross-process — the property
    # the Master Integrator's real two-process replay proof depends on.
    _create(standalone, grant_header=None)  # force lazy store construction
    store = runtime._get_nonce_store()
    assert isinstance(store, rca.SqliteNonceStore)


def test_create_nonce_store_failure_fails_closed(standalone, monkeypatch):
    # A nonce-store outage during the create path must FAIL CLOSED (503) and
    # create no run — never admit a mutation whose single-use replay claim cannot
    # be recorded, and never surface as an unhandled 500.
    class _BrokenStore:
        def claim(self, nonce, ts):
            raise rca.CommandAuthError(
                "nonce_store_unavailable", "runtime nonce store is unavailable", 503
            )

        def seen(self, nonce):
            return False

        def record(self, nonce, ts):
            pass

    monkeypatch.setattr(runtime, "_get_nonce_store", lambda: _BrokenStore())
    r = _create(standalone, grant_header=None)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == "nonce_store_unavailable"
    assert _task_count(standalone._db_path) == 0  # no effect on a fail-closed refusal
