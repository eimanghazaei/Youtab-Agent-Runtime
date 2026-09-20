"""C3 — WORKSPACE-scoped ``runs.create.idempotency.v1`` on the Agent Runtime
product surface (``POST /api/runtime/v1/runs``).

Codex Blocker 3 makes the create-run idempotency NATIVELY workspace-aware at the
Runtime's own boundary. The scope tuple is now
``(tenant, created_by, workspace, idempotency_key)`` where ``workspace`` is the
canonical Runtime-identity workspace admitted from ``x-youtab-workspace-id``
(``-`` when unscoped; guaranteed == the Simorgh grant's ``workspace_id`` after
managed admission). This adds NO second idempotency subsystem — it extends
``kanban_db.create_task_ex`` / ``_existing_idempotent`` and the route handoff.

Guarantees proven here against a REAL, file-backed run store (HTTP boundary +
direct ``create_task_ex`` for the races, exactly where the sibling v1 suite
does):

* CROSS-WORKSPACE INDEPENDENCE: the SAME (tenant, user, key) in two DIFFERENT
  authorised workspaces resolves to INDEPENDENT runs — workspace A replays only
  A's run, workspace B creates its OWN new run (B's create never hands back A's
  run id: no disclosure, no suppression);
* WITHIN a workspace: a changed effect-bearing payload is a permanent 409, a
  cosmetic-only change still replays;
* FAIL CLOSED (C3): a legacy / pre-column row whose ``workspace`` is NULL/unknown
  refuses a keyed create with a stable ``idempotency_legacy_unknown`` 409 and
  mints ZERO new run — even when its fingerprint WOULD match — so a new create
  can never silently spawn a divergent parallel run that ignores the legacy row;
* ATOMIC concurrency: concurrent same-workspace creates collapse to exactly one
  run; concurrent cross-workspace creates yield exactly one run PER workspace;
* durability across a store reopen (per workspace);
* a genuine PRE-COLUMN ``workspace`` DB upgrade adds the column, leaves legacy
  rows NULL, and then fails a keyed create closed.

Default (local-standalone) trust mode: no Simorgh grant is required, and the v1
signed-command builder ignores the workspace header, so a non-``-`` workspace
flows straight into ``identity.workspace``. The managed-admission workspace path
(grant ``workspace_id`` == header) is covered in
``test_runtime_create_admission_cxr.py``.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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

# A strong secret that clears the runtime-service entropy gate.
SECRET = secrets.token_urlsafe(48)

WS_A = "ws-alpha"
WS_B = "ws-beta"


class _FakeProfile:
    def __init__(self, name):
        self.name = name
        self.description = "test agent"
        self.model = "local-deterministic"
        self.provider = "local"
        self.skill_count = 3
        self.is_default = name == "default"


def _no_op_spawn(task, workspace, *, board=None):
    """A spawn that does not launch an LLM worker (returns the live test pid so the
    dispatcher keeps the run in a stable claimed state). The contract under test is
    create-time dedupe, not execution."""
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
    runtime._spawn_override = _no_op_spawn
    runtime._nonce_store = None  # fresh signed-command replay store

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)
    return app


def _build_app() -> FastAPI:
    _register_auth()
    return _make_app()


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

    app = _build_app()
    with TestClient(app) as c:
        c._db_path = db_path  # type: ignore[attr-defined]
        c._tmp_path = tmp_path  # type: ignore[attr-defined]
        yield c

    runtime.stop_dispatcher()
    runtime._spawn_override = None
    runtime._nonce_store = None


# --------------------------------------------------------------------------
# helpers
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
        # Canonical Runtime-identity workspace. The v1 signed-command builder
        # ignores this header, so it flows straight into ``identity.workspace``.
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


def _create(
    client,
    *,
    idem_key=None,
    task="add 2 and 2",
    tenant="tenantA",
    user="userA",
    correlation="cid-test",
    workspace=None,
    nonce=None,
    extra=None,
):
    """Signed create. Distinct command nonce each call; the SAME Idempotency-Key
    and workspace header may legitimately span calls (both are headers, not part
    of the v1 signed canonical string)."""
    payload = {"agent": "default", "task": task}
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers(tenant, user, correlation, workspace=workspace)
    headers.update(
        _sign("POST", path, tenant, user, body,
              nonce=nonce or f"n-{uuid.uuid4().hex}", correlation=correlation)
    )
    headers["Content-Type"] = "application/json"
    if idem_key is not None:
        headers["Idempotency-Key"] = idem_key
    return client.post(path, content=body, headers=headers)


def _row_count_ws(db_path, *, tenant, user, key, workspace):
    """Durable rows for a fully-scoped key (incl. workspace), read raw off disk."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE idempotency_key = ? AND tenant = ? AND created_by = ? "
            "AND workspace IS ?",
            (key, tenant, user, workspace),
        ).fetchone()["n"]
    finally:
        conn.close()


def _row_count_any_ws(db_path, *, tenant, user, key):
    """Durable rows for (key, tenant, user) across ALL workspaces."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE idempotency_key = ? AND tenant = ? AND created_by = ?",
            (key, tenant, user),
        ).fetchone()["n"]
    finally:
        conn.close()


def _stored_workspace(db_path, run_id):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT workspace FROM tasks WHERE id = ?", (run_id,)
        ).fetchone()["workspace"]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# cross-workspace independence (no disclosure, no suppression)
# --------------------------------------------------------------------------


def test_same_key_two_workspaces_are_independent_runs(client):
    key = "idem-ws-independent-001"

    # Workspace A: create + replay -> the SAME A run.
    a1 = _create(client, idem_key=key, workspace=WS_A)
    assert a1.status_code == 200, a1.text
    run_a = a1.json()["run_id"]
    a2 = _create(client, idem_key=key, workspace=WS_A)
    assert a2.status_code == 200, a2.text
    assert a2.json()["run_id"] == run_a, "A replay must return A's own run"

    # Workspace B (SAME tenant/user/key): gets its OWN new run — never A's. This is
    # the enumeration-resistance at the create boundary: B's create returns B's new
    # run id, so a colliding key in another workspace neither discloses nor
    # suppresses A's run.
    b1 = _create(client, idem_key=key, workspace=WS_B)
    assert b1.status_code == 200, b1.text
    run_b = b1.json()["run_id"]
    assert run_b != run_a, "workspace B must not be handed workspace A's run"
    b2 = _create(client, idem_key=key, workspace=WS_B)
    assert b2.status_code == 200, b2.text
    assert b2.json()["run_id"] == run_b, "B replay must return B's own run"

    # Each workspace persisted exactly ONE run under the shared key.
    assert _row_count_ws(client._db_path, tenant="tenantA", user="userA",
                         key=key, workspace=WS_A) == 1
    assert _row_count_ws(client._db_path, tenant="tenantA", user="userA",
                         key=key, workspace=WS_B) == 1
    assert _row_count_any_ws(client._db_path, tenant="tenantA", user="userA",
                             key=key) == 2
    # The scope value is actually persisted on each row.
    assert _stored_workspace(client._db_path, run_a) == WS_A
    assert _stored_workspace(client._db_path, run_b) == WS_B


def test_unscoped_and_scoped_same_key_are_independent(client):
    # A standalone (unscoped "-") create and a workspace-scoped create under the
    # SAME key are ALSO independent: "-" is a concrete scope, not a wildcard.
    key = "idem-ws-unscoped-001"
    r_unscoped = _create(client, idem_key=key)  # no workspace header -> "-"
    assert r_unscoped.status_code == 200, r_unscoped.text
    run_unscoped = r_unscoped.json()["run_id"]
    assert _stored_workspace(client._db_path, run_unscoped) == rca.WORKSPACE_UNSCOPED

    r_scoped = _create(client, idem_key=key, workspace=WS_A)
    assert r_scoped.status_code == 200, r_scoped.text
    assert r_scoped.json()["run_id"] != run_unscoped

    # Unscoped replay still returns the unscoped run.
    r_unscoped2 = _create(client, idem_key=key)
    assert r_unscoped2.status_code == 200, r_unscoped2.text
    assert r_unscoped2.json()["run_id"] == run_unscoped


# --------------------------------------------------------------------------
# within a workspace: changed payload -> 409; cosmetic change replays
# --------------------------------------------------------------------------


def test_changed_payload_within_workspace_is_409(client):
    key = "idem-ws-conflict-001"
    r1 = _create(client, idem_key=key, workspace=WS_A, task="compute A")
    assert r1.status_code == 200, r1.text
    run_a = r1.json()["run_id"]

    r2 = _create(client, idem_key=key, workspace=WS_A, task="compute B (different)")
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert detail["error"] == "idempotency_key_conflict"
    assert detail["run_id"] == run_a
    assert _row_count_ws(client._db_path, tenant="tenantA", user="userA",
                         key=key, workspace=WS_A) == 1


def test_cosmetic_change_within_workspace_replays(client):
    key = "idem-ws-cosmetic-001"
    r1 = _create(client, idem_key=key, workspace=WS_A, correlation="cid-first")
    assert r1.status_code == 200, r1.text
    r2 = _create(client, idem_key=key, workspace=WS_A, correlation="cid-second")
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == r1.json()["run_id"]


def test_changed_payload_in_one_workspace_does_not_affect_the_other(client):
    # A conflict in workspace A must not touch or suppress workspace B's run.
    key = "idem-ws-cross-conflict-001"
    a1 = _create(client, idem_key=key, workspace=WS_A, task="A original")
    assert a1.status_code == 200, a1.text
    b1 = _create(client, idem_key=key, workspace=WS_B, task="B original")
    assert b1.status_code == 200, b1.text
    run_b = b1.json()["run_id"]

    # Changed payload in A -> 409 in A only.
    a2 = _create(client, idem_key=key, workspace=WS_A, task="A changed")
    assert a2.status_code == 409, a2.text

    # B still replays its own run untouched.
    b2 = _create(client, idem_key=key, workspace=WS_B, task="B original")
    assert b2.status_code == 200, b2.text
    assert b2.json()["run_id"] == run_b


# --------------------------------------------------------------------------
# legacy NULL-workspace row -> fail closed (C3), zero new run
# --------------------------------------------------------------------------


def test_legacy_null_workspace_row_fails_closed_over_http(client):
    """An existing keyed row whose ``workspace`` is NULL (a legacy/pre-column row)
    must FAIL CLOSED — even though its fingerprint would match — so a keyed create
    neither replays it nor mints a divergent parallel run in the requested
    workspace. A stable ``idempotency_legacy_unknown`` 409, zero new run."""
    key = "idem-ws-legacy-http-001"
    # Seed a legacy row: keyed, owned by (tenantA,userA), a KNOWN fingerprint but
    # workspace NULL — exactly what the additive migration leaves for a row created
    # before the ``workspace`` column existed. The known fingerprint proves the
    # fail-closed comes from the NULL workspace, not a NULL fingerprint.
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks (id, title, status, priority, created_by, "
                "created_at, workspace_kind, tenant, idempotency_key, "
                "idempotency_fingerprint, workspace) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, NULL)",
                ("t_legacy_ws", "legacy", "running", 0, "userA",
                 int(time.time()), "scratch", "tenantA", key,
                 "some-known-fingerprint"),
            )

    r = _create(client, idem_key=key, workspace=WS_A, task="anything")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "idempotency_legacy_unknown"
    assert detail["run_id"] == "t_legacy_ws"
    # No new run created in ANY workspace under this key: still the one legacy row.
    assert _row_count_any_ws(client._db_path, tenant="tenantA", user="userA",
                             key=key) == 1


def test_pre_column_workspace_db_upgrade_then_create_fails_closed(tmp_path):
    """Genuine PRE-COLUMN ``workspace`` upgrade: build a ``tasks`` table with the
    ``workspace`` column DDL removed (the fingerprint column KEPT), seed a real
    legacy keyed row WITH a known fingerprint, run the additive migration, then
    assert (a) the column is added, (b) the legacy row's ``workspace`` is NULL, and
    (c) a keyed ``create_task_ex`` under the same key + a MATCHING fingerprint
    still fails closed with ``IdempotencyLegacyUnknown`` (the workspace-NULL check
    runs BEFORE the fingerprint compare) — never assuming a shared scope."""
    db_path = tmp_path / "legacy_ws.db"
    key = "idem-ws-pre-col-001"
    known_fp = "a-known-fingerprint"

    # 1. PRE-COLUMN tasks table: current schema with ONLY the ``workspace`` column
    #    DDL line removed (so that column genuinely does not exist yet).
    pre_schema = "\n".join(
        line for line in kb.SCHEMA_SQL.splitlines()
        if line.strip() != "workspace TEXT,"
    )
    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    try:
        raw.executescript(pre_schema)
        cols_before = {r["name"] for r in raw.execute("PRAGMA table_info(tasks)")}
        assert "workspace" not in cols_before, "workspace column must be absent pre-upgrade"
        assert "idempotency_fingerprint" in cols_before, "fingerprint column must exist"
        raw.execute(
            "INSERT INTO tasks (id, title, status, priority, created_by, "
            "created_at, workspace_kind, tenant, idempotency_key, "
            "idempotency_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("t_pre_col_ws", "legacy", "running", 0, "userA",
             int(time.time()), "scratch", "tenantA", key, known_fp),
        )
        raw.commit()

        # 2. Run the REAL additive migration.
        kb._migrate_add_optional_columns(raw)
        raw.commit()
        cols_after = {r["name"] for r in raw.execute("PRAGMA table_info(tasks)")}
        assert "workspace" in cols_after, "migration must add the workspace column"

        # 3. The legacy row's workspace is NULL (column existence != known scope).
        legacy_ws = raw.execute(
            "SELECT workspace FROM tasks WHERE id = ?", ("t_pre_col_ws",)
        ).fetchone()["workspace"]
        assert legacy_ws is None
    finally:
        raw.close()

    # 4. A keyed create under the same key + a MATCHING fingerprint must STILL fail
    #    closed on the NULL workspace (proves the workspace check precedes and
    #    overrides the would-be fingerprint replay).
    conn = kb.connect(db_path=db_path)
    try:
        with pytest.raises(kb.IdempotencyLegacyUnknown) as ei:
            kb.create_task_ex(
                conn,
                title="new attempt",
                assignee="default",
                created_by="userA",
                tenant="tenantA",
                idempotency_key=key,
                idempotency_fingerprint=known_fp,  # would replay if workspace were known
                idempotency_conflict_on_mismatch=True,
                idempotency_include_archived=True,
                workspace=WS_A,
            )
        assert ei.value.existing_id == "t_pre_col_ws"
    finally:
        conn.close()

    # 5. No duplicate row — still exactly the one legacy row.
    conn2 = sqlite3.connect(str(db_path))
    try:
        conn2.row_factory = sqlite3.Row
        n = conn2.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()["n"]
        assert n == 1
    finally:
        conn2.close()


# --------------------------------------------------------------------------
# atomic concurrency (same-workspace and cross-workspace)
# --------------------------------------------------------------------------


def test_concurrent_same_workspace_creates_one_run(client):
    # Two genuinely concurrent creators, SAME workspace/key, each on its own
    # connection to the same file — the real race the create path runs. Exercises
    # ``create_task_ex``'s atomic insert-or-get directly at the store layer.
    db_path = client._db_path
    key = "idem-ws-concurrent-same-001"
    fp = "same-effect-fp"
    barrier = threading.Barrier(2)
    results: list = []
    lock = threading.Lock()

    def _worker(_i):
        conn = kb.connect(board=runtime.RUNTIME_BOARD)
        try:
            barrier.wait(timeout=10)
            rid, created = kb.create_task_ex(
                conn,
                title="concurrent",
                body="race",
                assignee="default",
                created_by="userA",
                tenant="tenantA",
                idempotency_key=key,
                idempotency_fingerprint=fp,
                idempotency_conflict_on_mismatch=True,
                idempotency_include_archived=True,
                workspace=WS_A,
                board=runtime.RUNTIME_BOARD,
            )
            with lock:
                results.append((rid, created))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(_worker, range(2)))

    run_ids = {rid for rid, _ in results}
    created_flags = sorted(created for _, created in results)
    assert len(results) == 2
    assert len(run_ids) == 1, f"concurrent same-workspace creates diverged: {run_ids}"
    assert created_flags == [False, True], created_flags
    assert _row_count_ws(db_path, tenant="tenantA", user="userA",
                         key=key, workspace=WS_A) == 1


def test_concurrent_cross_workspace_creates_one_run_per_workspace(client):
    # Two genuinely concurrent creators, SAME tenant/user/key but DIFFERENT
    # workspaces: each must mint exactly one run in its OWN workspace (no false
    # dedupe across the workspace boundary, no cross-workspace suppression).
    db_path = client._db_path
    key = "idem-ws-concurrent-cross-001"
    fp = "same-effect-fp"
    barrier = threading.Barrier(2)
    results: list = []
    lock = threading.Lock()

    def _worker(ws):
        conn = kb.connect(board=runtime.RUNTIME_BOARD)
        try:
            barrier.wait(timeout=10)
            rid, created = kb.create_task_ex(
                conn,
                title="concurrent-cross",
                body="race",
                assignee="default",
                created_by="userA",
                tenant="tenantA",
                idempotency_key=key,
                idempotency_fingerprint=fp,
                idempotency_conflict_on_mismatch=True,
                idempotency_include_archived=True,
                workspace=ws,
                board=runtime.RUNTIME_BOARD,
            )
            with lock:
                results.append((ws, rid, created))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(_worker, [WS_A, WS_B]))

    assert len(results) == 2
    run_ids = {rid for _, rid, _ in results}
    assert len(run_ids) == 2, f"cross-workspace creates must NOT dedupe: {results}"
    # Both were fresh inserts (neither observed the other's row).
    assert sorted(created for _, _, created in results) == [True, True]
    assert _row_count_ws(db_path, tenant="tenantA", user="userA",
                         key=key, workspace=WS_A) == 1
    assert _row_count_ws(db_path, tenant="tenantA", user="userA",
                         key=key, workspace=WS_B) == 1


# --------------------------------------------------------------------------
# durability across a store reopen (per workspace)
# --------------------------------------------------------------------------


def test_persistence_survives_store_reopen_per_workspace(client):
    key = "idem-ws-restart-001"
    r1 = _create(client, idem_key=key, workspace=WS_A)
    assert r1.status_code == 200, r1.text
    run_a = r1.json()["run_id"]
    assert _stored_workspace(client._db_path, run_a) == WS_A

    # Simulate a restart: drop the schema-init cache + build a fresh app/client over
    # the SAME file. The re-create under the same key + workspace must resolve to
    # the SAME durable run (durability is in the store, not process memory).
    kb._INITIALIZED_PATHS.discard(str(Path(client._db_path).resolve()))
    app2 = _make_app()
    with TestClient(app2) as c2:
        c2._db_path = client._db_path  # type: ignore[attr-defined]
        r2 = _create(c2, idem_key=key, workspace=WS_A)
        assert r2.status_code == 200, r2.text
        assert r2.json()["run_id"] == run_a
        runtime.stop_dispatcher()

    assert _row_count_ws(client._db_path, tenant="tenantA", user="userA",
                         key=key, workspace=WS_A) == 1
