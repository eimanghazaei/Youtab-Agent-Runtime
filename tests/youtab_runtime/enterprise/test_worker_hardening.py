"""Hardening proofs for the enterprise worker boundary.

Covers process-tree containment (a timeout kills the COMPLETE tree, no orphan
grandchild), lease heartbeat/atomic renewal, lease-loss fail-closed settlement,
the bounded attempt ceiling (dead-letter rather than loop), and re-verification
of the existing boundary guarantees (shell=False, scrubbed env, bounded frames,
strict envelope, no forged receipt). All Lane-1 calls go through the real
``worker_lease`` / ``effect_ledger`` modules; no Lane-1 file is modified.
"""

from __future__ import annotations

import os
import sys
import time

import psutil
import pytest

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime import worker_lease as _lease
from youtab_runtime.enterprise.worker_boundary import (
    LeaseHeartbeat,
    SettlementError,
    WorkerBoundary,
    WorkerBoundaryError,
    WorkerCrash,
    WorkerMalformed,
    WorkerRefused,
    WorkerResult,
    WorkerTimeout,
    settle_with_lease,
    win32_job_available,
)
from youtab_runtime.run_journal import Principal
from youtab_runtime.run_states import EffectState

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "run_journal.db"


@pytest.fixture()
def principal():
    return Principal("tenant-a", "user-a")


# --------------------------------------------------------------------------- #
# 1. Process-tree containment: a timeout kills the grandchild too              #
# --------------------------------------------------------------------------- #
# A worker that, AFTER reading its request, spawns a detached grandchild which
# would outlive a naive single-child kill. The grandchild records its PID then
# heartbeats a file forever; the worker then blocks. A naive ``proc.kill()``
# leaves the grandchild orphaned — tree containment must reap it.
_GRANDCHILD = (
    "import sys,time,os\n"
    "pid_path=os.environ['ORPHAN_PIDFILE']\n"
    "hb_path=os.environ['ORPHAN_HBFILE']\n"
    "open(pid_path,'w').write(str(os.getpid()))\n"
    "while True:\n"
    "    open(hb_path,'a').write('x')\n"
    "    time.sleep(0.15)\n"
)
_WORKER_SPAWNS_GRANDCHILD = (
    "import sys,os,subprocess,time\n"
    "sys.stdin.read()\n"  # read request first, so job assignment wins the race
    "subprocess.Popen([sys.executable,'-c',os.environ['ORPHAN_GC']])\n"
    "time.sleep(60)\n"  # block well past the deadline
)


def test_timeout_kills_process_tree_no_orphan(tmp_path):
    pidfile = tmp_path / "gc.pid"
    hbfile = tmp_path / "gc.hb"
    worker = WorkerBoundary(
        python_argv=[sys.executable, "-c", _WORKER_SPAWNS_GRANDCHILD],
        repo_root=REPO_ROOT,
        env_overrides={
            "ORPHAN_GC": _GRANDCHILD,
            "ORPHAN_PIDFILE": str(pidfile),
            "ORPHAN_HBFILE": str(hbfile),
        },
    )
    with pytest.raises(WorkerTimeout):
        worker.run({"any": "request"}, deadline_seconds=2.0)

    # The grandchild must have started (so this is a real containment proof).
    deadline = time.time() + 5.0
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert pidfile.exists(), "grandchild never started; test is not exercising containment"
    gc_pid = int(pidfile.read_text().strip())

    # Give any surviving orphan time to keep heartbeating, then assert it is gone.
    time.sleep(1.5)
    orphans = [
        p for p in (_maybe_proc(gc_pid),)
        if p is not None and p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    ]
    # Best-effort reap of a leaked orphan so the test host is left clean.
    for p in orphans:
        try:
            p.kill()
        except psutil.Error:
            pass
    assert len(orphans) == 0, f"orphan grandchild {gc_pid} survived the tree kill"

    # Independent corroboration: the heartbeat file stopped growing after kill.
    size_a = hbfile.stat().st_size if hbfile.exists() else 0
    time.sleep(0.8)
    size_b = hbfile.stat().st_size if hbfile.exists() else 0
    assert size_a == size_b, "grandchild kept writing after the tree kill"


def _maybe_proc(pid: int):
    try:
        return psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None


def test_reports_containment_primitive():
    # Documents which primitive is in force on this host (Job Object vs fallback).
    if sys.platform == "win32":
        assert isinstance(win32_job_available(), bool)
    else:
        assert win32_job_available() is False


# --------------------------------------------------------------------------- #
# 2. Lease heartbeat + atomic renewal (holder-only, rotates the token)         #
# --------------------------------------------------------------------------- #
def _inprogress_effect(db_path, principal, token, *, expires_at, run_id="run-h"):
    detail = dict(_lease.lease_detail(token, expires_at))
    detail["workspace"] = "ws-acme"
    rec = _ledger.begin_effect(
        run_id, principal, "op.commit",
        {"workspace": "ws-acme", "business_key": "bk"},
        detail=detail, db_path=db_path,
    )
    won, rec = _ledger.try_claim(rec.effect_id, principal, db_path=db_path)
    assert won and rec.state == EffectState.IN_PROGRESS
    return rec.effect_id


def test_heartbeat_renews_and_rotates_token(db_path, principal):
    now = 1_000_000.0
    eid = _inprogress_effect(db_path, principal, "tok-0", expires_at=now + 10)
    hb = LeaseHeartbeat(
        eid, principal, "tok-0", lease_seconds=30.0,
        clock=lambda: now, db_path=db_path,
    )
    new_token = hb.renew_now()
    assert new_token != "tok-0"
    assert hb.current_token == new_token

    # The stale token can no longer act; only the rotated holder can.
    with pytest.raises(_lease.LeaseError):
        _lease.assert_lease_holder(eid, principal, "tok-0", db_path=db_path)
    _lease.assert_lease_holder(eid, principal, new_token, db_path=db_path)

    # Settlement by the current holder succeeds.
    state = settle_with_lease(eid, principal, new_token, db_path=db_path)
    assert state == "committed"


def test_heartbeat_non_holder_cannot_renew(db_path, principal):
    now = 1_000_000.0
    eid = _inprogress_effect(db_path, principal, "real", expires_at=now + 10)
    imposter = LeaseHeartbeat(
        eid, principal, "imposter", lease_seconds=30.0,
        clock=lambda: now, db_path=db_path,
    )
    with pytest.raises(_lease.LeaseError):
        imposter.renew_now()


def test_heartbeat_background_thread_renews(db_path, principal):
    now = [1_000_000.0]
    eid = _inprogress_effect(db_path, principal, "bg-0", expires_at=now[0] + 5)
    hb = LeaseHeartbeat(
        eid, principal, "bg-0", lease_seconds=60.0,
        clock=lambda: now[0], db_path=db_path,
    )
    hb.start(interval_seconds=0.05)
    try:
        time.sleep(0.3)
    finally:
        hb.stop()
    # The token rotated at least once and the current holder can still settle.
    assert hb.current_token != "bg-0"
    _lease.assert_lease_holder(eid, principal, hb.current_token, db_path=db_path)


# --------------------------------------------------------------------------- #
# 3. Lease loss prevents settlement (fail closed)                              #
# --------------------------------------------------------------------------- #
def test_lease_lost_via_sweep_blocks_settlement(db_path, principal):
    now = 2_000_000.0
    eid = _inprogress_effect(db_path, principal, "tok-A", expires_at=now - 1)
    # Sweep the expired lease to reconciliation_required (attempt below ceiling).
    swept = _lease.sweep_expired_leases(
        "run-h", principal, now=now, max_attempts=3, db_path=db_path
    )
    assert swept == {"reconciled": 1, "dead_lettered": 0}
    rec = _ledger.get_effect(eid, principal, db_path=db_path)
    assert rec.state == EffectState.RECONCILIATION_REQUIRED

    # The holder token is unchanged in the detail, but the effect left
    # in_progress -> settlement must fail closed.
    with pytest.raises(SettlementError):
        settle_with_lease(eid, principal, "tok-A", db_path=db_path)
    # And it stays non-committed.
    rec2 = _ledger.get_effect(eid, principal, db_path=db_path)
    assert rec2.state == EffectState.RECONCILIATION_REQUIRED


def test_settlement_refused_for_non_holder(db_path, principal):
    now = 2_000_000.0
    eid = _inprogress_effect(db_path, principal, "tok-real", expires_at=now + 100)
    with pytest.raises(SettlementError):
        settle_with_lease(eid, principal, "tok-wrong", db_path=db_path)
    assert _ledger.get_effect(eid, principal, db_path=db_path).state == EffectState.IN_PROGRESS


# --------------------------------------------------------------------------- #
# 4. Bounded attempt ceiling: dead-letter instead of looping                   #
# --------------------------------------------------------------------------- #
def test_reacquire_ceiling_dead_letters(db_path, principal):
    now = 3_000_000.0
    eid = _inprogress_effect(db_path, principal, "w1", expires_at=now - 1)
    # attempt starts at 1; ceiling 2. First reacquire wins (attempt -> 2).
    r1 = _lease.reacquire_expired_lease(
        eid, principal, "w2", now=now, new_expires_at=now - 1,  # already expired
        max_attempts=2, db_path=db_path,
    )
    assert r1 == {"reacquired": True, "attempt": 2, "dead_lettered": False}
    # Second reacquire hits the ceiling -> dead-letter, not another loop.
    r2 = _lease.reacquire_expired_lease(
        eid, principal, "w3", now=now, new_expires_at=now + 100,
        max_attempts=2, db_path=db_path,
    )
    assert r2["dead_lettered"] is True and r2["reacquired"] is False
    rec = _ledger.get_effect(eid, principal, db_path=db_path)
    assert rec.state == EffectState.FAILED
    assert rec.detail.get("dead_letter") is True

    # A dead-lettered effect can never be settled.
    with pytest.raises(SettlementError):
        settle_with_lease(eid, principal, "w3", db_path=db_path)


def test_sweep_at_ceiling_dead_letters(db_path, principal):
    now = 3_000_000.0
    # Seed an effect whose lease is already at the attempt ceiling.
    detail = dict(_lease.lease_detail("t", now - 1))
    detail["lease"]["attempt"] = 3
    detail["workspace"] = "ws-acme"
    rec = _ledger.begin_effect(
        "run-dead", principal, "op.commit",
        {"workspace": "ws-acme", "business_key": "bk"},
        detail=detail, db_path=db_path,
    )
    _ledger.try_claim(rec.effect_id, principal, db_path=db_path)
    out = _lease.sweep_expired_leases(
        "run-dead", principal, now=now, max_attempts=3, db_path=db_path
    )
    assert out == {"reconciled": 0, "dead_lettered": 1}
    assert _ledger.get_effect(rec.effect_id, principal, db_path=db_path).state == EffectState.FAILED


# --------------------------------------------------------------------------- #
# 5. Existing boundary guarantees re-verified                                  #
# --------------------------------------------------------------------------- #
def _echo_worker():
    # A reference echo worker: returns the required typed envelope.
    script = (
        "import sys,json,os\n"
        "req=json.loads(sys.stdin.read())\n"
        "print(json.dumps({'ok':True,'result':{'seen_secret':os.environ.get('SECRET_X'),"
        "'echo':req.get('marker')}}))\n"
    )
    return WorkerBoundary(python_argv=[sys.executable, "-c", script], repo_root=REPO_ROOT)


def test_scrubbed_env_no_secret_leak(monkeypatch):
    monkeypatch.setenv("SECRET_X", "super-secret-value")
    res = _echo_worker().run({"marker": "m1"}, deadline_seconds=15.0)
    assert isinstance(res, WorkerResult)
    assert res.result["echo"] == "m1"
    # The scrubbed allowlist must not carry the ambient secret into the child.
    assert res.result["seen_secret"] is None


def test_bounded_request_refused_before_spawn():
    worker = _echo_worker()
    big = {"marker": "x" * 2_000_000}
    with pytest.raises(WorkerBoundaryError):
        worker.run(big, deadline_seconds=15.0)


def test_bounded_response_refused():
    script = (
        "import sys; sys.stdin.read();"
        "sys.stdout.write('{\"ok\":true,\"result\":{\"blob\":\"' + 'a'*50 + '\"}}')"
    )
    worker = WorkerBoundary(
        python_argv=[sys.executable, "-c", script], repo_root=REPO_ROOT,
        max_response_bytes=16,
    )
    with pytest.raises(WorkerMalformed):
        worker.run({}, deadline_seconds=15.0)


def test_malformed_frame_fails_closed():
    script = "import sys; sys.stdin.read(); print('not-json{{')"
    worker = WorkerBoundary(python_argv=[sys.executable, "-c", script], repo_root=REPO_ROOT)
    with pytest.raises(WorkerMalformed):
        worker.run({}, deadline_seconds=15.0)


def test_missing_ok_field_fails_closed():
    script = "import sys,json; sys.stdin.read(); print(json.dumps({'result':{}}))"
    worker = WorkerBoundary(python_argv=[sys.executable, "-c", script], repo_root=REPO_ROOT)
    with pytest.raises(WorkerMalformed):
        worker.run({}, deadline_seconds=15.0)


def test_worker_refusal_surfaces():
    script = "import sys,json; sys.stdin.read(); print(json.dumps({'ok':False,'error':'nope'}))"
    worker = WorkerBoundary(python_argv=[sys.executable, "-c", script], repo_root=REPO_ROOT)
    with pytest.raises(WorkerRefused):
        worker.run({}, deadline_seconds=15.0)


def test_nonzero_exit_is_crash():
    script = "import sys; sys.stdin.read(); sys.exit(4)"
    worker = WorkerBoundary(python_argv=[sys.executable, "-c", script], repo_root=REPO_ROOT)
    with pytest.raises(WorkerCrash):
        worker.run({}, deadline_seconds=15.0)


def test_worker_cannot_forge_top_level_receipt():
    # A worker that appends a bogus 'receipt' top-level field: it must be ignored,
    # only the typed 'result' crosses back.
    script = (
        "import sys,json; sys.stdin.read();"
        "print(json.dumps({'ok':True,'result':{'x':1},"
        "'receipt':{'state':'committed-by-worker'}}))"
    )
    worker = WorkerBoundary(python_argv=[sys.executable, "-c", script], repo_root=REPO_ROOT)
    res = worker.run({}, deadline_seconds=15.0)
    assert res.result == {"x": 1}
    assert "receipt" not in res.result


if __name__ == "__main__":  # pragma: no cover - standalone smoke run
    import inspect
    import tempfile
    import traceback
    from pathlib import Path as _Path

    class _MP:
        def __init__(self):
            self._saved = {}

        def setenv(self, k, v):
            self._saved.setdefault(k, os.environ.get(k))
            os.environ[k] = v

        def undo(self):
            for k, v in self._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        params = inspect.signature(fn).parameters
        with tempfile.TemporaryDirectory() as td:
            kwargs: dict = {}
            if "db_path" in params:
                kwargs["db_path"] = _Path(td) / "run_journal.db"
            if "principal" in params:
                kwargs["principal"] = Principal("tenant-a", "user-a")
            if "tmp_path" in params:
                kwargs["tmp_path"] = _Path(td)
            mp = _MP()
            if "monkeypatch" in params:
                kwargs["monkeypatch"] = mp
            try:
                fn(**kwargs)
                passed += 1
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {fn.__name__}")
                traceback.print_exc()
            finally:
                mp.undo()
    print(f"\nworker_hardening standalone: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
