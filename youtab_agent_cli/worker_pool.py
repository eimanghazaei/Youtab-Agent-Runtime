"""WAVE-30H task 2a — isolated pre-warmed SINGLE-USE worker pool, integrated into
the real AgentRuntime dispatch path.

A fresh kanban worker pays the dominant startup cost every run: the eager
tool-registry import fan-out (``model_tools`` importing every tool module, which
drags in torch/transformers/playwright/numpy) plus a non-memoized engine.resolve.
This pool PRE-PAYS those once, in resident warm workers that block idle until a
task is assigned; each worker then runs EXACTLY ONE task through the real CLI
entry and exits (single-use — no state, grant, memory, secret or tenant is ever
carried between runs). The pool refills to keep ``size`` warm workers ready.

Design (cross-platform, no fork):
  * Warm worker = a real process (``python -m youtab_agent_cli.worker_pool --serve
    <dir>``) that imports the heavy stack (and optionally pre-resolves the engine),
    writes ``ready.json``, then polls for ``assign.json``.
  * ``WorkerPool.spawn(task, workspace, board)`` is the dispatcher ``spawn_fn``:
    it picks a ready warm worker, writes the EXACT per-run env+cmd from
    :func:`kanban_db.build_worker_invocation` (identical binding to a fresh spawn),
    returns the warm worker's REAL pid (so the dispatcher's crash detection +
    claim-TTL keep working unchanged), and refills the pool.
  * The worker applies the per-run env and runs the task via the real CLI
    (``runner="cli"``) or a deterministic runner (``runner="deterministic"``, tests),
    then exits. One run per worker.

Safety properties (all tested): per-run rebind (env applied fresh per task),
one-run-per-worker, bounded size, backpressure (block/queue when saturated),
stale-worker rejection (idle TTL), crash recovery (dead worker replaced),
zero-survivor teardown, checkpoint/cancel compatibility (unchanged — the pool only
provides warm processes; pause/checkpoint/resume and cancel act on the task as
before). Gated OFF by default; opt-in via ``YOUTAB_AGENT_RUNTIME_WORKER_POOL``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_IS_WINDOWS = os.name == "nt"


# ─────────────────────────────────────────────────────────────────────────────
# Resident warm-worker serve loop (runs as `python -m youtab_agent_cli.worker_pool
# --serve <dir>`).
# ─────────────────────────────────────────────────────────────────────────────

def _warm_boot(full: bool) -> dict:
    """Pay the expensive, per-run-invariant costs ONCE. Returns boot timings."""
    t0 = time.monotonic()
    timings: dict = {"pid": os.getpid()}
    if full:
        # The dominant cost: importing the tool registry fan-out (torch/transformers
        # via voice/wake/tts, playwright via browser, numpy) AND the CLI stack the
        # real worker runs through (cli.py + prompt_toolkit + rich). A fresh worker
        # re-pays all of this every run; a warm worker pays it here, once.
        import model_tools  # noqa: F401
        try:
            import youtab_agent_cli.main  # noqa: F401 — the CLI entry the worker runs
            import cli  # noqa: F401 — the quiet-chat runner
        except Exception:
            pass
        timings["import_ms"] = round((time.monotonic() - t0) * 1000, 1)
        # Pre-resolve + memoize the ECO engine connection so the per-task resolve
        # in the real worker hits the cache (opt-in cache, this process only).
        os.environ.setdefault("YOUTAB_ECO_RESOLVE_CACHE", "1")
        t1 = time.monotonic()
        try:
            from youtab_agent_cli import engine_connection as _ec
            _ec.resolve_connection(_ec_default_profile())
        except Exception:
            pass
        timings["resolve_ms"] = round((time.monotonic() - t1) * 1000, 1)
    else:
        timings["import_ms"] = round((time.monotonic() - t0) * 1000, 1)
        timings["resolve_ms"] = 0.0
    timings["warm_boot_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return timings


def _ec_default_profile() -> str:
    try:
        from youtab_agent_cli import agent_identity
        return getattr(agent_identity, "ECO_PROFILE_ID", "eco.v01")
    except Exception:
        return "eco.v01"


def _run_deterministic(task_id: str, db_path: str) -> int:
    """A hermetic single-use run: prove admission + per-run binding + completion
    WITHOUT the live model. Mirrors the deterministic-worker pattern used across
    the runtime tests. Returns an exit code."""
    from youtab_agent_cli import kanban_db as kb
    from youtab_agent_cli.worker_admission import (
        establish_managed_admission, ManagedWorkerAdmissionError)

    class _Agent:
        _admitted_command = None
    agent = _Agent()
    try:
        established = establish_managed_admission(agent)
    except ManagedWorkerAdmissionError as exc:
        c = kb.connect(db_path=Path(db_path))
        with kb.write_txn(c):
            kb._append_event(c, task_id, "worker_admission_error", {"error": str(exc)[:200]})
        c.close()
        return 3
    c = kb.connect(db_path=Path(db_path))
    with kb.write_txn(c):
        # Record exactly what per-run context this warm worker was bound to, so a
        # test can assert there is no leakage across the pool's reused processes.
        kb._append_event(c, task_id, "pool_worker_bound", {
            "pid": os.getpid(),
            "tenant": os.environ.get("YOUTAB_AGENT_TENANT"),
            "task_env": os.environ.get("YOUTAB_AGENT_KANBAN_TASK"),
            "has_admitted": agent._admitted_command is not None,
            "established": bool(established),
            # Leakage probe: build_worker_invocation drops YOUTAB_AGENT_TUI, so a
            # correctly-rebound pooled worker must observe it ABSENT even though
            # the warm process inherited the dispatcher's full env at pre-spawn.
            "tui_env": os.environ.get("YOUTAB_AGENT_TUI"),
        })
    c.close()
    c = kb.connect(db_path=Path(db_path))
    try:
        kb.complete_task(c, task_id, result="ok", summary="completed by pool worker (deterministic)")
    finally:
        c.close()
    return 0


def _run_cli(cmd: list) -> int:
    """Run the real worker in-process by driving the CLI argparse entry with the
    dispatcher's argv (imports already warm). Single-use: the CLI's own sys.exit
    ends this worker, which is exactly what we want.

    ``cmd`` is ``[<youtab entry...>, "-p", <profile>, "--cli", ..., "chat", ...]``.
    The youtab entry prefix (console-script vs ``python -m``) can resolve
    DIFFERENTLY in this warm process than it did in the dispatcher, so we do NOT
    match/strip it — the argparse args always begin at the first youtab flag
    ``-p`` (``_default_spawn`` always emits it), which is prefix-agnostic."""
    try:
        start = cmd.index("-p")
    except ValueError:
        # no -p (unexpected): fall back to dropping a python -m prefix if present
        start = 3 if (len(cmd) >= 3 and cmd[1] == "-m") else 1
    args = cmd[start:]
    sys.argv = ["youtab", *args]
    from youtab_agent_cli import main as _main
    # ``-p``/``--profile`` is intercepted by main._apply_profile_override(), which
    # sets YOUTAB_AGENT_HOME and STRIPS ``-p <profile>`` from sys.argv before
    # argparse. It runs at MODULE IMPORT — which, in this pre-warmed worker,
    # happened at boot with the wrong argv (a no-op). Re-run it now against the
    # task argv so the profile is applied and ``-p`` is stripped (otherwise
    # argparse sees ``-p`` as an unknown positional and rejects the run).
    try:
        _main._apply_profile_override()
    except SystemExit:
        raise
    except Exception:
        pass
    # main() calls sys.exit; translate into a return for the serve loop's finally.
    try:
        _main.main()
        return 0
    except SystemExit as se:
        return int(se.code) if isinstance(se.code, int) else (0 if se.code is None else 1)


def serve(workdir: str) -> int:
    """Resident warm-worker loop: warm-boot, signal ready, run ONE assigned task,
    exit. ``full`` warm-import and ``runner`` come from the assignment/env."""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    ready = wd / "ready.json"
    assign = wd / "assign.json"
    done = wd / "done.json"
    full = os.environ.get("YOUTAB_POOL_WARM_FULL", "1").strip() not in ("0", "", "false")
    timings = _warm_boot(full)
    ready.write_text(json.dumps({"ready_at": time.time(), **timings}), encoding="utf-8")
    # Block idle until assigned (or asked to shut down).
    shutdown = wd / "shutdown.flag"
    while not assign.exists():
        if shutdown.exists():
            return 0
        time.sleep(0.005)
    spec = json.loads(assign.read_text(encoding="utf-8"))
    # Per-run rebind: apply the EXACT env the dispatcher would have set.
    for k, v in (spec.get("env") or {}).items():
        os.environ[str(k)] = str(v)
    # Prune env keys the assignment says to drop (routing/session leakage guard).
    for k in (spec.get("env_drop") or []):
        os.environ.pop(str(k), None)
    runner = spec.get("runner", "cli")
    rc = 0
    try:
        if runner == "deterministic":
            rc = _run_deterministic(spec["task_id"], spec["db_path"])
        else:
            rc = _run_cli(spec["cmd"])
    finally:
        try:
            done.write_text(json.dumps({"done_at": time.time(), "rc": rc, "pid": os.getpid()}),
                            encoding="utf-8")
        except Exception:
            pass
    return rc


# ─────────────────────────────────────────────────────────────────────────────
# Pool manager (runs inside the dispatcher process).
# ─────────────────────────────────────────────────────────────────────────────

class _Worker:
    __slots__ = ("proc", "dir", "born", "assigned")

    def __init__(self, proc, wd):
        self.proc = proc
        self.dir = wd
        self.born = time.monotonic()
        self.assigned = False


class WorkerPool:
    def __init__(self, size: int = 2, *, runner: str = "cli", warm_full: bool = True,
                 idle_ttl_seconds: float = 900.0, base_dir: Optional[str] = None,
                 assign_wait_seconds: float = 20.0):
        self.size = max(1, int(size))
        self.runner = runner
        self.warm_full = warm_full
        self.idle_ttl = float(idle_ttl_seconds)
        self.assign_wait = float(assign_wait_seconds)
        self._base = Path(base_dir or (Path(os.environ.get("TEMP", "/tmp")) / f"youtab_pool_{uuid.uuid4().hex[:8]}"))
        self._base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ready: list[_Worker] = []
        self._all: list[_Worker] = []
        self._closed = False
        for _ in range(self.size):
            self._spawn_warm()

    # -- warm worker lifecycle --------------------------------------------------
    def _spawn_warm(self) -> None:
        wd = self._base / f"w_{uuid.uuid4().hex[:10]}"
        wd.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
        env["YOUTAB_POOL_WARM_FULL"] = "1" if self.warm_full else "0"
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if _IS_WINDOWS else 0
        popen_kwargs: dict = {"creationflags": flags} if _IS_WINDOWS else {"start_new_session": True}
        # Capture the warm worker's stdout/stderr to a per-worker log (ops
        # diagnosability + crash triage). The CLI runner's own run log is separate
        # (kanban worker log); this catches boot/import/exec failures.
        _logf = open(wd / "worker.log", "ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "youtab_agent_cli.worker_pool", "--serve", str(wd)],
            env=env, stdout=_logf, stderr=subprocess.STDOUT, **popen_kwargs)
        w = _Worker(proc, wd)
        self._all.append(w)
        self._ready.append(w)

    def _is_ready(self, w: _Worker) -> bool:
        return (w.dir / "ready.json").exists() and w.proc.poll() is None

    def _reap(self) -> None:
        """Drop dead/stale warm workers and refill (crash recovery + stale reject)."""
        now = time.monotonic()
        alive_ready = []
        for w in list(self._ready):
            if w.assigned:
                continue
            if w.proc.poll() is not None:  # crashed while idle
                self._all.remove(w) if w in self._all else None
                continue
            if (now - w.born) > self.idle_ttl:  # stale — reject + replace
                self._terminate(w)
                self._all.remove(w) if w in self._all else None
                continue
            alive_ready.append(w)
        self._ready = alive_ready
        # Prune assigned workers that have finished their single run. A worker is
        # removed from _ready at assignment (never re-enters the ready loop above),
        # so without this sweep a completed single-use worker lingers in _all for
        # the pool's lifetime — leaking _Worker/Popen objects and inflating
        # stats()["assigned"]. Keep assigned-and-still-running (in-flight) workers.
        self._all = [w for w in self._all
                     if not (w.assigned and w.proc.poll() is not None)]
        while len([w for w in self._all if not w.assigned]) < self.size and not self._closed:
            self._spawn_warm()

    def _take_ready(self) -> Optional[_Worker]:
        deadline = time.monotonic() + self.assign_wait
        while time.monotonic() < deadline:
            with self._lock:
                self._reap()
                for w in self._ready:
                    if not w.assigned and self._is_ready(w):
                        w.assigned = True
                        self._ready.remove(w)
                        return w
            time.sleep(0.02)  # backpressure: wait for a warm worker to become ready
        return None

    # -- dispatcher spawn_fn ----------------------------------------------------
    def spawn(self, task, workspace, *, board=None) -> Optional[int]:
        """Dispatcher spawn_fn. Assign a warm worker; fall back to a fresh spawn
        only if the pool is saturated past the backpressure window (never stalls
        the board). Returns a REAL pid for crash detection."""
        from youtab_agent_cli import kanban_db as kb
        if self._closed:
            return kb._default_spawn(task, workspace, board=board)
        # Take a warm worker FIRST (this is the backpressure wait); only build the
        # per-run invocation once we actually have a worker to assign it to.
        w = self._take_ready()
        if w is None:  # saturated past the backpressure window: fresh-spawn fallback
            return kb._default_spawn(task, workspace, board=board)
        env, cmd = kb.build_worker_invocation(task, workspace, board=board)
        # Only pass the delta the dispatcher itself injects, so the warm worker's
        # per-run binding is IDENTICAL to a fresh subprocess (tenant/grant/task/
        # workspace/board/correlation/profile/overrides).
        # env_drop makes the pooled binding IDENTICAL to a fresh subprocess: a
        # warm worker inherited the dispatcher's full ``os.environ`` at pre-spawn,
        # but ``build_worker_invocation`` deliberately REMOVES keys (session/
        # routing vars in ``session_context._VAR_MAP`` and ``YOUTAB_AGENT_TUI``).
        # Those keys are absent from ``env`` but still live in the warm process,
        # so we must tell serve() to prune them — otherwise e.g. an inherited
        # YOUTAB_AGENT_TUI=1 would boot the TUI and the worker would bail without
        # doing the run. The delta is computed in THIS dispatcher process, so
        # ``set(os.environ) - set(env)`` is exactly what the invocation dropped.
        env_drop = [k for k in os.environ if k not in env]
        spec = {
            "task_id": task.id, "db_path": env.get("YOUTAB_AGENT_KANBAN_DB", ""),
            "cmd": cmd, "runner": self.runner, "env": env, "env_drop": env_drop,
        }
        (w.dir / "assign.json").write_text(json.dumps(spec), encoding="utf-8")
        pid = w.proc.pid
        # refill happens on the next _reap via a background touch
        with self._lock:
            if not self._closed and len([x for x in self._all if not x.assigned]) < self.size:
                self._spawn_warm()
        return pid

    # -- teardown ---------------------------------------------------------------
    def _terminate(self, w: _Worker) -> None:
        try:
            (w.dir / "shutdown.flag").write_text("1", encoding="utf-8")
        except Exception:
            pass
        try:
            if w.proc.poll() is None:
                if _IS_WINDOWS:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(w.proc.pid)],
                                   capture_output=True, timeout=15)
                else:
                    import signal as _sig
                    os.killpg(os.getpgid(w.proc.pid), _sig.SIGKILL)
        except Exception:
            try:
                w.proc.kill()
            except Exception:
                pass
        # REAP the killed child so it does not linger as a zombie. On POSIX a
        # SIGKILL'd child stays in the process table (state Z) until its parent
        # waitpid()s it, and ``os.kill(pid, 0)`` reports a zombie as ALIVE — which
        # would make the zero-survivor teardown falsely report survivors (the
        # Windows path has no zombies, so this only bites on Linux). ``proc.wait``
        # performs that waitpid; bounded so a wedged reap can never hang close().
        try:
            w.proc.wait(timeout=10)
        except Exception:
            pass

    def close(self) -> dict:
        """Zero-survivor teardown of every warm worker (and its tree)."""
        self._closed = True
        pids = []
        with self._lock:
            for w in list(self._all):
                pids.append(w.proc.pid)
                self._terminate(w)
            self._all.clear()
            self._ready.clear()
        survivors = [p for p in pids if _pid_alive(p)]
        return {"terminated": pids, "survivors": survivors, "zero_survivors": not survivors}

    def stats(self) -> dict:
        with self._lock:
            return {"size": self.size, "total": len(self._all),
                    "ready": sum(1 for w in self._ready if self._is_ready(w)),
                    "assigned": sum(1 for w in self._all if w.assigned)}


def _pid_alive(pid: int) -> bool:
    try:
        if _IS_WINDOWS:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=10)
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _main(argv: Optional[list] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "--serve":
        return serve(argv[1])
    print("usage: python -m youtab_agent_cli.worker_pool --serve <dir>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
