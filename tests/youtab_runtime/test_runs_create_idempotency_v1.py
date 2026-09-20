"""Contract tests for ``runs.create.idempotency.v1`` on the Agent Runtime
product surface (``POST /api/runtime/v1/runs``) — re-homed on the #44 SECURE
lineage (CXR-05 retention + CXR-06 legacy-NULL fail-closed).

The contract (see ``web_routers/runtime.py::runtime_create_run`` and
``kanban_db.py::create_task_ex``) is additive and INDEPENDENT of any
managed-grant contract. Scoped to ``(tenant, created_by, Idempotency-Key)`` it
guarantees, durably:

* same key + same effect-bearing payload  -> the SAME run (200), no second
  create-time event, no second dispatch;
* same key + a CHANGED effect-bearing payload -> explicit 409 conflict at ANY
  age (never a silent create or overwrite) — CXR-05 permanent conflict;
* RETENTION (CXR-05): a completed / cancelled / archived run under the same key
  still returns the ORIGINAL run — a spent key never mints a duplicate, no TTL;
* FAIL CLOSED (CXR-06): an existing keyed row whose ``idempotency_fingerprint``
  is NULL/unknown (a legacy / pre-column row) is refused with a stable
  ``idempotency_legacy_unknown`` 409 — column existence is not proof of a known
  payload, so we neither replay a possibly-changed request nor create a dup;
* two same-key creates -> exactly ONE durable run (atomic insert-or-get under
  ``BEGIN IMMEDIATE``, not a read-then-write race);
* persistence survives a fresh store handle / process restart (file/DB-backed);
* ISOLATION: a different tenant or user reusing the same caller key can neither
  observe nor suppress another principal's run (enumeration-resistant 404);
* the capability marker is advertised ONLY when the durable path is active.

These are HTTP-boundary tests against a REAL, file-backed run store (so the
Master Integrator can also drive the concurrency/restart cases across real
processes). The worker "brain" is a no-op spawn (returns the live test pid) so
runs stay in a stable claimed state without needing an LLM — the contract under
test is create-time dedupe, not execution. Default (local-standalone) trust mode
so no Simorgh grant is required; managed-mode admission is covered separately.
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


class _FakeProfile:
    def __init__(self, name):
        self.name = name
        self.description = "test agent"
        self.model = "local-deterministic"
        self.provider = "local"
        self.skill_count = 3
        self.is_default = name == "default"


def _no_op_spawn(task, workspace, *, board=None):
    """A spawn that does not launch an LLM worker.

    Returns the live test-process pid so the dispatcher's liveness check keeps the
    run in a stable claimed ("running") state for the test instead of reclaiming
    it as crashed. The idempotency contract is create-time behaviour, so no real
    execution is required.
    """
    return os.getpid()


def _register_auth() -> None:
    """Register the service-auth provider + token routes and drive the real
    startup transition (declare ownership -> freeze -> verify -> VERIFIED), exactly
    as the dashboard lifespan would before accepting traffic. Done ONCE per test;
    the autouse ``_fresh_dashboard_auth_registry`` fixture gives per-test isolation.
    """
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
    """A fresh FastAPI app over the (already VERIFIED) auth state + a fresh nonce
    store. Kept separate from ``_register_auth`` so the restart test can build a
    SECOND app/client over the SAME file WITHOUT re-freezing the frozen registry
    (which would raise) — the durable store is what carries state across the
    simulated restart, not process memory."""
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

    # One known agent so create-run passes the agent existence check.
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


def _identity_headers(tenant="tenantA", user="userA", correlation="cid-test"):
    return {
        "Authorization": f"Bearer {SECRET}",
        "X-Youtab-Tenant-Id": tenant,
        "X-Youtab-User-Id": user,
        "X-Youtab-Roles": "member",
        "X-Youtab-Correlation-Id": correlation,
    }


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
    nonce=None,
    extra=None,
):
    """Signed create. Each call uses a DISTINCT command nonce (so it is never a
    replay); the SAME ``Idempotency-Key`` may legitimately span calls (it is a
    header, not part of the signed canonical string)."""
    payload = {"agent": "default", "task": task}
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers(tenant, user, correlation)
    headers.update(
        _sign("POST", path, tenant, user, body,
              nonce=nonce or f"n-{uuid.uuid4().hex}", correlation=correlation)
    )
    headers["Content-Type"] = "application/json"
    if idem_key is not None:
        headers["Idempotency-Key"] = idem_key
    return client.post(path, content=body, headers=headers)


def _row_count(db_path, *, tenant, user, key):
    """Count durable rows for a scoped key by reading the file DIRECTLY.

    A raw sqlite read (not through the app) proves the run is persisted on disk,
    not held in process memory.
    """
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


def _mode_event_count(client, run_id):
    events = client.get(
        f"/api/runtime/v1/runs/{run_id}/events", headers=_identity_headers()
    ).json()["events"]
    return len([e for e in events if e["kind"] == runtime._MODE_EVENT])


# --------------------------------------------------------------------------
# capability advertisement
# --------------------------------------------------------------------------


def test_capability_marker_present_when_active(client):
    caps = client.get(
        "/api/runtime/v1/capabilities", headers=_identity_headers()
    ).json()
    assert caps["idempotency_contract"] is True
    assert runtime.IDEMPOTENCY_CONTRACT in caps["supported"]
    assert caps["supported"].count(runtime.IDEMPOTENCY_CONTRACT) == 1


def test_capability_marker_absent_when_inactive(client, monkeypatch):
    # The marker is genuinely gated on the durable-path probe, not hardcoded:
    # force the probe False and it must disappear from both surfaces.
    monkeypatch.setattr(runtime, "_idempotency_contract_active", lambda: False)
    caps = client.get(
        "/api/runtime/v1/capabilities", headers=_identity_headers()
    ).json()
    assert caps["idempotency_contract"] is False
    assert runtime.IDEMPOTENCY_CONTRACT not in caps["supported"]


def test_capability_advertises_canonical_v2_when_supported(client):
    # CXR-01: the shared cross-repo canonical-v2 signature contract is advertised
    # (this engine genuinely implements the v2 builder + verifier).
    caps = client.get(
        "/api/runtime/v1/capabilities", headers=_identity_headers()
    ).json()
    assert caps["canonical_v2_supported"] is True
    assert runtime.CANONICAL_V2_CAPABILITY in caps["supported"]
    assert caps["supported"].count(runtime.CANONICAL_V2_CAPABILITY) == 1


def test_capability_canonical_v2_marker_is_gated_not_hardcoded(client, monkeypatch):
    # Prove the marker tracks real support, not a constant True: force the probe
    # False and it must disappear from both the flag and the ``supported`` list.
    monkeypatch.setattr(runtime, "_canonical_v2_supported", lambda: False)
    caps = client.get(
        "/api/runtime/v1/capabilities", headers=_identity_headers()
    ).json()
    assert caps["canonical_v2_supported"] is False
    assert runtime.CANONICAL_V2_CAPABILITY not in caps["supported"]


# --------------------------------------------------------------------------
# same key + same payload -> same run, one dispatch
# --------------------------------------------------------------------------


def test_same_key_same_payload_returns_same_run_one_dispatch(client):
    key = "idem-same-001"
    r1 = _create(client, idem_key=key)
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["run_id"]

    r2 = _create(client, idem_key=key)  # same key, fresh nonce, same payload
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == run_id, "idempotent retry must return the same run"

    # Exactly one durable row and exactly one create-time (dispatch) event.
    assert _row_count(client._db_path, tenant="tenantA", user="userA", key=key) == 1
    assert _mode_event_count(client, run_id) == 1


# --------------------------------------------------------------------------
# same key + changed payload -> 409 conflict (permanent)
# --------------------------------------------------------------------------


def test_same_key_changed_payload_is_409(client):
    key = "idem-conflict-001"
    r1 = _create(client, idem_key=key, task="compute A")
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["run_id"]

    r2 = _create(client, idem_key=key, task="compute B (different effect)")
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert detail["error"] == "idempotency_key_conflict"
    # Reconciliation: the conflict points back at the run the key already owns.
    assert detail["run_id"] == run_id

    # The conflict created NOTHING and overwrote nothing: still one row.
    assert _row_count(client._db_path, tenant="tenantA", user="userA", key=key) == 1


def test_cosmetic_change_is_not_a_conflict(client):
    # A different correlation id (a transport/cosmetic field) is NOT part of the
    # effect fingerprint, so a retry that only changes it must REPLAY, not 409.
    key = "idem-cosmetic-001"
    r1 = _create(client, idem_key=key, correlation="cid-first")
    assert r1.status_code == 200, r1.text
    r2 = _create(client, idem_key=key, correlation="cid-second")
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == r1.json()["run_id"]


# --------------------------------------------------------------------------
# concurrent same-key creates -> exactly one durable run
# --------------------------------------------------------------------------


def test_concurrent_same_key_creates_one_run(client):
    # Two GENUINELY concurrent creators, each on its OWN connection to the same
    # file — the real race the runtime create path runs. This exercises
    # ``create_task_ex``'s atomic insert-or-get (the exact call the HTTP handler
    # makes) directly at the store layer, avoiding TestClient's single-portal
    # serialisation so the durable-uniqueness guarantee is actually stressed. The
    # Master Integrator can lift this to two real OS processes; the store is
    # file-backed so the invariant holds cross-process too.
    db_path = client._db_path
    key = "idem-concurrent-001"
    fp = "same-effect-fp"
    barrier = threading.Barrier(2)
    results: list = []
    lock = threading.Lock()

    def _worker(_i):
        conn = kb.connect(board=runtime.RUNTIME_BOARD)
        try:
            barrier.wait(timeout=10)  # release both at once
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
    assert len(run_ids) == 1, f"concurrent same-key creates diverged: {run_ids}"
    # Exactly one creator inserted; the other observed the committed row.
    assert created_flags == [False, True], created_flags
    # Exactly one durable row survived the race (atomic insert-or-get).
    assert _row_count(db_path, tenant="tenantA", user="userA", key=key) == 1


# --------------------------------------------------------------------------
# persistence survives a fresh store handle / process restart
# --------------------------------------------------------------------------


def test_persistence_survives_store_reopen(client):
    key = "idem-restart-001"
    r1 = _create(client, idem_key=key)
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["run_id"]

    # The fingerprint is on DISK, read raw (durable, not in-memory).
    conn = sqlite3.connect(str(client._db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT idempotency_fingerprint FROM tasks WHERE id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["idempotency_fingerprint"], "fingerprint must be persisted durably"

    # Simulate a process restart: drop the schema-init cache and build a brand
    # new app/client over the SAME file. The re-create under the same key must
    # resolve to the SAME durable run.
    kb._INITIALIZED_PATHS.discard(str(Path(client._db_path).resolve()))
    app2 = _make_app()  # reuse the already-VERIFIED auth state; new app + nonce store
    with TestClient(app2) as c2:
        c2._db_path = client._db_path  # type: ignore[attr-defined]
        r2 = _create(c2, idem_key=key)
        assert r2.status_code == 200, r2.text
        assert r2.json()["run_id"] == run_id
        runtime.stop_dispatcher()

    assert _row_count(client._db_path, tenant="tenantA", user="userA", key=key) == 1


# --------------------------------------------------------------------------
# retention (CXR-05): completed / cancelled / archived run replays the original
# --------------------------------------------------------------------------


def test_archived_run_retention_replays_original(client):
    # An archived run projects as product "cancelled". Under the same key it must
    # STILL return the original run — a spent key never mints a duplicate.
    key = "idem-retain-archived-001"
    r1 = _create(client, idem_key=key)
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["run_id"]

    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        kb.archive_task(conn, run_id)

    r2 = _create(client, idem_key=key)
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == run_id, "archived key must replay the original run"
    # No duplicate: the original (now archived) row is the only one.
    assert _row_count(client._db_path, tenant="tenantA", user="userA", key=key) == 1


def test_completed_run_retention_replays_original(client):
    # A completed (done) run under the same key also replays the original.
    key = "idem-retain-done-001"
    r1 = _create(client, idem_key=key)
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["run_id"]

    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (int(time.time()), run_id),
            )

    r2 = _create(client, idem_key=key)
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == run_id
    assert _row_count(client._db_path, tenant="tenantA", user="userA", key=key) == 1


def test_archived_run_changed_payload_still_conflicts(client):
    # Retention + conflict together: a spent (archived) key reused with a DIFFERENT
    # effect must 409, never silently create a fresh run (permanent conflict).
    key = "idem-retain-conflict-001"
    r1 = _create(client, idem_key=key, task="original effect")
    assert r1.status_code == 200, r1.text
    run_id = r1.json()["run_id"]

    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        kb.archive_task(conn, run_id)

    r2 = _create(client, idem_key=key, task="a different effect entirely")
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"]["error"] == "idempotency_key_conflict"
    assert r2.json()["detail"]["run_id"] == run_id
    assert _row_count(client._db_path, tenant="tenantA", user="userA", key=key) == 1


# --------------------------------------------------------------------------
# CXR-06 fail closed: legacy / pre-column NULL fingerprint
# --------------------------------------------------------------------------


def test_legacy_null_fingerprint_row_fails_closed_over_http(client):
    """An existing keyed row with a NULL fingerprint (as a legacy/pre-column row
    looks after migration) must NOT be replayed and must NOT mint a duplicate: the
    HTTP create fails closed with a stable ``idempotency_legacy_unknown`` 409."""
    key = "idem-legacy-http-001"
    # Seed a legacy row DIRECTLY: keyed, owned by (tenantA,userA), fingerprint NULL
    # — byte-identical to what the additive migration leaves for a pre-column row.
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks (id, title, status, priority, created_by, "
                "created_at, workspace_kind, tenant, idempotency_key, "
                "idempotency_fingerprint) VALUES (?,?,?,?,?,?,?,?,?, NULL)",
                ("t_legacy_http", "legacy", "running", 0, "userA",
                 int(time.time()), "scratch", "tenantA", key),
            )

    r = _create(client, idem_key=key, task="anything")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "idempotency_legacy_unknown"
    assert detail["run_id"] == "t_legacy_http"
    # No new run was created — still exactly the one seeded legacy row.
    assert _row_count(client._db_path, tenant="tenantA", user="userA", key=key) == 1


def test_pre_column_db_upgrade_then_create_fails_closed(tmp_path):
    """Upgrade test: seed a genuine PRE-COLUMN database (a tasks table with no
    ``idempotency_fingerprint`` column + a real legacy keyed row), run the additive
    migration, and assert (a) the column is added, (b) the legacy row's value is
    NULL, and (c) a keyed ``create_task_ex`` under the same key fails closed with
    ``IdempotencyLegacyUnknown`` instead of assuming a match."""
    db_path = tmp_path / "legacy.db"
    key = "idem-legacy-pre-col-001"

    # 1. Build a PRE-COLUMN tasks table: the current schema with the fingerprint
    #    column DDL line removed (so the column genuinely does not exist).
    pre_schema = "\n".join(
        line for line in kb.SCHEMA_SQL.splitlines()
        if "idempotency_fingerprint TEXT" not in line
    )
    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    try:
        raw.executescript(pre_schema)
        raw.execute(
            "INSERT INTO tasks (id, title, status, priority, created_by, "
            "created_at, workspace_kind, tenant, idempotency_key) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("t_pre_col", "legacy", "running", 0, "userA",
             int(time.time()), "scratch", "tenantA", key),
        )
        raw.commit()
        cols_before = {r["name"] for r in raw.execute("PRAGMA table_info(tasks)")}
        assert "idempotency_fingerprint" not in cols_before, "column must be absent pre-upgrade"

        # 2. Run the REAL additive migration (the path that runs on opening a legacy DB).
        kb._migrate_add_optional_columns(raw)
        raw.commit()
        cols_after = {r["name"] for r in raw.execute("PRAGMA table_info(tasks)")}
        assert "idempotency_fingerprint" in cols_after, "migration must add the column"

        # 3. The legacy row's fingerprint is NULL (column existence != known payload).
        legacy_fp = raw.execute(
            "SELECT idempotency_fingerprint FROM tasks WHERE id = ?", ("t_pre_col",)
        ).fetchone()["idempotency_fingerprint"]
        assert legacy_fp is None
    finally:
        raw.close()

    # 4. A keyed create under the same key must FAIL CLOSED (never assume a match).
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
                idempotency_fingerprint="a-known-fingerprint",
                idempotency_conflict_on_mismatch=True,
                idempotency_include_archived=True,
            )
        assert ei.value.existing_id == "t_pre_col"
    finally:
        conn.close()

    # 5. No duplicate row was created — still exactly the one legacy row.
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
# isolation: cross-tenant / cross-user same key is enumeration-resistant
# --------------------------------------------------------------------------


def test_cross_tenant_same_key_is_enumeration_resistant(client):
    key = "shared-key-cross-tenant"
    ra = _create(client, idem_key=key, tenant="tenantA", user="userA",
                 correlation="cid-a-secret")
    assert ra.status_code == 200, ra.text
    run_a = ra.json()["run_id"]

    # Tenant B reuses A's key: gets its OWN new run, never A's (no suppression, no
    # disclosure, no false conflict across the owner boundary).
    rb = _create(client, idem_key=key, tenant="tenantB", user="userB",
                 correlation="cid-b-own")
    assert rb.status_code == 200, rb.text
    run_b = rb.json()["run_id"]
    assert run_b != run_a

    # B cannot even observe A's run existence.
    b_headers = _identity_headers(tenant="tenantB", user="userB", correlation="cid-b-own")
    assert client.get(
        f"/api/runtime/v1/runs/{run_a}", headers=b_headers
    ).status_code == 404


def test_cross_user_same_key_within_tenant_isolated(client):
    key = "shared-key-cross-user"
    ra = _create(client, idem_key=key, tenant="tenantA", user="userA")
    assert ra.status_code == 200, ra.text
    run_a = ra.json()["run_id"]

    rb = _create(client, idem_key=key, tenant="tenantA", user="userB")
    assert rb.status_code == 200, rb.text
    assert rb.json()["run_id"] != run_a

    # Same tenant + same user + same key still dedupes to the one run.
    ra2 = _create(client, idem_key=key, tenant="tenantA", user="userA")
    assert ra2.status_code == 200, ra2.text
    assert ra2.json()["run_id"] == run_a


def test_no_key_is_not_deduped(client):
    # Without an Idempotency-Key, two creates are two distinct runs (the contract
    # only engages when the caller opts in with a key).
    r1 = _create(client, idem_key=None)
    r2 = _create(client, idem_key=None)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["run_id"] != r2.json()["run_id"]
