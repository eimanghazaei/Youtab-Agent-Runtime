"""R5 — negative test for the post-authority-loss external-effect window.

ADR-0002 limitation #1: after durable run-authority loss the gateway fail-stops
(`_on_run_authority_lost` -> `agent.interrupt()` -> `os._exit(75)`). But
`interrupt()` only sets a COOPERATIVE per-thread flag; it does not synchronously
reap descendants. Tool effect sites spawn subprocesses with
`start_new_session=True` (own session/process group), so `os._exit(75)`
orphans them and they keep running — free to complete a NEW external effect
AFTER authority was lost.

This module encodes the DESIRED contract as an xfail: a fail-stop must leave no
descendant able to produce a new external effect. It reproduces the mechanism
deterministically (no LLM/agent stack): a harness process spawns a
new-session grandchild that writes a sentinel after a delay, then the harness
`os._exit(75)`s (mimicking the current fail-stop with no descendant reap). The
sentinel file is the stand-in external effect.

- `test_current_failstop_orphans_new_session_descendant_DOCUMENTS_GAP` PASSES
  today: it proves the gap (the orphaned grandchild still writes the sentinel).
- `test_failstop_must_leave_no_descendant_effect` is the acceptance contract,
  xfail until the effect-teardown seam is decided/implemented (R5 blocker:
  cross-module supervision, owner decision pending).
"""

import os
import subprocess
import sys
import textwrap
import time

import pytest

# A harness that spawns a detached (new-session) grandchild which writes SENTINEL
# after DELAY, then the harness exits with 75 WITHOUT reaping the grandchild —
# exactly the current _on_run_authority_lost path (interrupt-flag then os._exit).
_HARNESS_NO_REAP = textwrap.dedent(
    """
    import os, sys, subprocess, time
    sentinel = sys.argv[1]
    grandchild = [
        sys.executable, "-c",
        "import time,sys; time.sleep(1.5); open(sys.argv[1],'w').write('EFFECT')",
        sentinel,
    ]
    kw = {}
    if os.name == "posix":
        kw["start_new_session"] = True          # own session/process group
    else:
        kw["creationflags"] = 0x00000200         # CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(grandchild, **kw)
    time.sleep(0.2)
    os._exit(75)                                  # fail-stop, no descendant reap
    """
)


def _run_harness(tmp_path, harness_src):
    sentinel = tmp_path / "effect.sentinel"
    proc = subprocess.run([sys.executable, "-c", harness_src, str(sentinel)])
    assert proc.returncode == 75
    # Wait past the grandchild's delay; if it survived, the sentinel appears.
    time.sleep(2.5)
    return sentinel


def test_current_failstop_orphans_new_session_descendant_DOCUMENTS_GAP(tmp_path):
    """Characterizes the CURRENT behavior: os._exit(75) orphans a new-session
    descendant, which completes its external effect after the fail-stop."""
    sentinel = _run_harness(tmp_path, _HARNESS_NO_REAP)
    assert sentinel.exists(), (
        "expected the orphaned descendant to survive os._exit and write the "
        "sentinel (this documents the R5 residual effect window)"
    )
    assert sentinel.read_text() == "EFFECT"


@pytest.mark.xfail(
    reason="R5 blocker: closing the descendant effect window needs a synchronous "
    "effect-teardown seam in the process-supervision layer (tools/environments, "
    "win_job_supervisor), invoked by the durable fail-stop. Owner/architecture "
    "decision pending; not implemented across the module boundary.",
    strict=True,
)
def test_failstop_must_leave_no_descendant_effect(tmp_path):
    """Acceptance contract: after a fail-stop, no already-spawned descendant may
    complete a NEW external effect. Reuses the no-reap harness (the fixed harness
    would reap its descendant tree before exit); xfails until the seam lands."""
    sentinel = _run_harness(tmp_path, _HARNESS_NO_REAP)
    assert not sentinel.exists(), (
        "a descendant produced an external effect after the fail-stop"
    )
