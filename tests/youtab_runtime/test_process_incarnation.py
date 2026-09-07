"""PID-incarnation identity primitive (WAVE-30H R7 core)."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from youtab_runtime import process_incarnation as pi


def test_current_process_is_alive_under_its_own_token():
    tok = pi.current_incarnation()
    assert tok.startswith(f"{os.getpid()}:")
    assert pi.same_incarnation(os.getpid(), tok) is True
    assert pi.is_alive_incarnation(os.getpid(), tok) is True


def test_start_time_is_stable_across_reads():
    a = pi.process_start_time(os.getpid())
    time.sleep(0.05)
    b = pi.process_start_time(os.getpid())
    assert a is not None and a == b


def test_dead_pid_is_not_alive():
    # Spawn a child, capture its token, let it exit, then assert it reads dead.
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    proc.wait()
    tok = f"{proc.pid}:12345"  # any token; the pid is gone
    assert pi.pid_exists(proc.pid) is False
    assert pi.same_incarnation(proc.pid, tok) is False
    assert pi.is_alive_incarnation(proc.pid, tok) is False


def test_recycled_pid_with_different_start_time_is_rejected():
    # A live process (this one) but a token claiming a DIFFERENT start time must
    # not be accepted as the same incarnation — this is the PID-reuse guard.
    real = pi.process_start_time(os.getpid())
    forged = f"{os.getpid()}:{(real or 0) + 999999}"
    assert pi.same_incarnation(os.getpid(), forged) is False


def test_unknown_start_time_token_never_matches():
    assert pi.same_incarnation(os.getpid(), f"{os.getpid()}:?") is False
    assert pi.is_alive_incarnation(os.getpid(), None) is False
    assert pi.is_alive_incarnation(None, "1:2") is False


def test_live_child_matches_its_own_token_then_dies():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # give it a moment to appear
        for _ in range(50):
            if pi.pid_exists(proc.pid):
                break
            time.sleep(0.02)
        tok = pi.incarnation_token(proc.pid)
        assert pi.same_incarnation(proc.pid, tok) is True
    finally:
        proc.kill()
        proc.wait()
    assert pi.same_incarnation(proc.pid, tok) is False  # dead now
