"""C3 (Finding 2, Runtime side) — WORKSPACE-scoped READ/OWNERSHIP isolation on
the Agent Runtime product surface (``/api/runtime/v1``).

Create-time idempotency was already made workspace-aware (the scope tuple is
``(tenant, created_by, workspace, idempotency_key)`` — see
``test_runs_create_idempotency_workspace_v1``). This suite proves the OTHER half
of the boundary: the read / ownership predicate. Before the fix, reads were
scoped to ``(tenant, created_by)`` ONLY, so a user who is a member of both
workspace A and workspace B could, while acting in B, still READ and MUTATE the
runs they created in A. Workspace was not part of the ownership check.

The fix enforces ``(tenant, created_by, workspace)`` ONCE at the single shared
ownership loader ``runtime._load_owned_task`` (behind get / events / logs /
artifacts / cancel / retry / pause / resume / answer / approve) and adds the
workspace predicate to the list query (``runtime_list_runs``). This suite is the
adversarial matrix for that boundary.

Proven here against a REAL, file-backed run store over the authenticated HTTP
surface (same harness the sibling idempotency suite uses):

* a run created in workspace A is INVISIBLE from workspace B across EVERY read
  surface (detail / events / artifacts list / artifact bytes / logs) and is
  EXCLUDED from B's list — all as the SAME enum-resistant 404 a cross-user
  mismatch already produces (absent and forbidden are indistinguishable);
* a run created in A is IMMUTABLE from B (cancel over HTTP with a valid signed
  command from B returns 404 and writes ZERO cancel event / ZERO new run);
* every cross-workspace denial creates zero run / effect / provider call;
* the SAME (tenant, user, idempotency-key) in A and B remains two INDEPENDENT
  runs, each visible only in its own workspace (no disclosure, no suppression);
* wrong-tenant and different-user reads remain isolated (the three scope
  dimensions compose);
* the "-" unscoped caller matches ONLY "-" rows, never a concrete workspace;
* a legacy / pre-column row whose stored ``workspace`` is NULL is FAIL-CLOSED:
  invisible to a concrete-workspace read, to the "-" unscoped read, and to the
  list under any workspace (a NULL scope is not attributable to any workspace);
* the shared loader (which every mutation funnels through — retry / pause /
  resume / answer / approve) raises the enum-resistant 404 on a workspace
  mismatch, so those mutation surfaces are covered at the boundary too.

Default (local-standalone) trust mode: no Simorgh grant is required, and the v1
signed-command builder ignores the workspace header, so a non-``-`` workspace
flows straight into ``identity.workspace`` and a cross-workspace mutation clears
signature + admission and is then rejected purely by the ownership predicate.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli import runtime_command_auth as rca
from youtab_agent_cli.dashboard_auth import registry as auth_registry
from youtab_agent_cli.dashboard_auth import token_auth
from youtab_agent_cli.dashboard_auth.token_auth import token_auth_middleware
from youtab_agent_cli.web_routers import runtime

# A strong secret that clears the runtime-service entropy gate.
SECRET = secrets.token_urlsafe(48)

WS_A = "ws-alpha"
WS_B = "ws-beta"
DASH = rca.WORKSPACE_UNSCOPED  # "-" — the canonical unscoped/standalone value


class _FakeProfile:
    def __init__(self, name):
        self.name = name
        self.description = "test agent"
        self.model = "local-deterministic"
        self.provider = "local"
        self.skill_count = 3
        self.is_default = name == "default"


# Records every spawn so we can prove a cross-workspace denial launches NO worker
# (== no provider call). Only the legitimate in-workspace create should spawn.
_SPAWNS: list[str] = []


def _recording_spawn(task, workspace, *, board=None):
    """A spawn that launches no LLM worker but records that it ran. Returns the
    live test pid so the dispatcher keeps the run in a stable claimed state; the
    contract under test is read/ownership scoping, not execution."""
    _SPAWNS.append(getattr(task, "id", "?"))
    return os.getpid()


def _register_auth() -> None:
    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider

    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix(
        "/api/runtime/v1/", provider="runtime-service", capability="runtime"
    )
    token_auth.require_route_ownership(
        provider="runtime-service", path="/api/runtime/v1/", is_prefix=True,
        capability="runtime")
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()


def _make_app() -> FastAPI:
    runtime._spawn_override = _recording_spawn
    runtime._nonce_store = None  # fresh signed-command replay store

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)
    return app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db_path))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)

    monkeypatch.setattr(
        "youtab_agent_cli.profiles.list_profiles", lambda: [_FakeProfile("default")]
    )

    _SPAWNS.clear()
    _register_auth()
    app = _make_app()
    with TestClient(app) as c:
        c._db_path = db_path  # type: ignore[attr-defined]
        yield c

    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None


# --------------------------------------------------------------------------
# request helpers (mirror the sibling idempotency suite)
# --------------------------------------------------------------------------


def _identity_headers(tenant="tenantA", user="userA", correlation="cid-test",
                      workspace=None):
    h = {
        "Authorization": f"Bearer {SECRET}",
        "X-Youtab-Tenant-Id": tenant,
        "X-Youtab-User-Id": user,
        "X-Youtab-Roles": "member",
        "X-Youtab-Correlation-Id": correlation,
    }
    if workspace is not None:
        # The v1 signed-command builder ignores this header, so it flows straight
        # into ``identity.workspace``.
        h[rca.WORKSPACE_HEADER] = workspace
    return h


def _sign(method, path, tenant, user, body: bytes, nonce, correlation="cid-test"):
    ts = int(time.time())
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


def _create(client, *, idem_key=None, task="add 2 and 2", tenant="tenantA",
            user="userA", correlation="cid-test", workspace=None):
    payload = {"agent": "default", "task": task}
    body = json.dumps(payload).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers(tenant, user, correlation, workspace=workspace)
    headers.update(
        _sign("POST", path, tenant, user, body,
              nonce=f"n-{uuid.uuid4().hex}", correlation=correlation)
    )
    headers["Content-Type"] = "application/json"
    if idem_key is not None:
        headers["Idempotency-Key"] = idem_key
    r = client.post(path, content=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _cancel(client, run_id, *, tenant="tenantA", user="userA", workspace=None):
    """Signed cancel (empty body). Returns the raw response so callers assert."""
    path = f"/api/runtime/v1/runs/{run_id}/cancel"
    body = b""
    headers = _identity_headers(tenant, user, workspace=workspace)
    headers.update(_sign("POST", path, tenant, user, body, nonce=f"n-{uuid.uuid4().hex}"))
    return client.post(path, content=body, headers=headers)


# read-surface GETs -----------------------------------------------------------

def _get_detail(client, run_id, **kw):
    return client.get(f"/api/runtime/v1/runs/{run_id}",
                      headers=_identity_headers(**kw))


def _get_events(client, run_id, **kw):
    return client.get(f"/api/runtime/v1/runs/{run_id}/events",
                      headers=_identity_headers(**kw))


def _get_artifacts(client, run_id, **kw):
    return client.get(f"/api/runtime/v1/runs/{run_id}/artifacts",
                      headers=_identity_headers(**kw))


def _get_artifact_bytes(client, run_id, artifact_id, **kw):
    return client.get(f"/api/runtime/v1/runs/{run_id}/artifacts/{artifact_id}",
                      headers=_identity_headers(**kw))


def _get_logs(client, run_id, **kw):
    return client.get(f"/api/runtime/v1/runs/{run_id}/logs",
                      headers=_identity_headers(**kw))


def _list_run_ids(client, **kw):
    r = client.get("/api/runtime/v1/runs", headers=_identity_headers(**kw))
    assert r.status_code == 200, r.text
    return {run["run_id"] for run in r.json()["runs"]}


# direct-DB probes ------------------------------------------------------------

def _set_workspace_null(db_path, run_id):
    """Simulate a legacy / pre-``workspace``-column row: force stored NULL."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE tasks SET workspace = NULL WHERE id = ?", (run_id,))
        conn.commit()
    finally:
        conn.close()


def _event_count(db_path, run_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (run_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _task_count(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# a run created in A is INVISIBLE from B across every read surface
# --------------------------------------------------------------------------


def test_run_created_in_A_is_invisible_from_B_across_all_reads(client):
    run_a = _create(client, workspace=WS_A)

    # From the OWNING workspace A: every read succeeds and the run is listed.
    assert _get_detail(client, run_a, workspace=WS_A).status_code == 200
    assert _get_events(client, run_a, workspace=WS_A).status_code == 200
    assert _get_artifacts(client, run_a, workspace=WS_A).status_code == 200
    assert _get_logs(client, run_a, workspace=WS_A).status_code == 200
    assert run_a in _list_run_ids(client, workspace=WS_A)

    # From workspace B (SAME tenant + user): every surface is a 404, and the run
    # is not listed. Absent and forbidden are indistinguishable.
    assert _get_detail(client, run_a, workspace=WS_B).status_code == 404
    assert _get_events(client, run_a, workspace=WS_B).status_code == 404
    assert _get_artifacts(client, run_a, workspace=WS_B).status_code == 404
    # An artifact id under a run that B cannot see must 404 at the run gate,
    # never reaching the artifact lookup.
    assert _get_artifact_bytes(client, run_a, 1, workspace=WS_B).status_code == 404
    assert _get_logs(client, run_a, workspace=WS_B).status_code == 404
    assert run_a not in _list_run_ids(client, workspace=WS_B)
    assert _list_run_ids(client, workspace=WS_B) == set()

    # Same 404 shape as a cross-user mismatch (enum-resistant).
    body = _get_detail(client, run_a, workspace=WS_B).json()
    assert body["detail"] == {"error": "run_not_found"}


def test_run_created_in_B_is_invisible_from_A(client):
    # Symmetry: the isolation is not one-directional.
    run_b = _create(client, workspace=WS_B)
    assert _get_detail(client, run_b, workspace=WS_A).status_code == 404
    assert run_b not in _list_run_ids(client, workspace=WS_A)
    assert _get_detail(client, run_b, workspace=WS_B).status_code == 200


# --------------------------------------------------------------------------
# a run created in A is IMMUTABLE from B — and denial writes nothing
# --------------------------------------------------------------------------


def test_run_created_in_A_is_immutable_from_B_and_writes_nothing(client):
    run_a = _create(client, workspace=WS_A)
    tasks_before = _task_count(client._db_path)
    events_before = _event_count(client._db_path, run_a)
    spawns_before = len(_SPAWNS)

    # A valid signed cancel from workspace B (signature + standalone admission
    # both clear; only the ownership predicate rejects it).
    r = _cancel(client, run_a, workspace=WS_B)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == {"error": "run_not_found"}

    # Zero side effects: no new run, no cancel event on A's run, no worker spawn.
    # B's cancel 404s at the ownership loader INSIDE the write transaction, before
    # any event append or worker-kill, so the run is left byte-for-byte intact.
    assert _task_count(client._db_path) == tasks_before
    assert _event_count(client._db_path, run_a) == events_before
    assert len(_SPAWNS) == spawns_before

    # A still fully owns the run — B's rejected attempt changed nothing. (We do
    # not drive cancel-from-A here: this harness's spawn stub records the live
    # test pid as the worker pid, so a *successful* cancel would terminate_pid
    # the test process itself; immutability-from-B is the contract under test and
    # is already proven above.)
    assert _get_detail(client, run_a, workspace=WS_A).status_code == 200


def test_shared_loader_rejects_cross_workspace_for_every_mutation(client):
    """retry / pause / resume / answer / approve all funnel through the single
    shared loader ``_load_owned_task``; proving it 404s on a workspace mismatch
    covers those mutation surfaces at the boundary without re-driving each over
    HTTP with its own signed-command body."""
    run_a = _create(client, workspace=WS_A)

    def _identity(ws):
        return runtime.RuntimeIdentity(
            tenant="tenantA", user="userA", roles=["member"],
            correlation_id="cid-test", idempotency_key=None, workspace=ws,
        )

    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        # Owning workspace: loads fine.
        assert runtime._load_owned_task(conn, run_a, _identity(WS_A)).id == run_a
        # Foreign workspace / unscoped caller: enum-resistant 404.
        for ws in (WS_B, DASH):
            with pytest.raises(HTTPException) as ei:
                runtime._load_owned_task(conn, run_a, _identity(ws))
            assert ei.value.status_code == 404
            assert ei.value.detail == {"error": "run_not_found"}


# --------------------------------------------------------------------------
# tenant / user still isolated (the three scope dimensions compose)
# --------------------------------------------------------------------------


def test_wrong_tenant_and_different_user_remain_isolated(client):
    run_a = _create(client, tenant="tenantA", user="userA", workspace=WS_A)
    # Correct workspace but wrong tenant -> 404.
    assert _get_detail(client, run_a, tenant="tenantX", user="userA",
                       workspace=WS_A).status_code == 404
    # Correct workspace + tenant but different user -> 404.
    assert _get_detail(client, run_a, tenant="tenantA", user="userB",
                       workspace=WS_A).status_code == 404
    # All three correct -> 200.
    assert _get_detail(client, run_a, tenant="tenantA", user="userA",
                       workspace=WS_A).status_code == 200


# --------------------------------------------------------------------------
# same idempotency key across workspaces stays two independent runs
# --------------------------------------------------------------------------


def test_same_key_two_workspaces_yield_independent_isolated_runs(client):
    key = "idem-iso-001"
    run_a = _create(client, idem_key=key, workspace=WS_A)
    run_b = _create(client, idem_key=key, workspace=WS_B)
    assert run_a != run_b, "same key in A and B must be independent runs"

    # Each is visible only in its own workspace.
    assert _list_run_ids(client, workspace=WS_A) == {run_a}
    assert _list_run_ids(client, workspace=WS_B) == {run_b}
    assert _get_detail(client, run_a, workspace=WS_B).status_code == 404
    assert _get_detail(client, run_b, workspace=WS_A).status_code == 404


# --------------------------------------------------------------------------
# the "-" unscoped caller matches only "-" rows
# --------------------------------------------------------------------------


def test_unscoped_dash_caller_matches_only_dash_rows(client):
    run_unscoped = _create(client)  # no workspace header -> stored "-"
    run_scoped = _create(client, workspace=WS_A)

    # A "-" caller sees only the "-" run.
    assert _get_detail(client, run_unscoped, workspace=DASH).status_code == 200
    assert _get_detail(client, run_scoped, workspace=DASH).status_code == 404
    # No-header caller normalises to "-" identically.
    assert _get_detail(client, run_unscoped).status_code == 200
    assert _get_detail(client, run_scoped).status_code == 404
    # A concrete-workspace caller does NOT see the "-" run.
    assert _get_detail(client, run_unscoped, workspace=WS_A).status_code == 404
    assert _list_run_ids(client, workspace=DASH) == {run_unscoped}
    assert _list_run_ids(client, workspace=WS_A) == {run_scoped}


# --------------------------------------------------------------------------
# legacy NULL-workspace row is FAIL-CLOSED
# --------------------------------------------------------------------------


def test_legacy_null_workspace_row_is_invisible_everywhere(client):
    run_a = _create(client, workspace=WS_A)
    # It is readable while stored with a concrete workspace...
    assert _get_detail(client, run_a, workspace=WS_A).status_code == 200

    # ...but once its stored workspace is NULL (a pre-column legacy row whose
    # scope is UNKNOWN), it is not attributable to ANY workspace and must fail
    # closed: invisible to the original concrete workspace, to the "-" unscoped
    # caller, to a foreign workspace, and excluded from every list.
    _set_workspace_null(client._db_path, run_a)

    assert _get_detail(client, run_a, workspace=WS_A).status_code == 404
    assert _get_detail(client, run_a, workspace=DASH).status_code == 404
    assert _get_detail(client, run_a).status_code == 404  # no header -> "-"
    assert _get_detail(client, run_a, workspace=WS_B).status_code == 404
    assert _get_events(client, run_a, workspace=WS_A).status_code == 404
    assert _get_artifacts(client, run_a, workspace=WS_A).status_code == 404
    assert _get_logs(client, run_a, workspace=WS_A).status_code == 404
    assert run_a not in _list_run_ids(client, workspace=WS_A)
    assert run_a not in _list_run_ids(client, workspace=DASH)

    # And the shared loader agrees at the unit level for both concrete and "-".
    def _identity(ws):
        return runtime.RuntimeIdentity(
            tenant="tenantA", user="userA", roles=["member"],
            correlation_id="cid-test", idempotency_key=None, workspace=ws,
        )

    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        for ws in (WS_A, DASH):
            with pytest.raises(HTTPException) as ei:
                runtime._load_owned_task(conn, run_a, _identity(ws))
            assert ei.value.status_code == 404
