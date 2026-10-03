"""P4 — Windows Job Object owned-process-tree supervision (real child+grandchild).

Scope kept honest: this covers the SUBPROCESS surfaces. The delegate child runs
as a THREAD in the parent (DaemonThreadPoolExecutor), which a Job Object cannot
force-terminate — forced termination of in-process delegate work stays OPEN and
is asserted separately below. A killed Job-owned tree is TERMINATION, not resume.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object supervision")

from youtab_runtime.win_job_supervisor import WindowsJobSupervisor  # noqa: E402


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


def _kill(pid) -> None:
    if pid:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True)


def _scripts(tmp: Path):
    gc = tmp / "gc.py"
    gc.write_text("import os,sys,time\nopen(sys.argv[1],'w').write(str(os.getpid()))\ntime.sleep(60)\n",
                  encoding="utf-8")
    child = tmp / "child.py"
    child.write_text(
        "import os,sys,time,subprocess\n"
        "gc_py,gc_pid,child_pid=sys.argv[1:4]\n"
        "open(child_pid,'w').write(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable, gc_py, gc_pid])\n"
        "time.sleep(60)\n",
        encoding="utf-8")
    return child, gc


def _await(*paths, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline and not all(p.exists() for p in paths):
        time.sleep(0.1)
    assert all(p.exists() for p in paths), "child/grandchild did not start"


def test_owned_tree_survives_wait_then_cancel_kills_only_that_tree(tmp_path):
    child_py, gc_py = _scripts(tmp_path)
    child_pidf, gc_pidf = tmp_path / "c.pid", tmp_path / "g.pid"

    sup = WindowsJobSupervisor()
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    cpid = gpid = None
    try:
        # Child launched INTO the job from creation; the grandchild it spawns joins.
        sup.spawn([sys.executable, str(child_py), str(gc_py), str(gc_pidf), str(child_pidf)])
        _await(child_pidf, gc_pidf)
        cpid = int(child_pidf.read_text().strip())
        gpid = int(gc_pidf.read_text().strip())

        # A short client/UI wait does NOT kill the owned tree.
        time.sleep(1.0)
        assert _alive(cpid) and _alive(gpid), "owned tree must survive a client wait"
        assert _alive(unrelated.pid), "unrelated process alive before cancel"

        # Explicit cancel / execution deadline -> terminate ONLY the owned tree.
        sup.terminate()
        time.sleep(1.0)
        assert not _alive(cpid), "child terminated on cancel"
        assert not _alive(gpid), "grandchild terminated on cancel (owned tree, no orphan)"
        assert _alive(unrelated.pid), "unrelated process must be UNTOUCHED"
    finally:
        _kill(unrelated.pid); _kill(cpid); _kill(gpid)
        sup.close()


def test_kill_on_job_close_reaps_tree_on_parent_death(tmp_path):
    """KILL_ON_JOB_CLOSE: closing the job handle (parent-death equivalent) reaps
    owned processes — no orphan. TERMINATION, not resume."""
    child_py, gc_py = _scripts(tmp_path)
    child_pidf, gc_pidf = tmp_path / "c2.pid", tmp_path / "g2.pid"
    sup = WindowsJobSupervisor()
    cpid = gpid = None
    try:
        sup.spawn([sys.executable, str(child_py), str(gc_py), str(gc_pidf), str(child_pidf)])
        _await(child_pidf, gc_pidf)
        cpid = int(child_pidf.read_text().strip())
        gpid = int(gc_pidf.read_text().strip())
        assert _alive(cpid) and _alive(gpid)
        sup.close()  # parent-death equivalent
        time.sleep(1.0)
        assert not _alive(cpid) and not _alive(gpid), "KILL_ON_JOB_CLOSE reaps the tree; no orphan"
    finally:
        _kill(cpid); _kill(gpid)
        sup.close()


def test_in_process_delegate_forced_termination_is_open():
    """Honest boundary: the delegate child is a THREAD (DaemonThreadPoolExecutor),
    not a subprocess, so a Job Object cannot force-terminate it. Forced
    termination of in-process delegate work is OPEN; only cooperative stop
    (is_stop_requested) applies there."""
    from tools.daemon_pool import DaemonThreadPoolExecutor
    from concurrent.futures import ThreadPoolExecutor
    assert issubclass(DaemonThreadPoolExecutor, ThreadPoolExecutor)  # thread, not process
    pytest.skip("OPEN: forced termination of in-process (thread) delegate work is not "
                "covered by a Job Object; cooperative deadline/cancel only.")
