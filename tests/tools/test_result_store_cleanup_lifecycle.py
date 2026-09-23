"""Lifecycle proof (ADR-0005 §7a): the result-store scope cleanup is invoked by
the REAL environment teardown path, not left as an orphan helper.

Both teardown entrypoints in ``tools.terminal_tool`` — the idle reaper
``_cleanup_inactive_envs`` and the explicit ``cleanup_vm`` — must call
``tool_result_storage.cleanup_run_scope(env)`` before the environment is torn
down, so a run's spilled tool results are removed with the run.
"""

from __future__ import annotations

import time

from tools import terminal_tool as tt
from tools import tool_result_storage as trs


class _FakeEnv:
    def __init__(self, session_id="sess-teardown"):
        self._session_id = session_id
        self.cleaned = False

    @property
    def session_scope(self):
        return self._session_id

    def cleanup(self):
        self.cleaned = True


def _register(task_id, env, *, stale=False):
    with tt._env_lock:
        tt._active_environments[task_id] = env
        tt._last_activity[task_id] = 0 if stale else time.time()


def test_cleanup_vm_invokes_scope_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(trs, "cleanup_run_scope", lambda env: calls.append(env) or True)
    env = _FakeEnv("sess-vm-1")
    _register("task-cleanup-vm", env)
    try:
        tt.cleanup_vm("task-cleanup-vm")
    finally:
        with tt._env_lock:
            tt._active_environments.pop("task-cleanup-vm", None)
            tt._last_activity.pop("task-cleanup-vm", None)
    assert env in calls  # scope cleanup ran as part of teardown
    assert env.cleaned is True  # and the env itself was still torn down


def test_idle_reaper_invokes_scope_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(trs, "cleanup_run_scope", lambda env: calls.append(env) or True)
    env = _FakeEnv("sess-idle-1")
    _register("task-cleanup-idle", env, stale=True)
    try:
        tt._cleanup_inactive_envs(lifetime_seconds=0)  # everything is stale
    finally:
        with tt._env_lock:
            tt._active_environments.pop("task-cleanup-idle", None)
            tt._last_activity.pop("task-cleanup-idle", None)
    assert env in calls
    assert env.cleaned is True


def test_run_scope_uses_public_accessor():
    # The scope is read from the public ``session_scope`` accessor, not the
    # private ``_session_id`` of an unrelated module.
    class _Env:
        session_scope = "pub-scope-01"
        _session_id = "SHOULD-NOT-BE-USED"

    assert trs._run_scope(_Env()) == "run-pub-scope-01"


# ── cleanup lifecycle hardening (item 4) ─────────────────────────────────────


def test_teardown_survives_scope_cleanup_raising(monkeypatch):
    """If scope cleanup THROWS, environment teardown still completes."""
    def _boom(env):
        raise RuntimeError("scope cleanup exploded")
    monkeypatch.setattr(trs, "cleanup_run_scope", _boom)
    env = _FakeEnv("sess-raise-1")
    _register("task-cleanup-raise", env)
    try:
        tt.cleanup_vm("task-cleanup-raise")
    finally:
        with tt._env_lock:
            tt._active_environments.pop("task-cleanup-raise", None)
            tt._last_activity.pop("task-cleanup-raise", None)
    assert env.cleaned is True  # env.cleanup() still ran despite the cleanup error


def test_teardown_survives_scope_cleanup_returning_false(monkeypatch):
    monkeypatch.setattr(trs, "cleanup_run_scope", lambda env: False)
    env = _FakeEnv("sess-false-1")
    _register("task-cleanup-false", env, stale=True)
    try:
        tt._cleanup_inactive_envs(lifetime_seconds=0)
    finally:
        with tt._env_lock:
            tt._active_environments.pop("task-cleanup-false", None)
            tt._last_activity.pop("task-cleanup-false", None)
    assert env.cleaned is True


class _RecordingEnv:
    """Records the shell command + kwargs cleanup_run_scope issues."""

    def __init__(self, session_id="sess-rec", temp_dir="/sandbox/tmp"):
        self._session_id = session_id
        self._temp_dir = temp_dir
        self.calls = []

    @property
    def session_scope(self):
        return self._session_id

    def get_temp_dir(self):
        return self._temp_dir

    def execute(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return {"output": "", "returncode": 0}


def test_scope_cleanup_uses_bounded_timeout_and_no_process_kill():
    env = _RecordingEnv("sess-bounded-1")
    assert trs.cleanup_run_scope(env) is True
    cmd, kwargs = env.calls[-1]
    # bounded: an explicit timeout is always passed to the backend
    assert kwargs.get("timeout") and kwargs["timeout"] > 0
    # deletes only the run scope; never a process kill (rm only, no kill/taskkill)
    assert "rm -rf -- /sandbox/tmp/youtab-results/run-sess-bounded-1" in cmd
    assert "kill" not in cmd and "taskkill" not in cmd
