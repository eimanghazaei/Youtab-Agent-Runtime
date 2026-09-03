"""Tenant-plane PLAIN-RUN CANCELLATION coverage for /api/runtime/v1 (AR-PROD-01).

Focused, credential-free tests for the engine's plain-run cancel command
(``POST /api/runtime/v1/runs/{run_id}/cancel`` in
``youtab_agent_cli/web_routers/runtime.py``). The runtime is never mocked — the
real kanban run store drives every assertion; only the model "brain" is replaced
by deterministic local worker subprocesses (a strong-secret, no-network local
proof, authorized), exactly as in ``test_runtime_api.py``.

Coverage (the ten cancellation contract cases, engine plane):

  1. cancel BEFORE dispatch  -> terminal cancelled, no worker ever spawned
  2. cancel DURING an active run -> the live worker is killed; run is cancelled
  3. repeated cancel is IDEMPOTENT (2nd cancel: no error, still cancelled)
  4. cancel from the WRONG tenant / user is DENIED (404) and does NOT cancel
  5. a terminal run is not reopened/undone (result preserved, never re-run)
  6. cancel triggers NO fallback / retry / replay (no new run is created)
  7. cancel causes NO duplicate tool/external side effect (worker runs once)
  8. persisted terminal state + ordered events are correct
  9. a cancellation is DISTINGUISHABLE from a failure/timeout (distinct label
     and the cancel event is the discriminator)
 10. the gateway-forwarded correlation id is bound to the cancelled run
     (the id the connector emits on the signed path is the id the engine
     persists — cross-plane agreement).

The blocking-worker cases (2) exercise a real ``terminate_pid`` kill and so are
intended for the Linux CI job; every other case is deterministic and portable.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
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

# Repo root (this file is tests/youtab_agent_cli/test_runtime_cancellation.py).
REPO_ROOT = str(Path(__file__).resolve().parents[2])

# A strong secret that clears the runtime-service entropy gate.
SECRET = secrets.token_urlsafe(48)

# The cancel event kind the engine records to mark a product-initiated cancel.
CANCEL_EVENT = "runtime_cancel_requested"

# --- deterministic local workers -------------------------------------------

# Completes fast: emits ordered events, records ONE side-effect line, writes ONE
# artifact, then completes the task. The single side-effect line lets a test
# prove a later cancel does NOT re-run the worker (no duplicate side effect).
_INSTANT_WORKER_SRC = '''
import os, sys, time
from pathlib import Path
from youtab_agent_cli import kanban_db as kb

task_id = sys.argv[1]
db_path = Path(sys.argv[2])
sidecar = Path(sys.argv[3])
with sidecar.open("a", encoding="utf-8") as fh:
    fh.write(f"invocation pid={os.getpid()}\\n")
time.sleep(0.1)
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

# Starts, records ONE side-effect line + a worker_started event, then BLOCKS
# until either a release file appears (then it would complete) or it is killed.
# In the cancel-during-active test it is never released, so it must be killed by
# the engine's cancel; it must never write the "completed" marker or an artifact.
_BLOCKING_WORKER_SRC = '''
import os, sys, time
from pathlib import Path
from youtab_agent_cli import kanban_db as kb

task_id = sys.argv[1]
db_path = Path(sys.argv[2])
sidecar = Path(sys.argv[3])
release = Path(sys.argv[4])
with sidecar.open("a", encoding="utf-8") as fh:
    fh.write(f"started pid={os.getpid()}\\n")
conn = kb.connect(db_path=db_path)
with kb.write_txn(conn):
    kb._append_event(conn, task_id, "worker_started", {"pid": os.getpid()})
conn.close()
deadline = time.time() + 30
while time.time() < deadline and not release.exists():
    time.sleep(0.1)
# Only reached if released (never in the cancel test): mark completion so an
# accidental non-kill would be caught by the "no completion" assertions.
conn = kb.connect(db_path=db_path)
kb.complete_task(conn, task_id, result="RELEASED", summary="released")
conn.close()
with sidecar.open("a", encoding="utf-8") as fh:
    fh.write("completed\\n")
'''


class _FakeProfile:
    def __init__(self, name):
        self.name = name
        self.description = "test agent"
        self.model = "local-deterministic"
        self.provider = "local"
        self.skill_count = 3
        self.is_default = name == "default"


class _Harness:
    """Bundle of the TestClient plus the paths/handles a test asserts against."""

    def __init__(self, client, *, db_path, sidecar, release, spawned):
        self.client = client
        self.db_path = db_path
        self.sidecar = sidecar  # side-effect log written once per worker run
        self.release = release  # touch to release the blocking worker
        self.spawned = spawned  # list[subprocess.Popen] the dispatcher launched


def _make_harness(tmp_path, monkeypatch, worker_src):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_DB", str(db_path))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("YOUTAB_AGENT_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))
    monkeypatch.setenv("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET", SECRET)

    from plugins.dashboard_auth.runtime_service import RuntimeServiceProvider
    auth_registry.clear_providers()
    auth_registry.register_provider(RuntimeServiceProvider(secret=SECRET, scope="runtime"))
    token_auth.clear_token_routes()
    token_auth.register_token_route_prefix("/api/runtime/v1/", provider="runtime-service", capability="runtime")

    monkeypatch.setattr(
        "youtab_agent_cli.profiles.list_profiles", lambda: [_FakeProfile("default")]
    )

    worker_py = tmp_path / "worker.py"
    worker_py.write_text(worker_src, encoding="utf-8")
    sidecar = tmp_path / "invocations.log"
    release = tmp_path / "release.flag"
    spawned: list[subprocess.Popen] = []

    def _spawn(task, workspace, *, board=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, str(worker_py), task.id, str(db_path), str(sidecar), str(release)],
            env=env,
        )
        spawned.append(proc)
        return proc.pid

    runtime._spawn_override = _spawn
    runtime._nonce_store = None

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await token_auth_middleware(request, call_next)

    app.include_router(runtime.router)

    # Real startup ordering (WAVE-22): serve only from a VERIFIED generation.
    # This bare harness app has no lifespan, so drive the isolated registry
    # through the real declare → freeze → verify → VERIFIED transition.
    token_auth.require_route_ownership(
        provider="runtime-service", path="/api/runtime/v1/", is_prefix=True,
        capability="runtime")
    token_auth.freeze_token_routes()
    token_auth.verify_service_route_ownership()

    client = TestClient(app)
    client.__enter__()
    return _Harness(client, db_path=db_path, sidecar=sidecar, release=release, spawned=spawned)


def _teardown_harness(h: _Harness):
    runtime.stop_dispatcher()
    # Release then hard-stop any worker still alive so no process leaks.
    try:
        h.release.touch()
    except OSError:
        pass
    for proc in h.spawned:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
    try:
        h.client.__exit__(None, None, None)
    finally:
        runtime._spawn_override = None
        runtime._nonce_store = None
        # Registry is VERIFIED (frozen) — clear_* is refused after freeze; the
        # autouse fresh-registry fixture provides per-test isolation.


@pytest.fixture()
def instant(tmp_path, monkeypatch):
    """A harness whose worker completes immediately."""
    h = _make_harness(tmp_path, monkeypatch, _INSTANT_WORKER_SRC)
    try:
        yield h
    finally:
        _teardown_harness(h)


@pytest.fixture()
def blocking(tmp_path, monkeypatch):
    """A harness whose worker blocks until released or killed."""
    h = _make_harness(tmp_path, monkeypatch, _BLOCKING_WORKER_SRC)
    try:
        yield h
    finally:
        _teardown_harness(h)


# --------------------------------------------------------------------------
# signing / request helpers
#
# As of the correlation-binding slice (CORRELATION_CONTRACT v1) the per-request
# correlation id is field 8 of the signed canonical string, so a signed mutating
# command MUST cover the SAME correlation it forwards on the header (a mismatch
# now fails closed as ``bad_signature``). These helpers therefore sign the
# forwarded correlation rather than treating it as transport-only.
# --------------------------------------------------------------------------


def _identity_headers(tenant="tenantA", user="userA", roles="member", correlation="cid-test"):
    return {
        "Authorization": f"Bearer {SECRET}",
        "X-Youtab-Tenant-Id": tenant,
        "X-Youtab-User-Id": user,
        "X-Youtab-Roles": roles,
        "X-Youtab-Correlation-Id": correlation,
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


def _create_run(client, tenant="tenantA", user="userA", task="add 2 and 2",
                correlation="cid-test", nonce=None):
    body = json.dumps({"agent": "default", "task": task}).encode()
    path = "/api/runtime/v1/runs"
    headers = _identity_headers(tenant, user, correlation=correlation)
    headers.update(_sign("POST", path, tenant, user, body, nonce=nonce, correlation=correlation))
    headers["Content-Type"] = "application/json"
    return client.post(path, content=body, headers=headers)


def _cancel(client, run_id, tenant="tenantA", user="userA", correlation="cid-test", nonce=None):
    path = f"/api/runtime/v1/runs/{run_id}/cancel"
    headers = _identity_headers(tenant, user, correlation=correlation)
    headers.update(_sign("POST", path, tenant, user, b"", nonce=nonce, correlation=correlation))
    return client.post(path, headers=headers)


def _detail(client, run_id, tenant="tenantA", user="userA"):
    return client.get(f"/api/runtime/v1/runs/{run_id}", headers=_identity_headers(tenant, user))


def _events(client, run_id, tenant="tenantA", user="userA", after=0):
    return client.get(
        f"/api/runtime/v1/runs/{run_id}/events?after={after}",
        headers=_identity_headers(tenant, user),
    )


def _wait(predicate, timeout=20, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = predicate()
        if val:
            return val
        time.sleep(interval)
    return None


def _seed_task(*, tenant="tenantA", user="userA", correlation="cid-test",
               initial_status="running", title="seed", body="seed body"):
    """Create a run row directly on the runtime board (bypasses dispatch)."""
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        run_id = kb.create_task(
            conn,
            title=title,
            body=body,
            assignee="default",
            created_by=user,
            tenant=tenant,
            board=runtime.RUNTIME_BOARD,
            initial_status=initial_status,
            session_id=correlation,
        )
    return run_id


def _append_events(run_id, kinds):
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        with kb.write_txn(conn):
            for kind in kinds:
                kb._append_event(conn, run_id, kind, {"k": kind})


def _get_task(run_id):
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        return kb.get_task(conn, run_id)


# ==========================================================================
# Case 1 — cancel BEFORE dispatch: cancelled, no worker ever spawned
# ==========================================================================


def test_cancel_before_dispatch_is_cancelled_with_no_side_effect(instant):
    # A run that was created but never handed to the dispatcher: kanban's only
    # initial states are 'running'/'blocked', so a freshly created run is
    # 'running' with NO worker_pid and no spawned worker (dispatch never ran).
    # The spawn override is installed but must never fire.
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")

    r = _cancel(instant.client, run_id)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    # No worker was ever launched -> no dispatch, no side effect.
    assert instant.spawned == []
    assert not instant.sidecar.exists()

    # Persisted terminal state: the task is blocked and the cancel intent event
    # is recorded; detail projects 'cancelled'.
    task = _get_task(run_id)
    assert task.status == "blocked"
    events = _events(instant.client, run_id).json()["events"]
    assert any(e["kind"] == CANCEL_EVENT for e in events)
    assert _detail(instant.client, run_id).json()["status"] == "cancelled"


# ==========================================================================
# Case 2 — cancel DURING an active run: the live worker is killed
# ==========================================================================


# This test genuinely delivers a real signal to its OWN child worker (the
# engine's cancel calls terminate_pid -> os.kill on the worker pid), so it opts
# out of the hermetic live-system guard, exactly as sanctioned for tests that
# signal their own child. It is primarily a Linux-CI assertion.
@pytest.mark.live_system_guard_bypass
def test_cancel_during_active_run_kills_worker_and_marks_cancelled(blocking):
    r = _create_run(blocking.client, task="long running work")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # Wait until the real worker subprocess has actually started (it wrote its
    # side-effect line and a worker_started event) — the run is now active.
    started = _wait(lambda: blocking.sidecar.exists()
                    and "started" in blocking.sidecar.read_text(encoding="utf-8"))
    assert started, "worker did not start"
    assert blocking.spawned, "dispatcher never spawned a worker"
    proc = blocking.spawned[-1]

    # Cancel the in-flight run.
    r = _cancel(blocking.client, run_id)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    # In-flight work stopped: the worker process is dead and never completed.
    exited = _wait(lambda: proc.poll() is not None, timeout=15)
    assert exited, "worker subprocess was not terminated by cancel"
    sidecar_text = blocking.sidecar.read_text(encoding="utf-8")
    assert "completed" not in sidecar_text  # never reached its completion marker

    # Terminal state is cancelled; the worker's OWN output never landed (it was
    # killed mid-flight) — no released worker result, no artifact produced. The
    # only 'result' present is the cancel reason recorded by the block.
    detail = _detail(blocking.client, run_id).json()
    assert detail["status"] == "cancelled"
    assert detail.get("result") != "RELEASED"  # the worker never completed
    arts = blocking.client.get(
        f"/api/runtime/v1/runs/{run_id}/artifacts", headers=_identity_headers()
    ).json()["artifacts"]
    assert arts == []


# ==========================================================================
# Case 3 — repeated cancel is IDEMPOTENT
# ==========================================================================


def test_repeated_cancel_is_idempotent(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")

    first = _cancel(instant.client, run_id)
    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"

    # A second, independently-signed cancel (fresh nonce) is a no-op success,
    # not an error and not a state change.
    second = _cancel(instant.client, run_id)
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"

    # State is stable: still blocked, still projected cancelled.
    assert _get_task(run_id).status == "blocked"
    assert _detail(instant.client, run_id).json()["status"] == "cancelled"


# ==========================================================================
# Case 4 — cancel from the WRONG tenant / user is DENIED (404), no cancel
# ==========================================================================


def test_cancel_from_wrong_tenant_is_404_and_does_not_cancel(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(tenant="tenantA", user="userA", initial_status="running")

    # Different tenant: existence must not leak, and nothing is cancelled.
    wrong = _cancel(instant.client, run_id, tenant="tenantB", user="userA")
    assert wrong.status_code == 404

    # Different user, same tenant: also denied.
    wrong_user = _cancel(instant.client, run_id, tenant="tenantA", user="userB")
    assert wrong_user.status_code == 404

    # The run was NOT cancelled by the denied attempts: no cancel intent was
    # ever recorded and (read back by the owner) it is not projected cancelled.
    events = _events(instant.client, run_id).json()["events"]
    assert not any(e["kind"] == CANCEL_EVENT for e in events)
    assert _detail(instant.client, run_id).json()["status"] != "cancelled"

    # The rightful owner can still cancel it.
    ok = _cancel(instant.client, run_id, tenant="tenantA", user="userA")
    assert ok.status_code == 200
    assert ok.json()["status"] == "cancelled"


# ==========================================================================
# Case 5 — a terminal (completed) run is not reopened / re-executed
# ==========================================================================


def test_completed_run_is_not_reopened_or_reexecuted_by_cancel(instant):
    # Drive a real run to completion.
    run_id = _create_run(instant.client).json()["run_id"]
    completed = _wait(lambda: _detail(instant.client, run_id).json()["status"] == "completed",
                      timeout=25)
    assert completed, "run never completed"
    assert instant.sidecar.read_text(encoding="utf-8").count("invocation") == 1

    # Cancel the already-finished run.
    r = _cancel(instant.client, run_id)
    assert r.status_code == 200

    detail = _detail(instant.client, run_id).json()
    # The completed OUTPUT is preserved and the worker is NOT re-run: the run is
    # never reopened to a running/queued state, and there is exactly one worker
    # invocation and one artifact.
    assert detail["result"] == "2 + 2 = 4"
    assert instant.sidecar.read_text(encoding="utf-8").count("invocation") == 1
    arts = instant.client.get(
        f"/api/runtime/v1/runs/{run_id}/artifacts", headers=_identity_headers()
    ).json()["artifacts"]
    assert len(arts) == 1
    # The run stays terminal and keeps its completed outcome — never reopened to
    # running/queued and never relabelled by the late cancel.
    assert detail["status"] == "completed"
    assert _get_task(run_id).status == "done"  # kanban terminal state unchanged


def test_completed_run_should_remain_completed_after_late_cancel(instant):
    # Terminal-run preservation: a run that already completed keeps its
    # 'completed' status when a late cancel arrives — the cancel is a no-op and
    # must not mask the successful outcome as 'cancelled'. (Regression guard for
    # the terminal-run relabel fix in runtime.runtime_cancel_run.)
    run_id = _create_run(instant.client).json()["run_id"]
    assert _wait(
        lambda: _detail(instant.client, run_id).json()["status"] == "completed", timeout=25
    ), "run never completed"

    r = _cancel(instant.client, run_id)
    assert r.status_code == 200
    # The no-op cancel returns the preserved terminal status, and detail/events
    # continue to project 'completed' (no cancel event was recorded).
    assert r.json()["status"] == "completed"
    assert _detail(instant.client, run_id).json()["status"] == "completed"
    events = _events(instant.client, run_id).json()["events"]
    assert not any(e["kind"] == CANCEL_EVENT for e in events)


# ==========================================================================
# Case 6 — cancel triggers NO fallback / retry / replay (no new run)
# ==========================================================================


def test_cancel_does_not_create_a_retry_or_replacement_run(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")

    def _runtime_run_ids():
        return {
            x["run_id"]
            for x in instant.client.get(
                "/api/runtime/v1/runs", headers=_identity_headers()
            ).json()["runs"]
        }

    before = _runtime_run_ids()
    r = _cancel(instant.client, run_id)
    assert r.status_code == 200
    after = _runtime_run_ids()

    # No replacement/retry run was spawned by the cancel: the run set is
    # unchanged (a retry would ADD a new run id, as runtime_retry_run does).
    assert after == before
    # And no new worker subprocess was launched as a fallback execution.
    assert instant.spawned == []


# ==========================================================================
# Case 7 — cancel causes NO duplicate tool / external side effect
# ==========================================================================


def test_cancel_causes_no_duplicate_worker_side_effect(instant):
    run_id = _create_run(instant.client).json()["run_id"]
    assert _wait(
        lambda: _detail(instant.client, run_id).json()["status"] == "completed", timeout=25
    ), "run never completed"
    invocations_before = instant.sidecar.read_text(encoding="utf-8").count("invocation")
    assert invocations_before == 1

    # Cancel after completion + a second idempotent cancel: neither may cause
    # the worker (the external side effect) to run again.
    assert _cancel(instant.client, run_id).status_code == 200
    assert _cancel(instant.client, run_id).status_code == 200

    invocations_after = instant.sidecar.read_text(encoding="utf-8").count("invocation")
    assert invocations_after == 1  # exactly one side effect, no duplication


# ==========================================================================
# Case 8 — persisted terminal state + ordered events are correct
# ==========================================================================


def test_cancel_persists_terminal_state_and_orders_events(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")
    # Two authoritative pre-cancel events, so ordering is observable.
    _append_events(run_id, ["runtime_execution_mode", "worker_progress"])

    r = _cancel(instant.client, run_id)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # Persisted terminal state: the underlying task is blocked (kanban terminal
    # for a cancel), projected as 'cancelled', and terminal=True on the stream.
    assert _get_task(run_id).status == "blocked"
    stream = _events(instant.client, run_id).json()
    assert stream["status"] == "cancelled"
    assert stream["terminal"] is True

    events = stream["events"]
    ids = [e["id"] for e in events]
    kinds = [e["kind"] for e in events]
    # Monotonic ordering by id (== creation order). The cancel intent is
    # recorded exactly once, after the pre-existing mode/progress events, and
    # ahead of the terminal 'blocked' transition event that block_task writes.
    assert ids == sorted(ids)
    assert kinds.count(CANCEL_EVENT) == 1
    assert kinds.index("runtime_execution_mode") < kinds.index(CANCEL_EVENT)
    assert kinds.index("worker_progress") < kinds.index(CANCEL_EVENT)
    assert kinds.index(CANCEL_EVENT) < kinds.index("blocked")


# ==========================================================================
# Case 9 — a cancellation is DISTINGUISHABLE from a failure / timeout
# ==========================================================================


def test_cancellation_is_distinguishable_from_a_failure(instant):
    runtime.stop_dispatcher()

    # A run that FAILED / timed out ends 'blocked' with NO cancel event.
    failed_id = _seed_task(initial_status="running", title="failed")
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        kb.block_task(conn, failed_id, reason="worker timed out")

    # A run the user CANCELLED ends 'blocked' WITH a cancel event.
    cancelled_id = _seed_task(initial_status="running", title="cancelled")
    assert _cancel(instant.client, cancelled_id).status_code == 200

    failed_detail = _detail(instant.client, failed_id).json()
    cancelled_detail = _detail(instant.client, cancelled_id).json()

    # Distinct terminal reasons — a timeout/failure is NOT reported as a cancel.
    assert failed_detail["status"] == "blocked"
    assert cancelled_detail["status"] == "cancelled"
    assert failed_detail["status"] != cancelled_detail["status"]

    # The cancel event is the discriminator: present only for the cancellation.
    failed_events = _events(instant.client, failed_id).json()["events"]
    cancelled_events = _events(instant.client, cancelled_id).json()["events"]
    assert not any(e["kind"] == CANCEL_EVENT for e in failed_events)
    assert any(e["kind"] == CANCEL_EVENT for e in cancelled_events)


# ==========================================================================
# Case 10 — the gateway-forwarded correlation id is bound to the run
# ==========================================================================


def test_forwarded_correlation_is_bound_to_the_cancelled_run(instant):
    runtime.stop_dispatcher()
    correlation = "cid-cross-plane-0001"

    # Create through the signed path with the gateway-forwarded correlation.
    run_id = _create_run(
        instant.client, task="corr", correlation=correlation
    ).json()["run_id"]

    # The engine bound the forwarded correlation to the run (persisted).
    assert _get_task(run_id).session_id == correlation

    # Cancel under the SAME correlation the gateway would forward — accepted,
    # and the run stays bound to that one correlation (cross-plane agreement:
    # the id the connector emits is the id the engine persists).
    r = _cancel(instant.client, run_id, correlation=correlation)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert _get_task(run_id).session_id == correlation


# ==========================================================================
# Cancellation ATOMICITY (race) coverage — the cancel handler makes its
# terminal-state check + ownership decision + cancel-transition append ATOMIC
# under the run store's canonical single-writer lock (``kanban_db.write_txn``
# == ``BEGIN IMMEDIATE``). A worker completion cannot commit between the
# terminality read and the cancel append: it has either already committed (the
# cancel no-ops and the completion is preserved) or it is serialized after this
# transaction. These tests drive the REAL kanban run store (no mock lock) and,
# where a race must be forced, use the store's own write lock as the
# deterministic seam rather than a sleep.
# ==========================================================================


def _list_events(run_id):
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        return kb.list_events(conn, run_id)


def _cancel_event_count(run_id):
    return sum(1 for e in _list_events(run_id) if e.kind == CANCEL_EVENT)


def _completed_event_count(run_id):
    return sum(1 for e in _list_events(run_id) if e.kind == "completed")


def _store_complete(run_id, *, result="2 + 2 = 4", summary="store completion"):
    """Directly transition a seeded run to completed via the real store CAS.

    Models the worker's own completion commit (``kanban_db.complete_task`` opens
    its own ``BEGIN IMMEDIATE`` write txn), so it is serialized against the cancel
    handler's write txn exactly as a real worker would be.
    """
    with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
        return kb.complete_task(conn, run_id, result=result, summary=summary)


# --------------------------------------------------------------------------
# Race A — a worker completion that COMMITS WHILE the cancel is waiting on the
# write lock is preserved; the cancel observes the committed 'completed' inside
# the lock and no-ops. This is the regression guard for the check-then-act race:
# the terminality read is now INSIDE the write txn, so a completion can never
# land between a (previously pre-lock) read and the cancel-event append.
# --------------------------------------------------------------------------


def test_completion_committing_under_lock_while_cancel_waits_is_preserved(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")

    lock_held = threading.Event()
    release = threading.Event()
    holder_err = {}

    def _holder():
        # Hold the store's write lock across an in-flight completion, then commit
        # only when released. The cancel handler's BEGIN IMMEDIATE cannot proceed
        # until this COMMIT lands — the lock IS the deterministic seam.
        try:
            with kb.connect_closing(board=runtime.RUNTIME_BOARD) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE tasks SET status='done', completed_at=?, result=?, "
                    "current_run_id=NULL "
                    "WHERE id=? AND status IN ('running','ready','blocked')",
                    (int(time.time()), "2 + 2 = 4", run_id),
                )
                kb._append_event(
                    conn, run_id, "completed", {"result_len": 9, "summary": "won"}
                )
                lock_held.set()
                release.wait(10)
                conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 — surface to the test thread
            holder_err["exc"] = exc
            lock_held.set()

    ht = threading.Thread(target=_holder)
    ht.start()
    assert lock_held.wait(10), "holder never acquired the write lock"
    assert "exc" not in holder_err, holder_err.get("exc")

    cancel_out = {}

    def _do_cancel():
        r = _cancel(instant.client, run_id)
        cancel_out["code"] = r.status_code
        cancel_out["body"] = r.json()

    ct = threading.Thread(target=_do_cancel)
    ct.start()

    # Release the completion so it COMMITS; the cancel's BEGIN IMMEDIATE then
    # acquires the lock and reads the committed 'done'/completed state. On the
    # fixed handler this is an idempotent no-op returning the preserved
    # 'completed'; the pre-fix handler (pre-lock read) would have relabelled the
    # just-completed run as 'cancelled'.
    release.set()
    ct.join(20)
    ht.join(10)
    assert "exc" not in holder_err, holder_err.get("exc")

    assert cancel_out.get("code") == 200, cancel_out
    assert cancel_out["body"]["status"] == "completed"   # completion preserved
    assert _cancel_event_count(run_id) == 0              # no cancel event appended
    assert _get_task(run_id).status == "done"            # store terminal unchanged
    detail = _detail(instant.client, run_id).json()
    assert detail["status"] == "completed"
    assert detail["result"] == "2 + 2 = 4"               # successful outcome kept


# --------------------------------------------------------------------------
# Race A' — completion COMMITTED first (no contention), then a late cancel: the
# in-lock terminality read sees 'completed' and no-ops. Same guarantee, exercised
# purely through the store CAS with no threads.
# --------------------------------------------------------------------------


def test_late_cancel_after_store_completion_is_a_noop(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")

    assert _store_complete(run_id, result="2 + 2 = 4") is True
    assert _detail(instant.client, run_id).json()["status"] == "completed"

    r = _cancel(instant.client, run_id)
    assert r.status_code == 200
    assert r.json()["status"] == "completed"             # preserved, not relabelled
    assert _cancel_event_count(run_id) == 0              # no cancel event
    assert _completed_event_count(run_id) == 1           # single terminal event
    assert _get_task(run_id).status == "done"


# --------------------------------------------------------------------------
# Race B — a completion that arrives AFTER the cancellation has committed cannot
# turn a cancelled run back into a completed one: the product projection treats
# the cancel transition as authoritative, so the run stays 'cancelled'. Exactly
# one cancel transition is recorded (no duplicate cancel event).
# --------------------------------------------------------------------------


def test_stale_worker_completion_after_cancel_cannot_reopen_as_completed(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")

    # Cancellation wins (no completion racing at this instant).
    r = _cancel(instant.client, run_id)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert _cancel_event_count(run_id) == 1
    assert _get_task(run_id).status == "blocked"

    # A stale worker now tries to complete the already-cancelled run. Whatever the
    # underlying kanban CAS does, the product NEVER regresses cancelled ->
    # completed: the cancel transition remains authoritative.
    _store_complete(run_id, result="LATE STRAGGLER")

    assert _detail(instant.client, run_id).json()["status"] == "cancelled"
    stream = _events(instant.client, run_id).json()
    assert stream["status"] == "cancelled"
    assert stream["terminal"] is True
    assert _cancel_event_count(run_id) == 1              # still exactly one cancel


# --------------------------------------------------------------------------
# Race B' — completion strictly serialized AFTER the cancellation commit (the
# completion is "paused" until the cancel transaction lands) yields a single
# cancel transition and a 'cancelled' projection.
# --------------------------------------------------------------------------


def test_completion_paused_until_cancel_commits_yields_single_cancel(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")

    cancel_committed = threading.Event()
    completion_started = threading.Event()

    def _paused_completion():
        # Do not attempt the completion until the cancel transaction has
        # committed — models a worker whose completion commit is scheduled after
        # the cancel wins the lock.
        completion_started.set()
        cancel_committed.wait(10)
        _store_complete(run_id, result="PAUSED THEN LATE")

    wt = threading.Thread(target=_paused_completion)
    wt.start()
    assert completion_started.wait(10)

    r = _cancel(instant.client, run_id)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    cancel_committed.set()
    wt.join(10)

    assert _cancel_event_count(run_id) == 1              # exactly one cancel transition
    assert _detail(instant.client, run_id).json()["status"] == "cancelled"


# --------------------------------------------------------------------------
# Race C — simultaneous completion vs cancellation, repeated enough to hit BOTH
# winners. Every outcome is coherent and terminal: exactly one terminal product
# outcome, at most one cancel event and at most one completed event, and the two
# invariants that would break under a non-atomic check-then-act:
#   * no cancel event  => the run projects 'completed' (completion won cleanly);
#   * a cancel event   => the run projects 'cancelled' (cancel is authoritative).
# --------------------------------------------------------------------------


def test_simultaneous_completion_and_cancellation_is_always_coherent(instant):
    runtime.stop_dispatcher()

    winners = {"completed": 0, "cancelled": 0}

    def _assert_coherent(tag, run_id, cancel_status):
        """The invariant every terminal outcome must satisfy, whoever wins.

        Exactly one terminal PRODUCT outcome, at most one cancel event and at
        most one completed event, and the projection is authoritative and
        agrees with the cancel handler's own response:
          * completed  <=> no cancel event was recorded (completion won cleanly);
          * cancelled  <=> exactly one cancel transition was recorded.
        A cancelled run is NEVER projected 'completed' even if a straggler
        completion committed after the cancel.
        """
        cancel_ct = _cancel_event_count(run_id)
        completed_ct = _completed_event_count(run_id)
        proj = _detail(instant.client, run_id).json()["status"]
        assert proj in ("completed", "cancelled"), (tag, proj)
        assert cancel_ct <= 1, (tag, "duplicate cancel event", cancel_ct)
        assert completed_ct <= 1, (tag, "duplicate completed event", completed_ct)
        if proj == "completed":
            assert cancel_ct == 0, (tag, "completed but a cancel event exists")
            assert completed_ct == 1, (tag, "completed but no completed event")
        else:  # cancelled
            assert cancel_ct == 1, (tag, "cancelled but not exactly one cancel event")
        # The cancel HTTP response agrees with the persisted projection.
        assert cancel_status == proj, (tag, cancel_status, proj)
        winners[proj] += 1
        return proj

    # (1) GENUINE simultaneous races. Both operations block on a barrier and then
    # contend for the run store's single write lock; whichever commits first wins
    # and the loser is serialized behind it. The per-iteration invariant must hold
    # for EITHER winner — this is the coherence guarantee under real contention.
    for i in range(24):
        run_id = _seed_task(initial_status="running", title=f"sim-{i}")
        barrier = threading.Barrier(2)
        box = {}

        def _t_cancel(rid=run_id, b=box, bar=barrier):
            bar.wait()
            r = _cancel(instant.client, rid)
            b["code"] = r.status_code
            b["status"] = r.json().get("status")

        def _t_complete(rid=run_id, bar=barrier):
            bar.wait()
            _store_complete(rid, result="2 + 2 = 4")

        tc = threading.Thread(target=_t_cancel)
        tw = threading.Thread(target=_t_complete)
        tc.start()
        tw.start()
        tc.join(20)
        tw.join(20)
        assert box.get("code") == 200, box
        _assert_coherent(f"sim-{i}", run_id, box.get("status"))

    # (2) Deterministically force EACH ordering so BOTH winners are exercised with
    # the exact same coherence contract, independent of wall-clock scheduling
    # (the concurrent loop above is dominated by the cancel path's HTTP overhead,
    # so completion usually wins the lock; these two guarantee coverage of the
    # cancellation-wins terminal transition as well).
    for k in range(4):
        # completion-wins: completion commits first, the late cancel no-ops.
        rid = _seed_task(initial_status="running", title=f"det-complete-{k}")
        assert _store_complete(rid, result="2 + 2 = 4") is True
        cs = _cancel(instant.client, rid).json()["status"]
        assert _assert_coherent(f"det-complete-{k}", rid, cs) == "completed"

        # cancellation-wins: cancel commits first; a stale worker completion that
        # lands afterward cannot flip the run back to 'completed'.
        rid = _seed_task(initial_status="running", title=f"det-cancel-{k}")
        cs = _cancel(instant.client, rid).json()["status"]
        _store_complete(rid, result="LATE STRAGGLER")
        assert _assert_coherent(f"det-cancel-{k}", rid, cs) == "cancelled"

    # Both terminal winners were genuinely produced under the coherence contract.
    assert winners["completed"] > 0, winners
    assert winners["cancelled"] > 0, winners


# --------------------------------------------------------------------------
# Race D — repeated cancel performs NO extra mutation: the second (and third)
# cancel add no cancel event and no completed event, and never regress the
# terminal projection.
# --------------------------------------------------------------------------


def test_repeated_cancel_records_no_additional_mutation(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(initial_status="running")

    assert _cancel(instant.client, run_id).json()["status"] == "cancelled"
    events_after_first = [(e.id, e.kind) for e in _list_events(run_id)]
    assert sum(1 for _, k in events_after_first if k == CANCEL_EVENT) == 1

    for _ in range(3):
        r = _cancel(instant.client, run_id)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    # Event log is byte-for-byte unchanged after the repeats: exactly one cancel,
    # no new events, projection stable.
    assert [(e.id, e.kind) for e in _list_events(run_id)] == events_after_first
    assert _completed_event_count(run_id) == 0
    assert _detail(instant.client, run_id).json()["status"] == "cancelled"


# --------------------------------------------------------------------------
# Race E — a wrong-tenant cancel racing the run still cannot touch it: the
# ownership decision is made INSIDE the atomic section, so a denied caller
# writes nothing (404) and the rightful owner's later cancel is unaffected.
# --------------------------------------------------------------------------


def test_wrong_tenant_cancel_under_race_writes_nothing(instant):
    runtime.stop_dispatcher()
    run_id = _seed_task(tenant="tenantA", user="userA", initial_status="running")

    # Concurrent wrong-tenant + wrong-user attempts; both must 404 and mutate
    # nothing (no cancel event, no terminal transition).
    results = {}

    def _wrong(name, tenant, user):
        r = _cancel(instant.client, run_id, tenant=tenant, user=user)
        results[name] = r.status_code

    a = threading.Thread(target=_wrong, args=("tenant", "tenantB", "userA"))
    b = threading.Thread(target=_wrong, args=("user", "tenantA", "userB"))
    a.start()
    b.start()
    a.join(10)
    b.join(10)

    assert results["tenant"] == 404
    assert results["user"] == 404
    # The security-relevant invariant: the denied attempts wrote nothing. No
    # cancel intent was recorded and the run is NOT projected cancelled (its
    # exact non-terminal kanban status is immaterial and left untouched).
    assert _cancel_event_count(run_id) == 0
    assert _get_task(run_id).status not in ("done", "archived", "blocked")
    assert _detail(instant.client, run_id).json()["status"] != "cancelled"

    # The rightful owner can still cancel, exactly once.
    assert _cancel(instant.client, run_id).json()["status"] == "cancelled"
    assert _cancel_event_count(run_id) == 1
