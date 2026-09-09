"""WAVE-30H task 2a/3 — the pre-warmed single-use worker pool, proven in the REAL
managed dispatch path and at the pool-management level.

Part A (integration): a real create_run → dispatcher → WorkerPool.spawn → warm
worker re-admits the persisted Simorgh grant and completes the run. Proves the
pool is wired into the genuine execution path with per-run tenant/grant/memory
binding, one-run-per-worker, and a REAL pid for crash detection.

Part B (mechanics): bounded size, backpressure, stale-worker rejection, crash
recovery, zero-survivor teardown, per-run rebind isolation — on the WorkerPool
API directly with a hermetic deterministic runner.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from youtab_agent_cli.worker_pool import WorkerPool, _pid_alive
from youtab_runtime import managed_execution as mx
from youtab_runtime.contracts import BrainCommandEnvelopeV2

SECRET = secrets.token_urlsafe(48)
KEY_ID = "brain-pool"
_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes([71]) * 32)
_PUB = base64.b64encode(_SIGNER.public_key().public_bytes_raw()).decode()
_KEYS = json.dumps({KEY_ID: _PUB})
TENANT, USER = "tpool", "upool"


# ── Part A — integration through the real managed dispatch path ───────────────


@pytest.fixture()
def managed_pool(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("YOUTAB_RUNTIME_TRUST_MODE", "managed")
    monkeypatch.setenv("YOUTAB_BRAIN_PUBLIC_KEYS", _KEYS)

    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider
    import youtab_agent_cli.profiles as _profiles
    _orig = _profiles.list_profiles

    class _P:
        name = "default"; description = "t"; model = "m"; provider = "p"
        skill_count = 1; is_default = True
    _profiles.list_profiles = lambda: [_P()]
    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service", capability="runtime")

    pool = WorkerPool(size=2, runner="deterministic", warm_full=False,
                      base_dir=str(tmp_path / "pool"))
    runtime._spawn_override = pool.spawn
    runtime._nonce_store = None

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)
    token_auth.require_route_ownership(provider="runtime-service", path="/api/runtime/v1/",
                                       is_prefix=True, capability="runtime")
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()
    try:
        with TestClient(app) as c:
            yield c, pool
    finally:
        runtime.stop_dispatcher()
        proof = pool.close()
        runtime._spawn_override = None
        runtime._nonce_store = None
        _profiles.list_profiles = _orig
        assert proof["zero_survivors"], proof


def _mint(**over):
    now = over.pop("now", None) or datetime.now(UTC).replace(microsecond=0)
    f = dict(schema_version="youtab.agent-command.v2", issuer="youtab-one-brain",
             audience="youtab-agent-runtime", protocol_version="youtab.runtime-sig.v2",
             command_id=f"cmd-{uuid.uuid4().hex[:10]}", task_id=f"task-{uuid.uuid4().hex[:10]}",
             root_run_id=f"run-{uuid.uuid4().hex[:10]}", parent_task_id=None, attempt=1,
             tenant_id=TENANT, workspace_id="-", user_id=USER, membership_generation=1,
             authorization_epoch=1, agent_id="agent-default", engine_id="engine-local",
             trace_id=f"tr-{uuid.uuid4().hex[:8]}", nonce=f"grant-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
             objective="pool run", allowed_toolsets=("*",), allowed_memory_scopes=("*",),
             allowed_artifact_scopes=(), effect_proposal_scopes=(),
             reasoning={"max_iterations": 3, "max_spawn_depth": 1, "max_concurrent_agents": 1,
                        "max_total_tokens": 1000, "max_cost_micros": 0, "max_retries": 0,
                        "deadline_at": now + timedelta(minutes=20)},
             issued_at=now, expires_at=now + timedelta(minutes=30), key_id=KEY_ID, signature="0" * 88)
    f.update(over)
    env = BrainCommandEnvelopeV2(**f)
    sig = base64.b64encode(_SIGNER.sign(env.canonical_payload())).decode()
    g = env.model_dump(mode="json"); g["signature"] = sig
    return base64.b64encode(json.dumps(g, separators=(",", ":")).encode()).decode()


def _create(client, tenant=TENANT, user=USER):
    body = json.dumps({"agent": "default", "task": "pooled run"}).encode()
    path = "/api/runtime/v1/runs"
    corr = f"cid-{uuid.uuid4().hex[:8]}"
    ts = int(time.time()); nonce = f"n-{uuid.uuid4().hex}"
    canon = rca.canonical_string(method="POST", path=path, tenant=tenant, user=user,
                                 timestamp=str(ts), nonce=nonce, body=body, correlation=corr)
    h = {"Authorization": f"Bearer {SECRET}", "X-Youtab-Tenant-Id": tenant, "X-Youtab-User-Id": user,
         "X-Youtab-Roles": "member", "X-Youtab-Correlation-Id": corr, "Content-Type": "application/json",
         rca.SIGNATURE_HEADER: rca.compute_signature(SECRET, canon),
         rca.TIMESTAMP_HEADER: str(ts), rca.NONCE_HEADER: nonce,
         mx.GRANT_HEADER: _mint(tenant_id=tenant, user_id=user)}
    return client.post(path, content=body, headers=h)


def _events(client, run_id, tenant=TENANT, user=USER):
    return client.get(f"/api/runtime/v1/runs/{run_id}/events?after=0",
                      headers={"Authorization": f"Bearer {SECRET}", "X-Youtab-Tenant-Id": tenant,
                               "X-Youtab-User-Id": user, "X-Youtab-Roles": "member",
                               "X-Youtab-Correlation-Id": "cid-r"}).json()


def _run_to_completion(client, tenant=TENANT, user=USER, timeout=40):
    r = _create(client, tenant, user)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    dl = time.time() + timeout
    while time.time() < dl:
        data = _events(client, run_id, tenant, user)
        if data["terminal"]:
            return run_id, data
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} did not finish")


def test_pool_runs_managed_run_through_real_dispatch(managed_pool):
    client, pool = managed_pool
    r = _create(client)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    dl = time.time() + 40
    data = None
    while time.time() < dl:
        data = _events(client, run_id)
        if data["terminal"]:
            break
        time.sleep(0.25)
    assert data and data["status"] == "completed", data
    kinds = {e["kind"] for e in data["events"]}
    assert "pool_worker_bound" in kinds, kinds
    bound = [e for e in data["events"] if e["kind"] == "pool_worker_bound"][0]["payload"]
    # per-run rebind through the pool: managed admission succeeded, correct tenant/task.
    assert bound["has_admitted"] is True and bound["established"] is True
    assert bound["tenant"] == TENANT
    assert bound["task_env"] == run_id
    # the warm worker that ran it was a real pool process (a real OS pid the
    # dispatcher persisted for crash detection — cleared again on completion).
    assert isinstance(bound["pid"], int) and bound["pid"] > 0


def test_pool_per_run_rebind_isolation_across_tenants(managed_pool):
    client, pool = managed_pool
    _, a = _run_to_completion(client, tenant="tenantA-pool", user="userA-pool")
    _, b = _run_to_completion(client, tenant="tenantB-pool", user="userB-pool")
    ba = [e for e in a["events"] if e["kind"] == "pool_worker_bound"][0]["payload"]
    bb = [e for e in b["events"] if e["kind"] == "pool_worker_bound"][0]["payload"]
    # each run bound its OWN tenant; single-use workers -> distinct processes, so a
    # grant/tenant/memory context can never leak from one run into another.
    assert ba["tenant"] == "tenantA-pool" and bb["tenant"] == "tenantB-pool"
    assert ba["pid"] != bb["pid"]
    assert a["status"] == "completed" and b["status"] == "completed"


def test_one_run_per_worker(managed_pool):
    client, pool = managed_pool
    _, data = _run_to_completion(client)
    pid = [e for e in data["events"] if e["kind"] == "pool_worker_bound"][0]["payload"]["pid"]
    # the worker that ran it has EXITED (single-use); give it a moment to reap.
    dl = time.time() + 10
    while time.time() < dl and _pid_alive(pid):
        time.sleep(0.2)
    assert not _pid_alive(pid), f"pool worker {pid} did not exit after its single run"


def test_seam_routes_real_model_spawn_to_pool_when_enabled(monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_WORKER_POOL", "1")
    monkeypatch.setattr(runtime, "_spawn_override", None)
    monkeypatch.setattr(runtime, "_resolve_task_mode", lambda _id: "model")

    class _StubPool:
        def __init__(self): self.calls = []
        def spawn(self, task, workspace, *, board=None):
            self.calls.append((task.id, board)); return 9999
    stub = _StubPool()
    monkeypatch.setattr(runtime, "_get_worker_pool", lambda: stub)

    class _T:
        id = "t_seam"
    pid = runtime._mode_aware_spawn(_T(), "/ws", board="b")
    assert pid == 9999 and stub.calls == [("t_seam", "b")]


# ── Part C — production activation contract: fail-closed config ────────────────


def _cold_spawn_probe(monkeypatch):
    monkeypatch.setattr(runtime, "_spawn_override", None)
    monkeypatch.setattr(runtime, "_resolve_task_mode", lambda _id: "model")
    monkeypatch.setattr(runtime, "_worker_pool", None)
    monkeypatch.setattr(runtime, "_worker_pool_disabled", False)
    called = {"n": 0}
    monkeypatch.setattr(kb, "_default_spawn",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 111)
    return called


class _T:
    id = "t_cfg"


@pytest.mark.parametrize("bad_size", ["not-a-number", "0", "-3", "9999", "1.5", ""])
def test_invalid_or_unsafe_pool_size_fails_closed(monkeypatch, bad_size):
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_WORKER_POOL", "1")
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_WORKER_POOL_SIZE", bad_size)
    called = _cold_spawn_probe(monkeypatch)
    pid = runtime._mode_aware_spawn(_T(), "/ws", board=None)
    assert pid == 111 and called["n"] == 1        # fell back to the safe cold spawn
    assert runtime._get_worker_pool() is None     # pool stays disabled (sticky)


def test_pool_creation_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_WORKER_POOL", "1")
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_WORKER_POOL_SIZE", "2")
    called = _cold_spawn_probe(monkeypatch)
    import youtab_agent_cli.worker_pool as wp
    def _boom(*a, **k):
        raise RuntimeError("cannot spawn")
    monkeypatch.setattr(wp, "WorkerPool", _boom)
    pid = runtime._mode_aware_spawn(_T(), "/ws", board=None)
    assert pid == 111 and called["n"] == 1        # creation failure -> cold spawn
    assert runtime._get_worker_pool() is None     # disabled, never retried each spawn


def test_valid_size_within_ceiling_enables_pool(monkeypatch):
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_WORKER_POOL_SIZE", "3")
    assert runtime._worker_pool_size() == 3
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_WORKER_POOL_SIZE", "16")
    assert runtime._worker_pool_size() == 16      # ceiling accepted
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_WORKER_POOL_SIZE", "17")
    assert runtime._worker_pool_size() is None     # over ceiling rejected


# ── Part B — pool mechanics (hermetic, no live model) ─────────────────────────


def _wait_ready(pool, n, timeout=30):
    dl = time.time() + timeout
    while time.time() < dl:
        if pool.stats()["ready"] >= n:
            return True
        time.sleep(0.05)
    return False


def test_bounded_size_and_zero_survivor_teardown(tmp_path):
    pool = WorkerPool(size=3, runner="deterministic", warm_full=False,
                      base_dir=str(tmp_path / "p"))
    try:
        assert _wait_ready(pool, 3), pool.stats()
        assert pool.stats()["total"] == 3  # bounded — never more than size warm idle
    finally:
        proof = pool.close()
    assert proof["zero_survivors"] and not proof["survivors"], proof
    # every terminated pid is actually gone
    assert all(not _pid_alive(p) for p in proof["terminated"])


def test_crash_recovery_replaces_dead_worker(tmp_path):
    pool = WorkerPool(size=2, runner="deterministic", warm_full=False,
                      base_dir=str(tmp_path / "p"))
    try:
        assert _wait_ready(pool, 2)
        victim = pool._all[0]
        victim.proc.kill(); victim.proc.wait(timeout=10)
        # next reap (triggered by stats->_take_ready path) drops the corpse + refills
        pool._reap()
        assert _wait_ready(pool, 2), pool.stats()
        assert all(w.proc.poll() is None for w in pool._all)  # all replacements alive
    finally:
        assert pool.close()["zero_survivors"]


def test_stale_worker_rejected_and_replaced(tmp_path):
    pool = WorkerPool(size=2, runner="deterministic", warm_full=False,
                      idle_ttl_seconds=0.5, base_dir=str(tmp_path / "p"))
    try:
        assert _wait_ready(pool, 2)
        first = {w.proc.pid for w in pool._all}
        time.sleep(0.8)  # exceed idle TTL
        pool._reap()
        assert _wait_ready(pool, 2)
        second = {w.proc.pid for w in pool._all}
        assert first.isdisjoint(second), (first, second)  # all stale workers replaced
    finally:
        assert pool.close()["zero_survivors"]


def test_saturated_pool_backpressure_falls_back_not_stall(tmp_path, monkeypatch):
    # size=1, no warm worker ever marked ready (simulate saturation) -> spawn must
    # not stall the board forever; it falls back to a fresh spawn after the wait.
    pool = WorkerPool(size=1, runner="deterministic", warm_full=False,
                      assign_wait_seconds=0.3, base_dir=str(tmp_path / "p"))
    monkeypatch.setattr(pool, "_take_ready", lambda: None)  # force saturation
    called = {"n": 0}
    monkeypatch.setattr(kb, "_default_spawn", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 4321)

    class _T:
        id = "t_x"; assignee = "default"; tenant = TENANT
    pid = pool.spawn(_T(), str(tmp_path), board=None)
    assert pid == 4321 and called["n"] == 1  # fell back, did not stall
    pool.close()


# ── Part C — per-run env fidelity + single-use worker cleanup (WAVE-30H fixes) ──


def test_pool_spawn_populates_env_drop_for_subtracted_keys(tmp_path, monkeypatch):
    # Regression: build_worker_invocation REMOVES dispatcher-only keys (session
    # routing vars + YOUTAB_AGENT_TUI) from the per-run env, but a warm worker
    # inherited the dispatcher's FULL env at pre-spawn. spawn() must therefore
    # emit env_drop = (dispatcher env) - (per-run env) so serve() prunes them;
    # otherwise e.g. an inherited YOUTAB_AGENT_TUI=1 boots the TUI and the worker
    # bails without doing the run. Before the fix, spec had no "env_drop" key.
    db = tmp_path / "k.db"
    per_run_env = {"KEEP": "1", "YOUTAB_AGENT_KANBAN_DB": str(db)}
    monkeypatch.setattr(
        kb, "build_worker_invocation",
        lambda task, ws, board=None: (dict(per_run_env),
                                      ["youtab", "-p", "default", "--cli", "chat", "-q", "x"]))
    monkeypatch.setenv("YOUTAB_AGENT_TUI", "1")           # must be dropped
    monkeypatch.setenv("YOUTAB_AGENT_DISPATCHER_ONLY", "leak")  # dispatcher-only -> dropped
    pool = WorkerPool(size=1, runner="deterministic", warm_full=False,
                      base_dir=str(tmp_path / "p"))
    try:
        assert _wait_ready(pool, 1)

        class _T:
            id = "t_env"; assignee = "default"; tenant = TENANT
        pid = pool.spawn(_T(), str(tmp_path), board=None)
        assert pid
        assigned = [w for w in pool._all if w.assigned]
        assert assigned, "spawn should mark a warm worker assigned"
        spec = json.loads((assigned[0].dir / "assign.json").read_text(encoding="utf-8"))
        drop = set(spec["env_drop"])  # KeyError here pre-fix (seam was dead)
        assert "YOUTAB_AGENT_TUI" in drop
        assert "YOUTAB_AGENT_DISPATCHER_ONLY" in drop
        assert "KEEP" not in drop  # present in the per-run env -> retained
    finally:
        assert pool.close()["zero_survivors"]


def test_reap_prunes_dead_single_use_workers_from_all(tmp_path):
    # Regression: a worker is removed from _ready at assignment and never re-enters
    # the _reap ready-loop, so after its single run exits it lingered in _all for
    # the pool's lifetime (leaked _Worker/Popen, inflated stats()["assigned"]).
    pool = WorkerPool(size=2, runner="deterministic", warm_full=False,
                      base_dir=str(tmp_path / "p"))
    try:
        assert _wait_ready(pool, 2)
        w = pool._take_ready()  # assign one worker (removed from _ready, stays in _all)
        assert w is not None and w.assigned
        w.proc.terminate()      # simulate the single-use run finishing/exiting
        w.proc.wait(timeout=10)
        pool._reap()
        dead_assigned = [x for x in pool._all if x.assigned and x.proc.poll() is not None]
        assert dead_assigned == [], "finished assigned worker must be pruned from _all"
        assert _wait_ready(pool, 2)          # refilled to size
        assert pool.stats()["assigned"] == 0  # no lingering dead-assigned counted
    finally:
        assert pool.close()["zero_survivors"]
