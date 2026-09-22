"""Phase 3-A migration test: v1 (global idempotency) -> v2 (scoped) on a POPULATED DB.

Codex requirement: a fresh-database-only test is insufficient. This builds a v1
schema (no `operation` column, OLD global unique index on idempotency_key),
populates runs/events/results, then opens SqliteRunStore to trigger migration and
proves: existing data preserved, old global index replaced with the scoped index,
and scoped replay/conflict/cross-tenant/concurrent creation all work afterward.
"""

import sqlite3
import threading

import pytest

from youtab_runtime.durable_run_store import (
    DurableRunStore,
    IdempotencyConflict,
    RunIdentity,
)

_V1_RUNS = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
    organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, parent_task_id TEXT, delegation_id TEXT, idempotency_key TEXT,
    request_digest TEXT, state TEXT NOT NULL, progress_seq INTEGER NOT NULL DEFAULT 0,
    last_progress_at REAL, checkpoint_ref TEXT, result_ref TEXT, error_ref TEXT,
    execution_deadline REAL, lease_owner TEXT, lease_epoch INTEGER NOT NULL DEFAULT 0,
    lease_expiry REAL, heartbeat_at REAL, cancel_requested_at REAL, cancel_reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
)
"""
_V1_EVENTS = """
CREATE TABLE run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload TEXT, created_at REAL NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id), UNIQUE(run_id, seq)
)
"""


def _build_v1(db):
    conn = sqlite3.connect(db)
    conn.executescript(_V1_RUNS + ";" + _V1_EVENTS)
    conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO schema_meta VALUES('version','1')")
    # OLD global unique index (the defect we must migrate away from).
    conn.execute(
        "CREATE UNIQUE INDEX ux_runs_idempotency ON runs(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    # Populate: one keyed run with a result + events, one unkeyed run.
    conn.execute(
        "INSERT INTO runs(run_id,task_id,tenant_id,organization_id,workspace_id,principal_id,"
        "agent_id,idempotency_key,request_digest,state,progress_seq,last_progress_at,result_ref,"
        "created_at,updated_at) VALUES('old-1','t-1','tA','o1','w1','p1','a1','k','d1','RUNNING',"
        "2,1000.0,'r://old',1.0,2.0)"
    )
    conn.execute(
        "INSERT INTO runs(run_id,task_id,tenant_id,organization_id,workspace_id,principal_id,"
        "agent_id,state,created_at,updated_at) VALUES('old-2','t-2','tA','o1','w1','p1','a1',"
        "'SUCCEEDED',1.0,2.0)"
    )
    conn.execute("INSERT INTO run_events(run_id,seq,event_id,kind,payload,created_at) "
                 "VALUES('old-1',1,'e1','accepted','{}',1.0)")
    conn.execute("INSERT INTO run_events(run_id,seq,event_id,kind,payload,created_at) "
                 "VALUES('old-1',2,'e2','progress','{}',1.5)")
    conn.commit()
    conn.close()


def _index_sql(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_runs_idempotency'"
        ).fetchone()[0]
    finally:
        conn.close()


def _identity(**kw):
    base = dict(task_id="t", run_id="new-1", tenant_id="tA", organization_id="o1",
                workspace_id="w1", principal_id="p1", agent_id="a1")
    base.update(kw)
    return RunIdentity(**base)


def test_migration_preserves_data_and_replaces_index(tmp_path):
    db = tmp_path / "durable_runs.db"
    _build_v1(str(db))
    # Precondition: the OLD global index has no tenant scope.
    assert "tenant_id" not in _index_sql(str(db))

    # Opening the store runs the v1->v2 migration.
    s = DurableRunStore(db_path=db)

    # Existing rows preserved, with the new operation column defaulted.
    r1 = s.get_run("old-1")
    assert r1 is not None and r1["state"] == "RUNNING" and r1["result_ref"] == "r://old"
    assert r1["operation"] == "run" and r1["progress_seq"] == 2
    assert s.get_run("old-2")["state"] == "SUCCEEDED"
    # Existing events preserved.
    ev = s.get_events("old-1", from_seq=0)
    assert [e["seq"] for e in ev] == [1, 2]

    # Index replaced with the scoped one.
    idx = _index_sql(str(db))
    for col in ("tenant_id", "workspace_id", "principal_id", "operation", "idempotency_key"):
        assert col in idx


def test_scoped_semantics_after_migration(tmp_path):
    db = tmp_path / "durable_runs.db"
    _build_v1(str(db))
    s = DurableRunStore(db_path=db)

    # Same scope + same key + same digest as the migrated 'old-1' -> replay it.
    replay = s.create_run(_identity(run_id="dupe", idempotency_key="k", request_digest="d1"))
    assert replay["run_id"] == "old-1", "scoped replay must return the migrated run"

    # Same scope + same key + different digest -> fail closed.
    with pytest.raises(IdempotencyConflict):
        s.create_run(_identity(run_id="dupe2", idempotency_key="k", request_digest="dX"))

    # Same key, DIFFERENT tenant -> independent run (was impossible under the old
    # global index, which would have collided).
    other = s.create_run(_identity(run_id="tenantB", tenant_id="tB", idempotency_key="k",
                                   request_digest="dY"))
    assert other["run_id"] == "tenantB"


def test_concurrent_create_after_migration_single_row(tmp_path):
    db = tmp_path / "durable_runs.db"
    _build_v1(str(db))
    DurableRunStore(db_path=db)  # migrate first
    results = []

    def _create(i):
        cs = DurableRunStore(db_path=db)
        try:
            row = cs.create_run(_identity(run_id=f"c{i}", idempotency_key="fresh",
                                          request_digest="same"))
            results.append(row["run_id"])
        except Exception:  # noqa: BLE001
            pass

    ts = [threading.Thread(target=_create, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM runs WHERE idempotency_key='fresh'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1 and len(set(results)) == 1
