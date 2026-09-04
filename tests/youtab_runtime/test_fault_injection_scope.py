"""Fault-injection scope + gating tests for the WAVE-26 harness (agent 4).

Faults are contained by construction: every launch gets its own isolated home
and its own run-journal DB, so a fault recorded for one owned child can never
appear in another's journal. Fault injection is enabled only via the in-code
``isolated_benchmark=True`` flag — there is no production environment bypass.
"""

from __future__ import annotations

import pytest

from youtab_runtime.harness_process import (
    CHILD_MODE_RUN_ONCE,
    HarnessFaultDisabled,
    HarnessProcess,
)
from youtab_constants import _get_platform_default_youtab_home


@pytest.fixture()
def harness(tmp_path):
    h = HarnessProcess(isolated_benchmark=True, home=tmp_path / "home")
    try:
        yield h
    finally:
        h.close()


def _kinds(events):
    return [e.kind for e in events]


# ── gating: no production bypass ─────────────────────────────────────────────
def test_fault_injection_disabled_without_isolated_flag():
    # The only way to obtain a harness (and thus fault injection) is the in-code
    # flag; there is deliberately no env var that enables it.
    with pytest.raises(HarnessFaultDisabled):
        HarnessProcess()


def test_harness_refuses_real_platform_home():
    # The child-side guard mirrors this: it aborts before touching a real home.
    with pytest.raises(Exception):
        HarnessProcess(isolated_benchmark=True, home=_get_platform_default_youtab_home())


# ── provider / tool faults are observable and scoped ─────────────────────────
def test_tool_error_fault_is_recorded_in_the_owned_childs_journal(harness):
    harness.launch(
        mode=CHILD_MODE_RUN_ONCE,
        fault_plan=[{"type": "tool_error", "tool_name": "web_fetch"}],
    )
    assert harness.wait_ready(timeout=30)
    assert harness.wait_exit(15)

    tool_results = harness.list_run_events(category="tool_result")
    assert tool_results, "expected an injected tool_result event"
    statuses = [e.payload.get("status") for e in tool_results]
    assert "error" in statuses


def test_provider_timeout_fault_records_unknown_usage_not_zero(harness):
    harness.launch(
        mode=CHILD_MODE_RUN_ONCE,
        fault_plan=[{"type": "provider_timeout"}],
    )
    assert harness.wait_ready(timeout=30)
    assert harness.wait_exit(15)

    usage = harness.list_run_events(category="usage")
    assert usage, "expected an injected usage event"
    ev = usage[0]
    # A provider timeout must be recorded as unknown, never a fabricated zero.
    assert ev.payload.get("usage_status") == "unknown"
    assert ev.payload.get("input_tokens") is None
    assert ev.payload.get("cost", {}).get("status") == "unknown"


def test_faults_only_affect_the_owned_child(tmp_path):
    # Child A gets a tool fault; child B (a separate owned instance) gets none.
    # B's journal must be entirely free of the fault — isolation by construction.
    a = HarnessProcess(isolated_benchmark=True, home=tmp_path / "a")
    b = HarnessProcess(isolated_benchmark=True, home=tmp_path / "b")
    try:
        a.launch(mode=CHILD_MODE_RUN_ONCE,
                 fault_plan=[{"type": "tool_error", "tool_name": "poisoned"}])
        b.launch(mode=CHILD_MODE_RUN_ONCE)
        assert a.wait_ready(timeout=30)
        assert b.wait_ready(timeout=30)
        assert a.wait_exit(15)
        assert b.wait_exit(15)

        assert a.list_run_events(category="tool_result"), "A should have the fault"
        assert b.list_run_events(category="tool_result") == [], "B must be clean"
        # And B's run completed normally.
        assert "run_completed" in _kinds(b.list_run_events(category="lifecycle"))
    finally:
        a.close()
        b.close()


# ── event interruption leaves recoverable state ──────────────────────────────
def test_event_interruption_leaves_resume_pending_and_no_completion(harness):
    harness.launch(
        mode=CHILD_MODE_RUN_ONCE,
        set_resume_pending=True,
        fault_plan=[{"type": "event_interruption", "exit_code": 42}],
    )
    assert harness.wait_exit(15)
    # The child died mid-run: nonzero exit, resume marker still set, no completion.
    assert harness.instance.process.poll() == 42
    assert harness.resume_pending_exists() is True
    lifecycle_kinds = _kinds(harness.list_run_events(category="lifecycle"))
    assert "run_failed" in lifecycle_kinds
    assert "run_completed" not in lifecycle_kinds

    # And the interruption is recoverable on restart.
    harness.restart(graceful=False, mode=CHILD_MODE_RUN_ONCE)
    assert harness.wait_ready(timeout=30)
    assert harness.wait_exit(15)
    assert "resume_claimed" in _kinds(harness.list_run_events(category="lifecycle"))
    assert harness.resume_pending_exists() is False


# ── cleanup after failure ────────────────────────────────────────────────────
def test_context_manager_reaps_child_even_after_a_fault(tmp_path):
    leaked_pid = None
    with HarnessProcess(isolated_benchmark=True, home=tmp_path / "home") as h:
        h.launch(mode="idle", set_resume_pending=True)
        assert h.wait_ready(timeout=30)
        leaked_pid = h.pid
        assert h.is_alive()
        # Exit the context WITHOUT explicitly killing — close() must reap it.
    from youtab_runtime.harness_process import _pid_exists

    # Give the OS a moment; the owned child must be gone after close().
    import time

    for _ in range(100):
        if not _pid_exists(leaked_pid):
            break
        time.sleep(0.05)
    assert not _pid_exists(leaked_pid), "close() must reap the owned child"


def test_owned_home_is_removed_on_close(tmp_path):
    # When the harness created its own temp home, close() removes it.
    h = HarnessProcess(isolated_benchmark=True)  # auto temp home
    created_home = h.home
    assert created_home.exists()
    h.launch(mode=CHILD_MODE_RUN_ONCE)
    assert h.wait_ready(timeout=30)
    assert h.wait_exit(15)
    h.close()
    assert not created_home.exists()
