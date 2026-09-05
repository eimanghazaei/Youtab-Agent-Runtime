"""Ownership-proof tests for the WAVE-26 harness process controller (agent 4).

These tests operate exclusively on harness-created child processes rooted in an
isolated ``tmp_path`` home, so they are safe to run anywhere — nothing here can
touch a real Youtab install. The harness's process primitives (start-time guard,
no-kill liveness probe, advisory lock) are cross-platform by construction, so no
test is skipped by platform; a genuinely POSIX-only capability would be guarded
via ``tests/_wincompat.py`` rather than a blanket ``skipif``.
"""

from __future__ import annotations

import json

import pytest

import youtab_runtime.harness_process as hp
from youtab_runtime.harness_process import (
    HarnessFaultDisabled,
    HarnessOwnershipError,
    HarnessProcess,
    HarnessUpdateExcluded,
    _child_ack_path,
    _cookie_path,
)
from youtab_runtime.run_states import OwnershipOutcome
from youtab_constants import _get_platform_default_youtab_home


@pytest.fixture()
def harness(tmp_path):
    h = HarnessProcess(isolated_benchmark=True, home=tmp_path / "home")
    try:
        yield h
    finally:
        h.close()


def _launch_ready_idle(h: HarnessProcess) -> None:
    h.launch(mode="idle")
    assert h.wait_ready(timeout=30), "child never reached readiness"


# ── construction / bypass guards ─────────────────────────────────────────────
def test_construction_requires_isolated_benchmark_flag():
    # There is no env var that enables the harness; the flag is in-code only.
    with pytest.raises(HarnessFaultDisabled):
        HarnessProcess()


def test_refuses_real_platform_home():
    with pytest.raises(hp.HarnessError):
        HarnessProcess(isolated_benchmark=True, home=_get_platform_default_youtab_home())


def test_is_real_platform_home_predicate(tmp_path):
    assert hp._is_real_platform_home(_get_platform_default_youtab_home()) is True
    assert hp._is_real_platform_home(tmp_path / "home") is False


@pytest.mark.parametrize(
    "command",
    [
        ["youtab", "update"],
        ["youtab.exe", "update", "--force"],
        ["python", "-m", "youtab_agent_cli.main", "update"],
    ],
)
def test_update_command_is_hard_excluded(harness, command):
    # youtab update runs git reset --hard on the real checkout (RECON R4 HAZARD).
    with pytest.raises(HarnessUpdateExcluded):
        harness.launch(command=command)


# ── ownership proof ──────────────────────────────────────────────────────────
def test_owned_child_proves_owned_and_is_killable(harness):
    _launch_ready_idle(harness)
    assert harness.prove_ownership() is OwnershipOutcome.OWNED
    assert harness.kill(force=True) is OwnershipOutcome.OWNED
    assert harness.wait_exit(15)
    assert not harness.is_alive()


def test_pid_reuse_yields_not_owned_and_refuses_kill(harness):
    # Simulate PID reuse: same pid, different start_time in the on-disk cookie.
    _launch_ready_idle(harness)
    cookie_path = _cookie_path(harness.home)
    cookie = json.loads(cookie_path.read_text(encoding="utf-8"))
    cookie["start_time"] = (cookie["start_time"] or 0) + 10_000_000
    cookie_path.write_text(json.dumps(cookie), encoding="utf-8")

    assert harness.prove_ownership() is OwnershipOutcome.NOT_OWNED
    with pytest.raises(HarnessOwnershipError) as exc:
        harness.kill(force=True)
    assert exc.value.outcome is OwnershipOutcome.NOT_OWNED
    # Child must still be alive: the refusal was real, not a silent kill.
    assert harness.is_alive()


def test_wrong_launch_token_yields_not_owned(harness):
    _launch_ready_idle(harness)
    cookie_path = _cookie_path(harness.home)
    cookie = json.loads(cookie_path.read_text(encoding="utf-8"))
    cookie["launch_token"] = "0" * 32
    cookie_path.write_text(json.dumps(cookie), encoding="utf-8")
    assert harness.prove_ownership() is OwnershipOutcome.NOT_OWNED


def test_child_ack_token_mismatch_yields_not_owned(harness):
    # A mismatched child ack (mutual-binding failure) is fatal even if the cookie
    # and pid line up — the running process did not actually receive our token.
    _launch_ready_idle(harness)
    ack_path = _child_ack_path(harness.home)
    ack = json.loads(ack_path.read_text(encoding="utf-8"))
    ack["launch_token"] = "deadbeef" * 4
    ack_path.write_text(json.dumps(ack), encoding="utf-8")
    assert harness.prove_ownership() is OwnershipOutcome.NOT_OWNED


def test_ambiguous_start_time_refuses_kill_fail_closed(harness, monkeypatch):
    # When start_time cannot be determined we cannot disprove PID reuse, so
    # ownership is AMBIGUOUS and a kill must be refused (fail closed).
    _launch_ready_idle(harness)
    monkeypatch.setattr(hp, "get_process_start_time", lambda pid: None)
    assert harness.prove_ownership() is OwnershipOutcome.AMBIGUOUS
    with pytest.raises(HarnessOwnershipError) as exc:
        harness.kill(force=True)
    assert exc.value.outcome is OwnershipOutcome.AMBIGUOUS
    assert harness.is_alive()


def test_stale_dead_handle_is_not_owned_and_kill_is_noop(harness):
    _launch_ready_idle(harness)
    pid = harness.pid
    # Owned kill, then the pid is a stale/dead handle.
    assert harness.kill(force=True) is OwnershipOutcome.OWNED
    assert harness.wait_exit(15)
    # Cookie still references the now-dead pid.
    assert harness.prove_ownership(pid) is OwnershipOutcome.NOT_OWNED
    # kill() on the dead handle is a safe no-op (no raise, no signal).
    assert harness.kill(force=True, pid=pid) is OwnershipOutcome.NOT_OWNED


def test_never_claims_a_foreign_live_process(tmp_path):
    # Two independent harness-owned children. Neither controller may claim the
    # other's live pid — proving ownership is by token/cookie/start_time, never a
    # cmdline scan that could match an unrelated process.
    a = HarnessProcess(isolated_benchmark=True, home=tmp_path / "a")
    b = HarnessProcess(isolated_benchmark=True, home=tmp_path / "b")
    try:
        a.launch(mode="idle")
        b.launch(mode="idle")
        assert a.wait_ready(timeout=30)
        assert b.wait_ready(timeout=30)
        # A cannot prove ownership of B's pid (cookie pid mismatch).
        assert a.prove_ownership(b.pid) is OwnershipOutcome.NOT_OWNED
        with pytest.raises(HarnessOwnershipError):
            a.kill(force=True, pid=b.pid)
        # B is untouched and still alive.
        assert b.is_alive()
    finally:
        a.close()
        b.close()


def test_only_the_owned_child_is_killed(tmp_path):
    a = HarnessProcess(isolated_benchmark=True, home=tmp_path / "a")
    b = HarnessProcess(isolated_benchmark=True, home=tmp_path / "b")
    try:
        a.launch(mode="idle")
        b.launch(mode="idle")
        assert a.wait_ready(timeout=30)
        assert b.wait_ready(timeout=30)
        assert a.kill(force=True) is OwnershipOutcome.OWNED
        assert a.wait_exit(15)
        assert not a.is_alive()
        # Killing A left B completely alone.
        assert b.is_alive()
    finally:
        a.close()
        b.close()
