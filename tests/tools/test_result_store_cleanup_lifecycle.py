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
