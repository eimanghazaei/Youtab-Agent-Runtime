"""R5 — the durable fail-stop reaps effect-capable descendants before exit.

ADR-0002 limitation #1: after durable run-authority loss the gateway fail-stops
(`_on_run_authority_lost` -> `agent.interrupt()` -> descendant reap ->
`os._exit(75)`). `interrupt()` only sets a COOPERATIVE per-thread flag; it does
not reap descendants. Tool effect sites spawn subprocesses with
`start_new_session` / `CREATE_NEW_PROCESS_GROUP` (own session/process group), so
without a synchronous reap `os._exit(75)` would ORPHAN them and they could
complete a NEW external effect after authority was lost.

The fix (R5, owner-approved): the fail-stop calls the SINGLE process-supervision
authority's `process_registry.reap_effect_descendants()` after the cooperative
interrupt and before exit. These tests reproduce the mechanism deterministically
(no LLM/agent stack) with a detached/new-session descendant whose sentinel file
is the stand-in external effect:

- `..._orphans_...DOCUMENTS_GAP`: a harness that exits WITHOUT the reap still
  orphans the descendant (sentinel appears) — proves the gap the hook closes.
- `..._reaps_detached_descendant`: a harness that runs the REAL reap before exit
  leaves NO descendant effect (sentinel absent) and reports contained=True.
"""

import os
import subprocess
import sys
import textwrap
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_DETACH_KW = (
    "kw = {'start_new_session': True} if os.name == 'posix' "
    "else {'creationflags': 0x00000200}"  # CREATE_NEW_PROCESS_GROUP
)

# Harness A: spawn a detached grandchild that writes SENTINEL after a delay,
# then os._exit(75) WITHOUT reaping — the pre-R5 fail-stop.
_HARNESS_NO_REAP = textwrap.dedent(
    f"""
    import os, sys, subprocess, time
    sentinel = sys.argv[1]
    gc = [sys.executable, "-c",
          "import time,sys; time.sleep(1.5); open(sys.argv[1],'w').write('EFFECT')",
          sentinel]
    {_DETACH_KW}
    subprocess.Popen(gc, **kw)
    time.sleep(0.2)
    os._exit(75)
    """
)

# Harness B: same detached grandchild, then the REAL reap before exit. The
# supervision module is imported BEFORE spawning (as in the real gateway, where
# it is loaded long before authority loss), so the reap runs immediately and the
# child's delayed effect has not yet fired.
_HARNESS_WITH_REAP = textwrap.dedent(
    f"""
    import os, sys, subprocess, time
    from tools.process_registry import reap_effect_descendants   # pre-imported
    sentinel, result = sys.argv[1], sys.argv[2]
    gc = [sys.executable, "-c",
          "import time,sys; time.sleep(3.0); open(sys.argv[1],'w').write('EFFECT')",
          sentinel]
    {_DETACH_KW}
    subprocess.Popen(gc, **kw)
    time.sleep(0.2)
    r = reap_effect_descendants(deadline_s=4.0)
    open(result, "w").write("contained=%s killed=%d unverified=%s" %
                            (r["contained"], len(r["killed"]), r["unverified"]))
    os._exit(75)
    """
)


def _run(harness_src, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", harness_src, *args], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )
    return proc


def test_current_failstop_orphans_detached_descendant_DOCUMENTS_GAP(tmp_path):
    """No-reap fail-stop orphans a detached descendant → it completes its
    external effect after exit. Documents the gap the R5 hook closes."""
    sentinel = tmp_path / "effect.sentinel"
    proc = _run(_HARNESS_NO_REAP, str(sentinel))
    assert proc.returncode == 75
    time.sleep(2.5)
    assert sentinel.exists() and sentinel.read_text() == "EFFECT", (
        "expected the orphaned detached descendant to survive os._exit"
    )


def test_failstop_reaps_detached_descendant(tmp_path):
    """The R5 reap terminates a detached descendant before exit, so NO external
    effect occurs after the fail-stop, and containment is verified."""
    sentinel = tmp_path / "effect.sentinel"
    result = tmp_path / "reap.result"
    proc = _run(_HARNESS_WITH_REAP, str(sentinel), str(result))
    assert proc.returncode == 75, proc.stderr
    # The reap ran and verified containment before exit.
    assert result.exists(), f"reap result missing; stderr={proc.stderr}"
    assert "contained=True" in result.read_text(), result.read_text()
    # Past the grandchild's 3.0s delay: its effect must NOT have happened.
    time.sleep(3.5)
    assert not sentinel.exists(), (
        "a descendant produced an external effect after the fail-stop reap"
    )
