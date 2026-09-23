"""Exact-SHA tests for the D1 single-authority admission gate
(``youtab_runtime.durable_ingress_process``) and the caller-pinned kanban id that
makes the kanban row a same-id execution-transport projection of the one durable
run.

These prove the durable RunStore is the SOLE idempotency/lifecycle authority at
the product ingress: concurrent duplicate -> one run, mismatched digest -> 409
signal, dedup returns the ORIGINAL run id, the id survives kill/restart, a
competing instance is refused, and authority loss fails closed. The kanban row
shares the durable run id (no second id, no second dedup). The create-to-worker
dispatch and the worker->durable approval projection are covered elsewhere / held.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

import youtab_runtime.durable_ingress_process as dip
from youtab_runtime.durable_ingress import AuthorityLost, IdempotencyConflict
from youtab_runtime.durable_run_authority import AuthorityHeld
from youtab_runtime.durable_run_store import RunState

_IDENT = dict(
    tenant_id="t1",
    organization_id="o1",
    workspace_id="w1",
    principal_id="t1:u1",
    agent_id="agent-x",
)


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """Enable durable ingress against an isolated sqlite store; reset the process
    singleton before and after so no test leaks the authority (or its file lock)."""
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_INGRESS", "1")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "sqlite")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_DB_PATH", str(tmp_path / "ingress.db"))
    dip.reset_for_tests()
    try:
        yield tmp_path
    finally:
        dip.reset_for_tests()


# ------------------------------------------------------------------ disabled -- #
def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_INGRESS", raising=False)
    dip.reset_for_tests()
    assert dip.ingress_enabled() is False
    assert dip.get_ingress_authority() is None
    assert dip.ingress_ready() is False
    with pytest.raises(AuthorityLost):
        dip.admit_managed_run(run_id="t_x", **_IDENT)


# ------------------------------------------------------------------ positive -- #
def test_admit_owner_stamped_and_ready(enabled):
    dip.acquire_ingress_authority()
    assert dip.ingress_ready() is True
    res = dip.admit_managed_run(run_id="t_run1", idempotency_key="k1",
                                request_digest="d1", **_IDENT)
    assert res.created is True
    assert res.row["run_id"] == "t_run1"          # the caller's canonical id is used
    assert res.row["state"] == RunState.QUEUED.value
    assert res.row["lease_owner"]                  # owner-stamped -> recoverable


def test_dedup_returns_original_run_id(enabled):
    dip.acquire_ingress_authority()
    first = dip.admit_managed_run(run_id="t_a", idempotency_key="k1",
                                  request_digest="d1", **_IDENT)
    # a replay with the SAME key but a fresh generated id returns the ORIGINAL run
    dup = dip.admit_managed_run(run_id="t_b", idempotency_key="k1",
                                request_digest="d1", **_IDENT)
    assert dup.created is False
    assert dup.row["run_id"] == first.row["run_id"] == "t_a"


def test_mismatched_digest_conflicts(enabled):
    dip.acquire_ingress_authority()
    dip.admit_managed_run(run_id="t_a", idempotency_key="k1",
                          request_digest="dA", **_IDENT)
    with pytest.raises(IdempotencyConflict):
        dip.admit_managed_run(run_id="t_b", idempotency_key="k1",
                              request_digest="dB", **_IDENT)


def test_concurrent_duplicate_single_run(enabled):
    """Two concurrent admits of the same key must yield exactly ONE created run;
    the loser observes the winner's original id (no second worker effect)."""
    dip.acquire_ingress_authority()
    barrier = threading.Barrier(2)
    out: list = []

    def worker(rid: str) -> None:
        barrier.wait()
        try:
            r = dip.admit_managed_run(run_id=rid, idempotency_key="k1",
                                      request_digest="d1", **_IDENT)
            out.append((r.created, r.row["run_id"]))
        except Exception as exc:  # surface anything unexpected
            out.append(("err", type(exc).__name__))

    ts = [threading.Thread(target=worker, args=(f"t_{i}",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    created = [o for o in out if o[0] is True]
    deduped = [o for o in out if o[0] is False]
    assert len(created) == 1, out
    assert len(deduped) == 1, out
    assert created[0][1] == deduped[0][1]  # both resolve to the one run id


# ------------------------------------------------------------- restart/rival -- #
def test_dedup_and_id_survive_restart(enabled):
    dip.acquire_ingress_authority()
    first = dip.admit_managed_run(run_id="t_keep", idempotency_key="k1",
                                  request_digest="d1", **_IDENT)
    dip.release_ingress_authority()  # simulate instance handoff

    dip.reset_for_tests()  # fresh process singleton, same store file
    dip.acquire_ingress_authority()
    dup = dip.admit_managed_run(run_id="t_other", idempotency_key="k1",
                                request_digest="d1", **_IDENT)
    assert dup.created is False
    assert dup.row["run_id"] == first.row["run_id"] == "t_keep"


def test_competing_instance_refused(enabled, tmp_path, monkeypatch):
    dip.acquire_ingress_authority()
    # a second, independent authority on the SAME store file must fail to acquire
    from youtab_runtime.durable_ingress import DurableRunStateAuthority
    rival = DurableRunStateAuthority(backend="sqlite",
                                     db_path=tmp_path / "ingress.db")
    with pytest.raises(AuthorityHeld):
        rival.acquire()


# --------------------------------------------------------------- fail-stop --- #
def test_loss_latches_fail_closed(enabled):
    hits: list = []
    dip.acquire_ingress_authority(on_lost=lambda r: hits.append(r))
    # simulate a loss signal reaching the module (as the store's loss listener would)
    dip._on_lost("advisory lock lost")
    assert hits == ["advisory lock lost"]
    assert dip.ingress_ready() is False
    with pytest.raises(AuthorityLost):
        dip.admit_managed_run(run_id="t_z", idempotency_key="k9",
                              request_digest="d1", **_IDENT)


# ------------------------------------------------------------ request digest -- #
def test_request_digest_is_stable_and_order_independent():
    a = dip.request_digest_for({"agent": "x", "task": "hi", "goal_mode": True})
    b = dip.request_digest_for({"goal_mode": True, "task": "hi", "agent": "x"})
    c = dip.request_digest_for({"agent": "x", "task": "bye", "goal_mode": True})
    assert a == b        # key order does not change the digest
    assert a != c        # a different body changes it
    assert a.startswith("sha256:")


def test_request_digest_drops_none():
    a = dip.request_digest_for({"agent": "x", "engine": None})
    b = dip.request_digest_for({"agent": "x"})
    assert a == b        # an omitted optional == explicit null


# ----------------------------------------------- kanban same-id projection --- #
def _mk_kanban_db(tmp_path):
    from pathlib import Path

    from youtab_agent_cli import kanban_db as kb
    conn = kb.connect(Path(tmp_path) / "kanban.db")  # auto-inits schema
    return kb, conn


def test_create_task_ex_uses_caller_pinned_id(tmp_path):
    kb, conn = _mk_kanban_db(tmp_path)
    try:
        tid, created = kb.create_task_ex(conn, title="t", task_id="t_pinned123")
        assert created is True
        assert tid == "t_pinned123"                 # verbatim, not server-generated
        assert kb.get_task(conn, "t_pinned123") is not None
    finally:
        conn.close()


def test_create_task_ex_pinned_id_collision_raises(tmp_path):
    kb, conn = _mk_kanban_db(tmp_path)
    try:
        kb.create_task_ex(conn, title="a", task_id="t_dup")
        with pytest.raises(sqlite3.IntegrityError):
            # same pinned id again -> real PK conflict, no silent id reroll
            kb.create_task_ex(conn, title="b", task_id="t_dup")
    finally:
        conn.close()


def test_create_task_ex_default_id_still_generated(tmp_path):
    kb, conn = _mk_kanban_db(tmp_path)
    try:
        tid, created = kb.create_task_ex(conn, title="t")
        assert created is True
        assert tid.startswith("t_")                 # server-generated as before
    finally:
        conn.close()


# ── per-run worker capability + launcher carry (D2 producer boundary auth) ────
# A strong (>=43 char) service secret so the derived worker-cap key is available.
_STRONG_SECRET = "test-only-strong-runtime-service-secret-000000000"


def test_worker_capability_binds_scope_epoch_and_revokes(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_INGRESS", "1")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "sqlite")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", _STRONG_SECRET)
    dip.reset_for_tests()
    dip.acquire_ingress_authority()
    try:
        rid = dip.admit_managed_run(run_id="t_r1", idempotency_key="k1",
                                    request_digest="d1", **_IDENT).row["run_id"]
        cap = dip.mint_worker_capability(rid)
        assert cap and dip.verify_worker_capability(rid, cap) is True
        ok, reason = dip.authorize_worker_capability(rid, cap)
        assert ok is True and reason == "ok"
        # cross-run: a cap minted for another run is refused
        assert dip.verify_worker_capability("t_other", cap) is False
        assert dip.authorize_worker_capability("t_other", cap)[0] is False
        # arbitrary / service-secret / empty caps are rejected
        assert dip.authorize_worker_capability(rid, "nope")[1] == "unauthorized_worker"
        assert dip.authorize_worker_capability(rid, _STRONG_SECRET)[1] == "unauthorized_worker"
        assert dip.authorize_worker_capability(rid, None)[1] == "unauthorized_worker"
        # revocation: a cancelled/terminal run refuses the (still-signed) cap
        dip.get_ingress_authority().store.set_state(rid, RunState.CANCELLED, strict=False)
        assert dip.authorize_worker_capability(rid, cap) == (False, "run_not_active")
    finally:
        dip.reset_for_tests()


def test_worker_capability_revoked_by_epoch_takeover(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_INGRESS", "1")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "sqlite")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", _STRONG_SECRET)
    dip.reset_for_tests()
    dip.acquire_ingress_authority()
    rid = dip.admit_managed_run(run_id="t_r1", idempotency_key="k1",
                                request_digest="d1", **_IDENT).row["run_id"]
    cap = dip.mint_worker_capability(rid)
    dip.reset_for_tests()                 # takeover: a new instance, new epoch
    dip.acquire_ingress_authority()
    try:
        # the old cap embeds the prior epoch -> refused after the takeover (revoked);
        # the run is also reconciled to UNKNOWN, itself an inactive state.
        assert dip.authorize_worker_capability(rid, cap)[0] is False
    finally:
        dip.reset_for_tests()


def test_worker_capability_none_without_secret_or_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_INGRESS", "1")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "sqlite")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.delenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", raising=False)
    dip.reset_for_tests()
    dip.acquire_ingress_authority()
    try:
        dip.admit_managed_run(run_id="t_r1", idempotency_key="k1",
                              request_digest="d1", **_IDENT)
        assert dip.mint_worker_capability("t_r1") is None          # no secret
    finally:
        dip.reset_for_tests()
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", _STRONG_SECRET)
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_INGRESS", raising=False)
    assert dip.mint_worker_capability("t_r1") is None              # flag off


def test_build_worker_invocation_carries_capability(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_INGRESS", "1")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND", "sqlite")
    monkeypatch.setenv("YOUTAB_AGENT_DURABLE_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", _STRONG_SECRET)
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SELF_URL", "http://127.0.0.1:9/")
    from pathlib import Path

    from youtab_agent_cli import kanban_db as kb
    dip.reset_for_tests()
    dip.acquire_ingress_authority()
    try:
        # the durable run must be admitted before the launcher can mint its cap
        dip.admit_managed_run(run_id="t_launch", idempotency_key="k1",
                              request_digest="d1", **_IDENT)
        conn = kb.connect(Path(tmp_path) / "kanban.db")
        try:
            kb.create_task_ex(conn, title="t", assignee="default", created_by="u",
                              tenant="t1", task_id="t_launch")
            task = kb.get_task(conn, "t_launch")
        finally:
            conn.close()
        env, _cmd = kb.build_worker_invocation(task, str(tmp_path / "ws"))
        assert dip.authorize_worker_capability(
            "t_launch", env.get("YOUTAB_AGENT_RUNTIME_WORKER_CAP"))[0] is True
        assert env.get("YOUTAB_AGENT_RUNTIME_INGRESS_URL") == "http://127.0.0.1:9"
    finally:
        dip.reset_for_tests()


def test_build_worker_invocation_no_capability_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTAB_AGENT_DURABLE_INGRESS", raising=False)
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", "off-strong-secret")
    from pathlib import Path

    from youtab_agent_cli import kanban_db as kb
    conn = kb.connect(Path(tmp_path) / "kanban.db")
    try:
        tid = kb.create_task(conn, title="t", assignee="default",
                             created_by="u", tenant="t1")
        task = kb.get_task(conn, tid)
    finally:
        conn.close()
    env, _cmd = kb.build_worker_invocation(task, str(tmp_path / "ws"))
    assert "YOUTAB_AGENT_RUNTIME_WORKER_CAP" not in env  # no-op when durable is off
