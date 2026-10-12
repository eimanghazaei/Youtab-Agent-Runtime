"""P0-G BASELINE CHARACTERIZATION — Windows MCP lifecycle: process-tree orphan risk.

Defect [C-3.2b]: MCP subprocess reaping relies on POSIX process groups
(`os.killpg`/`os.getpgid`) and a POSIX-only parent-death watchdog
(`tools/mcp_stdio_watchdog.py`, gated at `tools/mcp_tool.py:709`). On Windows
neither exists, so a hard parent death or a per-pid terminate does NOT reap a
server's grandchildren — they orphan.

This test runs on Windows and proves:
1. `os.killpg`/`os.getpgid` are absent (no process-group tree kill available);
2. `_wrap_command_with_watchdog` returns the command UNWRAPPED on non-POSIX
   (no parent-death watchdog);
3. a real child→grandchild tree: terminating only the direct child leaves the
   grandchild alive (orphan). Cleanup is by exact PID (never a name-based kill).
"""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name == "posix", reason="Windows orphan characterization")


def _alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True,
    ).stdout
    return str(pid) in out


def _kill_pid(pid: int):
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True)


def test_windows_has_no_process_group_tree_kill():
    assert not hasattr(os, "killpg"), "os.killpg must be absent on Windows"
    assert not hasattr(os, "getpgid"), "os.getpgid must be absent on Windows"


def test_watchdog_wrapper_is_noop_on_windows():
    from tools.mcp_tool import _wrap_command_with_watchdog

    cmd, args = _wrap_command_with_watchdog("some-mcp-server", ["--flag"])
    assert cmd == "some-mcp-server" and args == ["--flag"], (
        "on Windows the parent-death watchdog is not wired (command unwrapped)"
    )


def test_grandchild_orphans_when_only_child_is_terminated(tmp_path):
    gc_pidfile = tmp_path / "gc.pid"
    gc_script = tmp_path / "gc.py"
    child_script = tmp_path / "child.py"
    gc_script.write_text(
        "import os,sys,time\n"
        "open(sys.argv[1],'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    child_script.write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    child = subprocess.Popen([sys.executable, str(child_script), str(gc_script), str(gc_pidfile)])
    gpid = None
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not gc_pidfile.exists():
            time.sleep(0.1)
        assert gc_pidfile.exists(), "grandchild did not start"
        gpid = int(gc_pidfile.read_text().strip())
        assert _alive(gpid), "grandchild should be alive before we kill the child"

        # Terminate ONLY the direct child (as a per-pid kill would; no tree kill
        # exists on Windows). This does NOT reap the grandchild.
        child.terminate()
        child.wait(timeout=10)
        time.sleep(1.0)

        # DEFECT REPRODUCED: the grandchild is orphaned — still alive.
        assert _alive(gpid), (
            "DEFECT: grandchild orphaned after child terminate — Windows has no "
            "process-tree kill; MCP server descendants would leak"
        )
    finally:
        if gpid is not None:
            _kill_pid(gpid)
        if child.poll() is None:
            child.terminate()
