"""R7: PID-incarnation-safe worker termination (WAVE-30H).

The OS recycles PID numbers. Terminating a bare PID can kill an innocent process
that inherited the number after the worker exited. These tests prove the reclaim/
terminate path verifies the recorded (pid+start-time) incarnation before sending
ANY signal, and that liveness is incarnation-aware.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from youtab_agent_cli import kanban_db as kb
from youtab_runtime import process_incarnation as pi


@pytest.fixture()
def host_lock():
    # A claim lock owned by THIS host, so the host-local guard passes.
    return kb._claimer_id()


def _spy():
    calls = []
    def fn(pid, sig):
        calls.append((int(pid), int(sig)))
    return fn, calls


# ── the harm case: a recycled PID must never be signalled ────────────────────


def test_incarnation_mismatch_never_signals(host_lock):
    # This process is alive, but the recorded incarnation claims a different
    # start time (as a recycled PID would). Must NOT signal, and must report the
    # original worker as gone.
    fn, calls = _spy()
    forged = f"{os.getpid()}:{(pi.process_start_time(os.getpid()) or 0) + 987654}"
    info = kb._terminate_reclaimed_worker(
        os.getpid(), host_lock, signal_fn=fn, incarnation=forged
    )
    assert calls == []  # the innocent process was never signalled
    assert info["incarnation_mismatch"] is True
    assert info["terminated"] is True
    assert info["termination_attempted"] is False


def test_dead_pid_never_signals(host_lock):
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    proc.wait()
    fn, calls = _spy()
    info = kb._terminate_reclaimed_worker(
        proc.pid, host_lock, signal_fn=fn, incarnation=f"{proc.pid}:123"
    )
    assert calls == []
    assert info["terminated"] is True


def test_matching_incarnation_terminates_real_worker(host_lock):
    # A real child; verified incarnation matches -> it IS signalled and dies.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for _ in range(50):
            if pi.pid_exists(proc.pid):
                break
            time.sleep(0.02)
        token = pi.incarnation_token(proc.pid)
        info = kb._terminate_reclaimed_worker(
            proc.pid, host_lock, incarnation=token  # real os.kill
        )
        assert info["incarnation_verified"] is True
        assert info["terminated"] is True
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()


def test_legacy_no_incarnation_falls_back_to_pid(host_lock):
    # No recorded incarnation (pre-R7 row): the pid-only path is used, so the
    # spy is signalled (SIGTERM) — behaviour preserved for old rows.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for _ in range(50):
            if pi.pid_exists(proc.pid):
                break
            time.sleep(0.02)
        info = kb._terminate_reclaimed_worker(
            proc.pid, host_lock, incarnation=None  # real os.kill terminates it
        )
        assert info["termination_attempted"] is True
        assert info["terminated"] is True
        assert info["incarnation_verified"] is None
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()


def test_non_host_local_lock_is_untouched():
    fn, calls = _spy()
    info = kb._terminate_reclaimed_worker(
        os.getpid(), "someotherhost-1234:abc", signal_fn=fn, incarnation="x"
    )
    assert calls == []
    assert info["host_local"] is False


# ── incarnation-aware liveness ───────────────────────────────────────────────


def test_worker_incarnation_alive_matches_self():
    assert kb._worker_incarnation_alive(os.getpid(), pi.current_incarnation()) is True


def test_worker_incarnation_alive_rejects_recycled_pid():
    forged = f"{os.getpid()}:{(pi.process_start_time(os.getpid()) or 0) + 987654}"
    assert kb._worker_incarnation_alive(os.getpid(), forged) is False


def test_worker_incarnation_alive_legacy_falls_back_to_pid_alive():
    # No incarnation recorded -> pid-only liveness (this process is alive).
    assert kb._worker_incarnation_alive(os.getpid(), None) is True


def test_worker_incarnation_alive_dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    proc.wait()
    assert kb._worker_incarnation_alive(proc.pid, f"{proc.pid}:1") is False
