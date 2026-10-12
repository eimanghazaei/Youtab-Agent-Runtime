"""R5 — the durable fail-stop reaps effect-capable descendants before exit.

ADR-0002 limitation #1: after durable run-authority loss the gateway fail-stops
(`_on_run_authority_lost` -> `agent.interrupt()` -> descendant reap ->
`os._exit(75)`). `interrupt()` only sets a COOPERATIVE per-thread flag; it does
not reap descendants. Tool effect sites spawn subprocesses with
`start_new_session` / `CREATE_NEW_PROCESS_GROUP`, so without a synchronous reap
`os._exit(75)` would ORPHAN them and they could complete a NEW external effect
after authority was lost.

The fix (R5): the fail-stop calls the SINGLE supervision authority's
`ProcessRegistry.reap_effect_descendants()`. This module proves the mechanism and
the four review-hardened invariants:
  1. enumeration failure -> contained=False (never vacuous True);
  2. unknown PID identity -> never signalled, and contained=False;
  3. deadline bounds the WHOLE teardown (terminate + settle), observable elapsed;
  4. child-spawn race -> the sweep repeats until a clean sweep (or the deadline).
"""

import os
import subprocess
import sys
import textwrap
import time

from tools.process_registry import ProcessRegistry

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

# Harness B: same detached grandchild, then the REAL reap before exit (module
# pre-imported, as in the real gateway where it is loaded before authority loss).
_HARNESS_WITH_REAP = textwrap.dedent(
    f"""
    import os, sys, subprocess, time
    from tools.process_registry import reap_effect_descendants
    sentinel, result = sys.argv[1], sys.argv[2]
    gc = [sys.executable, "-c",
          "import time,sys; time.sleep(3.0); open(sys.argv[1],'w').write('EFFECT')",
          sentinel]
    {_DETACH_KW}
    subprocess.Popen(gc, **kw)
    time.sleep(0.2)
    r = reap_effect_descendants(deadline_s=6.0)
    open(result, "w").write("contained=%s signalled=%d unverified=%s" %
                            (r["contained"], len(r["signalled"]), r["unverified"]))
    os._exit(75)
    """
)


def _run(harness_src, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", harness_src, *args], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )


def _spawn_child(seconds=30):
    """A real, identity-bearing child of THIS process."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


# ---- Integration: real subprocess through the fail-stop mechanism ----

def test_current_failstop_orphans_detached_descendant_DOCUMENTS_GAP(tmp_path):
    sentinel = tmp_path / "effect.sentinel"
    proc = _run(_HARNESS_NO_REAP, str(sentinel))
    assert proc.returncode == 75
    time.sleep(2.5)
    assert sentinel.exists() and sentinel.read_text() == "EFFECT"


def test_failstop_reaps_detached_descendant(tmp_path):
    sentinel = tmp_path / "effect.sentinel"
    result = tmp_path / "reap.result"
    proc = _run(_HARNESS_WITH_REAP, str(sentinel), str(result))
    assert proc.returncode == 75, proc.stderr
    assert result.exists(), f"reap result missing; stderr={proc.stderr}"
    assert "contained=True" in result.read_text(), result.read_text()
    time.sleep(3.5)
    assert not sentinel.exists()


# ---- Review finding 1: enumeration failure -> contained=False ----

def test_enumeration_failure_is_not_contained(monkeypatch):
    reg = ProcessRegistry()
    monkeypatch.setattr(reg, "_enumerate_owned_descendants", lambda: ({}, False))
    r = reg.reap_effect_descendants(deadline_s=1.0)
    assert r["enumeration_ok"] is False
    assert r["contained"] is False   # never a vacuous True on unknown tree


# ---- Review finding 2: unknown identity -> never signalled, not contained ----

def test_unknown_identity_is_never_killed_and_not_contained(monkeypatch):
    reg = ProcessRegistry()
    child = _spawn_child(30)
    try:
        # Force identity to be unavailable for every pid.
        monkeypatch.setattr(reg, "_safe_host_start_time", staticmethod(lambda pid: None))
        r = reg.reap_effect_descendants(deadline_s=3.0)
        assert child.pid in r["unidentified"]
        assert child.pid not in r["signalled"]      # unknown identity NEVER signalled
        assert r["contained"] is False              # fail-closed
        time.sleep(0.5)
        assert child.poll() is None                 # the child was NOT killed
    finally:
        child.kill()
        child.wait(timeout=10)


# ---- Review finding 3: deadline bounds the whole op (budget exceeded) ----

def test_budget_exceeded_is_not_contained_and_does_not_signal(monkeypatch):
    reg = ProcessRegistry()
    child = _spawn_child(30)
    try:
        # Zero budget: the deadline is spent before any target is signalled.
        r = reg.reap_effect_descendants(deadline_s=0.0)
        assert r["budget_exceeded"] is True
        assert r["contained"] is False
        assert child.pid not in r["signalled"]
        assert r["elapsed_s"] <= 2.0                # whole op is bounded
        time.sleep(0.3)
        assert child.poll() is None                 # not signalled -> still alive
    finally:
        child.kill()
        child.wait(timeout=10)


# ---- Review finding 4: child-spawn race -> re-sweep catches the late child ----

def test_child_spawn_race_is_caught_by_resweep(monkeypatch):
    """A descendant spawned DURING teardown (after the first snapshot) must be
    caught by a repeat sweep — a single snapshot would miss it and report a
    false contained=True."""
    reg = ProcessRegistry()
    alive = {100}                      # pid 100 present at t0
    spawned_late = {"done": False}

    def fake_enumerate():
        # Snapshot of currently-alive owned pids (identity baseline = 1).
        snap = {pid: 1 for pid in alive}
        # Simulate pid 100 spawning pid 101 DURING teardown, once, after the
        # first snapshot has been taken.
        if 100 in alive and not spawned_late["done"]:
            alive.add(101)
            spawned_late["done"] = True
        return snap, True

    def fake_is_ours(pid, start):
        return pid in alive

    def fake_terminate(pid, start=None):
        alive.discard(pid)             # kill

    monkeypatch.setattr(reg, "_enumerate_owned_descendants", fake_enumerate)
    monkeypatch.setattr(reg, "_host_pid_is_ours", fake_is_ours)
    monkeypatch.setattr(reg, "_terminate_host_pid", fake_terminate)

    r = reg.reap_effect_descendants(deadline_s=5.0)
    assert r["contained"] is True
    assert r["sweeps"] >= 2                          # it re-swept
    assert set(r["verified_dead"]) == {100, 101}     # the late child was caught
    assert r["unverified"] == []
    assert not alive                                 # everything reaped
