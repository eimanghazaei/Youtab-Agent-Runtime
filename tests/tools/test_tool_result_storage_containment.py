"""Containment regression for the tool-result overflow store (ADR-0005 §7a).

`tool_result_storage` is an allow-listed internal-plane writer: it spills a tool's
own oversized output into a fixed sandbox result-store root. The only
attacker-influenceable input to the write path is the system ``tool_use_id``. This
suite proves the enforced containment invariant — a crafted ``tool_use_id`` can
never escape the store root, nest, or traverse, and `_write_to_sandbox` fails
closed (no `env.execute`) on any unsafe leaf.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.tool_result_storage import (
    _safe_result_filename,
    _write_to_sandbox,
    maybe_persist_tool_result,
)


def _env(temp_dir="/sandbox/tmp", rc=0):
    env = MagicMock()
    env.get_temp_dir.return_value = temp_dir
    env.execute.return_value = {"output": "", "returncode": rc}
    return env


# ── _write_to_sandbox: enforced containment at the write chokepoint ──────────


def test_write_contained_leaf_succeeds():
    env = _env()
    ok = _write_to_sandbox("payload", "/sandbox/tmp/youtab-results/abc.txt", env)
    assert ok is True
    env.execute.assert_called_once()
    cmd = env.execute.call_args[0][0]
    assert "/sandbox/tmp/youtab-results/abc.txt" in cmd
    # content travels via stdin, never embedded in the command string
    assert env.execute.call_args.kwargs.get("stdin_data") == "payload"


@pytest.mark.parametrize(
    "remote_path",
    [
        "/sandbox/tmp/youtab-results/..",   # parent traversal leaf
        "/sandbox/tmp/youtab-results/.",    # current-dir leaf
        "/sandbox/tmp/youtab-results/",     # empty leaf (trailing sep)
    ],
)
def test_write_rejects_unsafe_leaf_fails_closed(remote_path):
    env = _env()
    ok = _write_to_sandbox("payload", remote_path, env)
    assert ok is False
    env.execute.assert_not_called()  # zero side effect on violation


# ── _safe_result_filename: no traversal / single component ───────────────────


@pytest.mark.parametrize(
    "raw_id",
    [
        "../../etc/passwd",
        "..",
        "../..",
        "a/b/c",
        r"..\..\windows\system32",
        "/absolute/evil",
        "",
        "...",
        "normal-tool_use.id",
    ],
)
def test_safe_result_filename_is_a_single_safe_component(raw_id):
    name = _safe_result_filename(raw_id)
    assert "/" not in name and "\\" not in name
    assert name not in ("", ".", "..")
    assert not name.startswith(".")  # no hidden / traversal-looking leading dot
    assert name.endswith(".txt")


# ── maybe_persist_tool_result: a malicious id stays inside the store root ─────


def test_malicious_tool_use_id_stays_in_store_root():
    env = _env()
    content = "X" * 5000
    out = maybe_persist_tool_result(
        content=content,
        tool_name="search_files",
        tool_use_id="../../etc/passwd",
        env=env,
        threshold=0,  # force persistence
    )
    assert "<persisted-output>" in out
    env.execute.assert_called_once()
    cmd = env.execute.call_args[0][0]
    # the write lands inside the fixed store root ...
    assert "/sandbox/tmp/youtab-results/" in cmd
    # ... and never at the traversed target or with a traversal component
    assert "/etc/passwd" not in cmd
    assert "/.." not in cmd and "../" not in cmd
    assert env.execute.call_args.kwargs.get("stdin_data") == content


def test_normal_tool_use_id_persists_within_store_root():
    env = _env()
    content = "Y" * 5000
    out = maybe_persist_tool_result(
        content=content, tool_name="search_files",
        tool_use_id="toolu_01ABC", env=env, threshold=0,
    )
    assert "<persisted-output>" in out
    cmd = env.execute.call_args[0][0]
    assert "/sandbox/tmp/youtab-results/toolu_01ABC.txt" in cmd


def test_small_result_not_persisted_no_write():
    env = _env()
    out = maybe_persist_tool_result(
        content="tiny", tool_name="search_files", tool_use_id="toolu_x",
        env=env, threshold=1000,
    )
    assert out == "tiny"
    env.execute.assert_not_called()
