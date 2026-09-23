"""Containment + isolation regression for the tool-result overflow store
(ADR-0005 §7a).

`tool_result_storage` is an allow-listed internal-plane writer: it spills a tool's
own oversized output into a per-run-scoped sandbox root. This suite proves the
security invariants: per-run isolation (opaque Runtime scope, not caller input),
enforced containment at the write chokepoint, shell-safe command construction,
private-permission + symlink-rejection hardening, a bounded per-result spill, and
a scope-limited cleanup that can never delete outside its own run scope.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import tools.tool_result_storage as trs
from tools.tool_result_storage import (
    _run_scope,
    _safe_result_filename,
    _spill_dir,
    _write_to_sandbox,
    cleanup_run_scope,
    maybe_persist_tool_result,
)


def _env(temp_dir="/sandbox/tmp", rc=0, session_id="sess-a"):
    env = MagicMock()
    env.get_temp_dir.return_value = temp_dir
    env.execute.return_value = {"output": "", "returncode": rc}
    env._session_id = session_id
    return env


# ── _write_to_sandbox: enforced containment + hardening at the chokepoint ────


def test_write_contained_leaf_succeeds_and_is_hardened():
    env = _env()
    ok = _write_to_sandbox("payload", "/sandbox/tmp/youtab-results/run-sess-a/abc.txt", env)
    assert ok is True
    env.execute.assert_called_once()
    cmd = env.execute.call_args[0][0]
    assert "/sandbox/tmp/youtab-results/run-sess-a/abc.txt" in cmd
    # hardening is emitted: private umask, 0700 dir, 0600 file, symlink guards
    assert "umask 077" in cmd
    assert "chmod 700" in cmd and "chmod 600" in cmd
    assert cmd.count("[ -L ") >= 3  # parent, spill dir, leaf
    # content travels via stdin only, never argv/interpolation
    assert "payload" not in cmd
    assert env.execute.call_args.kwargs.get("stdin_data") == "payload"


@pytest.mark.parametrize(
    "remote_path",
    [
        "/sandbox/tmp/youtab-results/run-x/..",
        "/sandbox/tmp/youtab-results/run-x/.",
        "/sandbox/tmp/youtab-results/run-x/",
    ],
)
def test_write_rejects_unsafe_leaf_fails_closed(remote_path):
    env = _env()
    ok = _write_to_sandbox("payload", remote_path, env)
    assert ok is False
    env.execute.assert_not_called()  # zero side effect on violation


def test_trusted_temp_root_with_spaces_and_metachars_is_quoted():
    env = _env(temp_dir="/sandbox/weird dir$(id)")
    content = "Z" * 5000
    maybe_persist_tool_result(
        content=content, tool_name="search_files", tool_use_id="tc1",
        env=env, threshold=0,
    )
    cmd = env.execute.call_args[0][0]
    # the metachar-laden trusted root is single-quoted, never interpolated
    assert "'/sandbox/weird dir$(id)/youtab-results/run-sess-a'" in cmd
    # no unquoted command substitution leaks
    assert "$(id)" in cmd  # present, but only inside single quotes
    assert " $(id)" not in cmd.replace("'", "")  # sanity: not executable bare


# ── per-run isolation (opaque Runtime scope, not caller input) ───────────────


def test_run_scope_prefers_env_session_id_and_is_opaque():
    env = _env(session_id="abc123def456")
    assert _run_scope(env) == "run-abc123def456"
    assert _spill_dir(env) == "/sandbox/tmp/youtab-results/run-abc123def456"


def test_run_scope_generated_when_session_id_unusable():
    env = MagicMock()
    env.get_temp_dir.return_value = "/t"
    env._session_id = object()  # not a safe string → generated opaque scope
    scope = _run_scope(env)
    assert scope.startswith("run-") and len(scope) > 8
    # cached on the env so the whole run shares one scope
    assert _run_scope(env) == scope


def test_same_tool_use_id_different_runs_are_isolated():
    env_a = _env(session_id="runA00000001")
    env_b = _env(session_id="runB00000002")
    common_id = "toolu_shared"
    maybe_persist_tool_result("Q" * 5000, "search_files", common_id, env=env_a, threshold=0)
    maybe_persist_tool_result("R" * 5000, "search_files", common_id, env=env_b, threshold=0)
    cmd_a = env_a.execute.call_args[0][0]
    cmd_b = env_b.execute.call_args[0][0]
    assert "/youtab-results/run-runA00000001/toolu_shared.txt" in cmd_a
    assert "/youtab-results/run-runB00000002/toolu_shared.txt" in cmd_b
    # neither run's write targets the other's scope (no cross-run overwrite)
    assert "run-runB00000002" not in cmd_a
    assert "run-runA00000001" not in cmd_b


def test_caller_string_cannot_select_another_scope():
    # A malicious tool_use_id containing another run's scope cannot redirect the
    # write out of THIS run's scope: it is sanitized to a single filename leaf.
    env = _env(session_id="runOWN000001")
    maybe_persist_tool_result(
        "S" * 5000, "search_files",
        tool_use_id="../run-victim/steal", env=env, threshold=0,
    )
    cmd = env.execute.call_args[0][0]
    assert "/youtab-results/run-runOWN000001/" in cmd
    # the malicious id is flattened into a harmless filename leaf inside THIS
    # run's scope — never a "run-victim" directory component, never a traversal.
    assert "/run-victim/" not in cmd
    assert "/../" not in cmd


# ── _safe_result_filename: single safe component ─────────────────────────────


@pytest.mark.parametrize(
    "raw_id",
    ["../../etc/passwd", "..", "../..", "a/b/c", r"..\..\win", "/abs/evil", "", "...",
     "normal-tool_use.id"],
)
def test_safe_result_filename_is_a_single_safe_component(raw_id):
    name = _safe_result_filename(raw_id)
    assert "/" not in name and "\\" not in name
    assert name not in ("", ".", "..")
    assert not name.startswith(".")
    assert name.endswith(".txt")


# ── bounded spill (no unbounded write) ───────────────────────────────────────


def test_oversized_spill_is_capped(monkeypatch):
    monkeypatch.setattr(trs, "MAX_SPILL_BYTES", 1000)
    env = _env()
    content = "y" * 5000  # far over the (patched) cap
    out = maybe_persist_tool_result(
        content=content, tool_name="search_files", tool_use_id="big",
        env=env, threshold=0,
    )
    assert "<persisted-output>" in out
    # only the capped prefix is written to disk (no unbounded spill)
    assert len(env.execute.call_args.kwargs.get("stdin_data")) == 1000
    # the message still reports the true original size
    assert "5,000 characters" in out


# ── scope-limited cleanup ────────────────────────────────────────────────────


def test_cleanup_removes_only_current_scope():
    env = _env(session_id="runCLEAN0001")
    ok = cleanup_run_scope(env)
    assert ok is True
    cmd = env.execute.call_args[0][0]
    # targets exactly this run's scope dir, guarded to be inside the store root
    # (paths without shell metacharacters are not quoted by shlex.quote).
    assert "rm -rf -- /sandbox/tmp/youtab-results/run-runCLEAN0001" in cmd
    assert "case /sandbox/tmp/youtab-results/run-runCLEAN0001 in /sandbox/tmp/youtab-results/*)" in cmd
    assert "[ -L /sandbox/tmp/youtab-results/run-runCLEAN0001 ]" in cmd
    # exactly one removal, and its target is precisely this run's scope dir —
    # never a blanket removal of the store root itself.
    assert cmd.count("rm -rf") == 1
    assert cmd.split("rm -rf -- ", 1)[1].strip() == "/sandbox/tmp/youtab-results/run-runCLEAN0001"


def test_cleanup_no_env_is_noop():
    assert cleanup_run_scope(None) is False


def test_cleanup_failure_returns_false_and_never_raises():
    env = _env(session_id="runFAIL00001", rc=95)  # symlink-guard exit
    assert cleanup_run_scope(env) is False


# ── normal spill behavior unchanged ──────────────────────────────────────────


def test_small_result_not_persisted_no_write():
    env = _env()
    out = maybe_persist_tool_result(
        content="tiny", tool_name="search_files", tool_use_id="toolu_x",
        env=env, threshold=1000,
    )
    assert out == "tiny"
    env.execute.assert_not_called()


def test_normal_spill_persists_within_scoped_root():
    env = _env(session_id="runNORMAL001")
    out = maybe_persist_tool_result(
        content="Y" * 5000, tool_name="search_files", tool_use_id="toolu_01ABC",
        env=env, threshold=0,
    )
    assert "<persisted-output>" in out
    cmd = env.execute.call_args[0][0]
    assert "/sandbox/tmp/youtab-results/run-runNORMAL001/toolu_01ABC.txt" in cmd
