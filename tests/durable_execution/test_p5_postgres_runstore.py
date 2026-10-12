"""P5 — real PostgreSQL RunStore backend, tested on a real PostgreSQL instance.

Each test runs against an ISOLATED disposable database. Same authority/interface
as the SQLite backend (no second ledger/id-space/receipt). If no PostgreSQL is
reachable the whole module is skipped as ENV-UNAVAILABLE (not a pass).
"""

import os
import threading
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from youtab_runtime.durable_run_store import IdempotencyConflict, InvalidTransition, RunIdentity, RunState  # noqa: E402
from youtab_runtime.durable_run_store_pg import PostgresRunStore  # noqa: E402

_ADMIN = os.environ.get("YOUTAB_TEST_PG_DSN",
                        "postgresql://youtab:devpass@127.0.0.1:55432/durable")


def _pg_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_reachable(),
                                reason="no reachable PostgreSQL (ENV-UNAVAILABLE)")


@pytest.fixture
def pg_dsn():
    dbname = "de_" + uuid.uuid4().hex[:12]
    with psycopg.connect(_ADMIN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    base = _ADMIN.rsplit("/", 1)[0]
    dsn = f"{base}/{dbname}"
    try:
        yield dsn
    finally:
        with psycopg.connect(_ADMIN, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (dbname,))
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


def _ident(run_id="r", **kw):
    base = dict(task_id=run_id, run_id=run_id, tenant_id="t", organization_id="o",
                workspace_id="w", principal_id="p", agent_id="a", operation="run")
    base.update(kw)
    return RunIdentity(**base)


def test_fail_closed_without_dsn():
    with pytest.raises(Exception):
        PostgresRunStore(None)


def test_scoped_idempotency_and_conflict(pg_dsn):
    s = PostgresRunStore(pg_dsn)
    a = s.create_run(_ident("a", idempotency_key="K", request_digest="D1"))
    b = s.create_run(_ident("b", idempotency_key="K", request_digest="D1"))
    assert a["run_id"] == b["run_id"] == "a"
    with pytest.raises(IdempotencyConflict):
        s.create_run(_ident("c", idempotency_key="K", request_digest="D2"))
    # different tenant -> independent
    d = s.create_run(_ident("d", tenant_id="t2", idempotency_key="K", request_digest="DX"))
    assert d["run_id"] == "d"


def test_concurrent_claim_single_winner_and_fencing(pg_dsn):
    s = PostgresRunStore(pg_dsn)
    s.create_run(_ident("cc"))
    winners = []
    barrier = threading.Barrier(8)

    def _c(i):
        cs = PostgresRunStore(pg_dsn)
        barrier.wait()
        e = cs.claim("cc", owner=f"pid:{1000 + i}")
        if e is not None:
            winners.append(e)

    ts = [threading.Thread(target=_c, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join(15) for t in ts]
    assert len(winners) == 1, f"exactly one claimer wins on real PG; got {winners}"
    # Stale epoch cannot heartbeat (fencing).
    assert s.heartbeat("cc", "pid:1000", winners[0] - 1) is False


def test_terminal_immutability(pg_dsn):
    s = PostgresRunStore(pg_dsn)
    s.create_run(_ident("t1"))
    s.claim("t1", owner="pid:1")
    s.transition("t1", RunState.RUNNING)
    s.transition("t1", RunState.SUCCEEDED, result_ref="r://1")
    with pytest.raises(InvalidTransition):
        s.transition("t1", RunState.RUNNING)


def test_event_sequence_and_reconnect(pg_dsn):
    s = PostgresRunStore(pg_dsn)
    s.create_run(_ident("e1"))
    s.claim("e1", owner="pid:1")
    s.transition("e1", RunState.RUNNING)
    for i in range(5):
        s.record_progress("e1", step=f"s{i}")
    ev = s.get_events("e1", from_seq=0)
    seqs = [e["seq"] for e in ev]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    mid = seqs[3]
    tail = s.get_events("e1", from_seq=mid)
    assert [e["seq"] for e in tail] == [x for x in seqs if x > mid]


def test_restart_recovery_same_db(pg_dsn):
    s1 = PostgresRunStore(pg_dsn)
    s1.admit(_ident("rr", execution_deadline=None), owner="pid:1")
    s1.record_progress("rr", step="half", checkpoint_ref="ckpt://1")
    s1.transition("rr", RunState.SUCCEEDED, result_ref="r://done")
    del s1
    s2 = PostgresRunStore(pg_dsn)  # "restart" — fresh store, same database
    row = s2.get_run("rr")
    assert row is not None and row["state"] == "SUCCEEDED" and row["result_ref"] == "r://done"
    assert row["checkpoint_ref"] == "ckpt://1"
    assert sum(1 for e in s2.get_events("rr", from_seq=0) if e["kind"] == "state.succeeded") == 1


def test_dead_owner_reconcile_and_healthy_queued_untouched(pg_dsn):
    s = PostgresRunStore(pg_dsn)
    s.create_run(_ident("healthy"))               # ownerless QUEUED (healthy)
    s.admit(_ident("dead"), owner="pid:999999")   # owner-stamped RUNNING
    reconciled = s.reconcile_dead_owner(lambda pid: False)
    assert "dead" in reconciled and "healthy" not in reconciled
    assert s.get_run("dead")["state"] == "UNKNOWN"
    assert s.get_run("healthy")["state"] == "QUEUED"


def test_cancel_and_deadline_enforcement(pg_dsn):
    import time
    s = PostgresRunStore(pg_dsn)
    s.admit(_ident("cx"), owner="pid:1")
    row = s.request_cancel("cx", reason="user", by="principal:p")
    assert row["state"] == "CANCELLING" and row["cancel_requested_at"] is not None
    # deadline enforcement (active)
    s.admit(_ident("dl", execution_deadline=time.time() - 1), owner="pid:1")
    enforced = s.enforce_execution_deadlines()
    assert "dl" in enforced
    assert s.get_run("dl")["state"] == "CANCELLING"
    assert s.is_stop_requested("dl") is True


def _build_v1(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("""CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, principal_id TEXT NOT NULL,
            agent_id TEXT NOT NULL, parent_task_id TEXT, delegation_id TEXT, idempotency_key TEXT,
            request_digest TEXT, state TEXT NOT NULL, progress_seq BIGINT NOT NULL DEFAULT 0,
            last_progress_at DOUBLE PRECISION, checkpoint_ref TEXT, result_ref TEXT, error_ref TEXT,
            execution_deadline DOUBLE PRECISION, lease_owner TEXT, lease_epoch BIGINT NOT NULL DEFAULT 0,
            lease_expiry DOUBLE PRECISION, heartbeat_at DOUBLE PRECISION, cancel_requested_at DOUBLE PRECISION,
            cancel_reason TEXT, attempts BIGINT NOT NULL DEFAULT 0, created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL)""")
        conn.execute("CREATE UNIQUE INDEX ux_runs_idempotency ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL")
        conn.execute("""CREATE TABLE run_events (id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
            seq BIGINT NOT NULL, event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload JSONB,
            created_at DOUBLE PRECISION NOT NULL, UNIQUE(run_id, seq))""")
        conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO schema_meta VALUES('version','1')")
        conn.execute("INSERT INTO runs(run_id,task_id,tenant_id,organization_id,workspace_id,principal_id,"
                     "agent_id,idempotency_key,request_digest,state,result_ref,created_at,updated_at) "
                     "VALUES('old-1','t','tA','o','w','p','a','K','D1','RUNNING','r://old',1,2)")


def test_migration_on_populated_v1_db(pg_dsn):
    _build_v1(pg_dsn)
    # Opening the store runs the v1->v2 migration on the populated DB.
    s = PostgresRunStore(pg_dsn)
    row = s.get_run("old-1")
    assert row is not None and row["state"] == "RUNNING" and row["result_ref"] == "r://old"
    assert row["operation"] == "run"
    with psycopg.connect(pg_dsn) as conn:
        idxdef = conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname='ux_runs_idempotency'").fetchone()[0]
    for col in ("tenant_id", "workspace_id", "principal_id", "operation", "idempotency_key"):
        assert col in idxdef
    # scoped semantics after migration
    replay = s.create_run(_ident("dupe", tenant_id="tA", idempotency_key="K", request_digest="D1"))
    assert replay["run_id"] == "old-1"
    with pytest.raises(IdempotencyConflict):
        s.create_run(_ident("x", tenant_id="tA", idempotency_key="K", request_digest="D2"))
    other = s.create_run(_ident("tb", tenant_id="tB", idempotency_key="K", request_digest="DY"))
    assert other["run_id"] == "tb"
