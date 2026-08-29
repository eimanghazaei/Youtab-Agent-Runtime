"""Engine-side tests for the correlation binding slice (CORRELATION_CONTRACT v1).

Covers the crypto lockstep (golden vectors), the fail-closed correlation
validation on signed mutations, the additive persisted column + migration,
worker-env dispatch stamping, and DTO surfacing. Pure/sqlite only — no
subprocess, no network, no POSIX assumptions (runs on Windows CI).
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli import runtime_command_auth as rca
from youtab_agent_cli.web_routers import runtime

# --- FROZEN golden vectors (CORRELATION_CONTRACT section 2) -----------------
G_METHOD = "POST"
G_PATH = "/api/runtime/v1/runs"
G_TENANT = "tnt_demo"
G_USER = "usr_demo"
G_TS = "1735689600"
G_NONCE = "n-000000000000000000000000000000ff"
G_BODY = b'{"agent":"a","task":"t"}'
G_CORR = "cid-0123456789abcdef0123456789abcdef"
G_SECRET = "test-secret-000000000000000000000000000000000"  # 45 chars (>=43)

G_CANONICAL = (
    "POST\n/api/runtime/v1/runs\ntnt_demo\nusr_demo\n1735689600\n"
    "n-000000000000000000000000000000ff\n"
    "446fe7bdcf0a94bcd256b7249603dab3f9440f9bc84834935dd811d3602eccf0\n"
    "cid-0123456789abcdef0123456789abcdef"
)
G_SIG_V1 = "0c1cea5a67c1871a7b4c21a6c0be1b1de5b1738f0eedb5cd834699bbf3a41fb1"
G_SIG_V0 = "76f27590c45479d7ed3eba0240a974a1536b8dac2a0c3785c5f1a1da4a6eaf7f"


# --------------------------------------------------------------------------
# crypto lockstep
# --------------------------------------------------------------------------


def test_golden_canonical():
    canonical = rca.canonical_string(
        method=G_METHOD, path=G_PATH, tenant=G_TENANT, user=G_USER,
        timestamp=G_TS, nonce=G_NONCE, body=G_BODY, correlation=G_CORR,
    )
    assert canonical == G_CANONICAL
    assert rca.compute_signature(G_SECRET, canonical) == G_SIG_V1


def _headers(sig, *, ts=G_TS, nonce=G_NONCE, correlation=G_CORR):
    h = {
        rca.SIGNATURE_HEADER: sig,
        rca.TIMESTAMP_HEADER: str(ts),
        rca.NONCE_HEADER: nonce,
    }
    if correlation is not None:
        h[rca.CORRELATION_HEADER] = correlation
    return h


def _verify(headers, *, store, now=int(G_TS), nonce_body=G_BODY):
    rca.verify_command(
        method=G_METHOD, path=G_PATH, tenant=G_TENANT, user=G_USER,
        body=nonce_body, headers=headers, secret=G_SECRET, store=store, now=now,
    )


def test_v1_signature_accepted():
    # Acceptance == no CommandAuthError raised. (We do not assert store.seen
    # here: the golden timestamp is fixed in the past, and the in-memory store
    # prunes by real wall-clock on record, which would evict it. Nonce
    # RECORDING under a current timestamp is covered in test_runtime_command_auth.)
    store = rca._MemoryNonceStore()
    _verify(_headers(G_SIG_V1), store=store)


def test_old_seven_field_signature_rejected():
    """The pre-change 7-field sig must fail now that correlation is covered."""
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError) as ei:
        _verify(_headers(G_SIG_V0), store=store)
    assert ei.value.code == "bad_signature"
    assert ei.value.http_status == 401
    # signature failure must NOT burn the nonce
    assert store.seen(G_NONCE) is False


def test_tampered_correlation_is_bad_signature():
    store = rca._MemoryNonceStore()
    tampered = _headers(G_SIG_V1, correlation="cid-ffffffffffffffffffffffffffffffff")
    with pytest.raises(rca.CommandAuthError) as ei:
        _verify(tampered, store=store)
    assert ei.value.code == "bad_signature"
    assert store.seen(G_NONCE) is False


def test_missing_correlation_on_signed_mutation():
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError) as ei:
        _verify(_headers(G_SIG_V1, correlation=None), store=store)
    assert ei.value.code == "missing_correlation"
    assert ei.value.http_status == 401
    assert store.seen(G_NONCE) is False


def test_malformed_correlation_on_signed_mutation():
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError) as ei:
        _verify(_headers(G_SIG_V1, correlation="bad id!"), store=store)
    assert ei.value.code == "invalid_correlation"
    assert ei.value.http_status == 400
    assert store.seen(G_NONCE) is False


def test_short_correlation_rejected():
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError) as ei:
        _verify(_headers(G_SIG_V1, correlation="short"), store=store)
    assert ei.value.code == "invalid_correlation"


def test_nonce_not_burned_by_bad_probe_then_valid_succeeds():
    """A bad-signature probe must not consume a legitimate nonce."""
    store = rca._MemoryNonceStore()
    with pytest.raises(rca.CommandAuthError):
        _verify(_headers(G_SIG_V0), store=store)  # bad sig, same nonce
    assert store.seen(G_NONCE) is False
    # The genuine command with the SAME nonce still works: it would raise
    # ``replayed`` had the bad probe burned the nonce. No exception == not burned.
    _verify(_headers(G_SIG_V1), store=store)


# --------------------------------------------------------------------------
# persisted column + migration
# --------------------------------------------------------------------------


def test_fresh_db_has_correlation_column(tmp_path):
    conn = kb.connect(db_path=tmp_path / "fresh.db")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "correlation_id" in cols
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(tasks)")}
    assert "idx_tasks_correlation_id" in idx
    conn.close()


def test_legacy_db_migration_adds_correlation_column(tmp_path):
    """A legacy tasks table lacking the column gets it added, idempotently."""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    # Minimal legacy-shaped tasks table WITHOUT correlation_id.
    conn.execute(
        "CREATE TABLE tasks ("
        " id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,"
        " status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER,"
        " started_at INTEGER, completed_at INTEGER, workspace_kind TEXT,"
        " workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER,"
        " session_id TEXT)"
    )
    # The migration also touches task_events; give it a minimal legacy table so
    # the run only exercises the tasks.correlation_id addition path.
    conn.execute(
        "CREATE TABLE task_events ("
        " id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT,"
        " created_at INTEGER)"
    )
    conn.commit()
    cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "correlation_id" not in cols_before

    kb._migrate_add_optional_columns(conn)
    cols_after = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "correlation_id" in cols_after

    # idempotent: a second run is a no-op and does not raise
    kb._migrate_add_optional_columns(conn)
    conn.close()


def test_task_round_trips_correlation_id(tmp_path):
    conn = kb.connect(db_path=tmp_path / "rt.db")
    task_id = kb.create_task(
        conn,
        title="t",
        body="do it",
        assignee="default",
        created_by="userA",
        tenant="tenantA",
        correlation_id="cid-roundtrip-abcdef012345",
        session_id="cid-roundtrip-abcdef012345",
    )
    conn.commit()
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.correlation_id == "cid-roundtrip-abcdef012345"
    # legacy overload still written unchanged
    assert task.session_id == "cid-roundtrip-abcdef012345"
    conn.close()


# --------------------------------------------------------------------------
# dispatch stamping (worker env assembly, pure)
# --------------------------------------------------------------------------


def test_apply_correlation_env_sets_var_when_present():
    task = SimpleNamespace(correlation_id="cid-dispatch-0011223344")
    env: dict[str, str] = {}
    kb._apply_correlation_env(env, task)
    assert env["YOUTAB_AGENT_CORRELATION_ID"] == "cid-dispatch-0011223344"


def test_apply_correlation_env_noop_when_absent():
    task = SimpleNamespace(correlation_id=None)
    env: dict[str, str] = {}
    kb._apply_correlation_env(env, task)
    assert "YOUTAB_AGENT_CORRELATION_ID" not in env


# --------------------------------------------------------------------------
# DTO surfacing
# --------------------------------------------------------------------------


def _make_task(tmp_path, correlation):
    conn = kb.connect(db_path=tmp_path / "dto.db")
    task_id = kb.create_task(
        conn, title="t", body="b", assignee="default", created_by="userA",
        tenant="tenantA", correlation_id=correlation, session_id=correlation,
    )
    conn.commit()
    task = kb.get_task(conn, task_id)
    conn.close()
    return task


def test_run_summary_surfaces_correlation(tmp_path):
    task = _make_task(tmp_path, "cid-summary-99887766")
    summary = runtime._run_summary(task)
    assert summary["correlation_id"] == "cid-summary-99887766"


def test_event_projection_surfaces_correlation():
    ev = SimpleNamespace(
        id=7,
        kind="runtime_execution_mode",
        payload={"mode": "model", "correlation_id": "cid-evt-55443322"},
        created_at=123,
    )
    proj = runtime._event_projection("run-1", ev)
    assert proj["correlation_id"] == "cid-evt-55443322"


def test_event_projection_correlation_none_when_absent():
    ev = SimpleNamespace(id=8, kind="worker_started", payload={"pid": 1}, created_at=1)
    proj = runtime._event_projection("run-1", ev)
    assert proj["correlation_id"] is None
