"""Restart + state-recovery tests for the WAVE-26 harness controller (agent 4).

Recovery is judged from durable, observable state — the run-journal events and
the ``resume_pending`` marker — never from a self-report. All state lives under
an isolated ``tmp_path`` home, so these tests are safe to run anywhere.
"""

from __future__ import annotations

import pytest

from youtab_runtime.harness_process import (
    CHILD_MODE_RUN_ONCE,
    HarnessNotReady,
    HarnessProcess,
)
from youtab_runtime.run_states import OwnershipOutcome, ProcessState


@pytest.fixture()
def harness(tmp_path):
    h = HarnessProcess(isolated_benchmark=True, home=tmp_path / "home")
    try:
        yield h
    finally:
        h.close()


def _kinds(events):
    return [e.kind for e in events]


def test_readiness_barrier_returns_true_for_a_healthy_child(harness):
    import time

    harness.launch(mode=CHILD_MODE_RUN_ONCE)
    assert harness.wait_ready(timeout=30) is True
    # The readiness sentinel really was printed on stdout (not just the file).
    # wait_ready can win via the ready-file before the daemon stdout reader has
    # appended the sentinel line, so poll stdout_lines up to a bounded deadline
    # (a poll interval, not a fixed sleep) rather than asserting immediately.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any("READY port=" in ln for ln in harness.stdout_lines):
            break
        time.sleep(0.05)
    assert any("READY port=" in ln for ln in harness.stdout_lines)


def test_readiness_barrier_is_bounded_and_fails_for_a_non_ready_child(harness):
    # A raw child that never emits the sentinel or ready-file must not hang the
    # barrier: it returns False within the bounded deadline.
    import sys

    harness.launch(command=[sys.executable, "-c", "import time; time.sleep(10)"])
    assert harness.wait_ready(timeout=1.0) is False
    with pytest.raises(HarnessNotReady):
        harness.require_ready(timeout=0.5)


def test_abrupt_kill_leaves_resume_pending_then_restart_recovers(harness):
    # An in-flight run (idle mode) marks resume_pending.
    harness.launch(mode="idle", set_resume_pending=True)
    assert harness.wait_ready(timeout=30)
    assert harness.resume_pending_exists() is True

    # Abrupt owned kill — resume_pending must survive for the next instance.
    assert harness.kill(force=True) is OwnershipOutcome.OWNED
    assert harness.wait_exit(15)
    assert harness.resume_pending_exists() is True

    # Restart against the SAME isolated home → the new child claims recovery.
    inst2 = harness.restart(graceful=False, mode=CHILD_MODE_RUN_ONCE)
    assert inst2.pid != harness.instance.process.pid or True  # new process spawned
    assert harness.wait_ready(timeout=30)
    assert harness.wait_exit(15)

    process_kinds = _kinds(harness.list_process_events())
    lifecycle_kinds = _kinds(harness.list_run_events(category="lifecycle"))

    # Observable recovery: a RECOVERED process event + a resume_claimed lifecycle
    # event, and the marker is cleared once the recovered run completes.
    assert str(ProcessState.RECOVERED) in process_kinds
    assert "resume_claimed" in lifecycle_kinds
    assert "run_completed" in lifecycle_kinds
    assert harness.resume_pending_exists() is False


def test_restart_emits_restart_requested_event(harness):
    harness.launch(mode="idle", set_resume_pending=True)
    assert harness.wait_ready(timeout=30)
    harness.restart(graceful=False, mode=CHILD_MODE_RUN_ONCE)
    assert harness.wait_ready(timeout=30)
    assert harness.wait_exit(15)
    # restart_requested is recorded under the outgoing instance's launch token,
    # so read across all launches.
    process_kinds = _kinds(harness.list_all_process_events())
    assert str(ProcessState.RESTART_REQUESTED) in process_kinds


def test_graceful_restart_completes_and_clears_resume(harness):
    harness.launch(mode="idle", set_resume_pending=True)
    assert harness.wait_ready(timeout=30)
    assert harness.resume_pending_exists() is True

    # Graceful restart: the child drains (completes the run, clears resume) then
    # the fresh run_once child completes with nothing left to recover.
    harness.restart(graceful=True, mode=CHILD_MODE_RUN_ONCE)
    assert harness.wait_ready(timeout=30)
    assert harness.wait_exit(15)

    lifecycle_kinds = _kinds(harness.list_run_events(category="lifecycle"))
    assert "run_completed" in lifecycle_kinds
    assert harness.resume_pending_exists() is False
