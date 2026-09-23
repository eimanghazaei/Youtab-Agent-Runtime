"""WAVE-26 harness-owned process control + fault injection (agent 4).

This module gives the benchmark a *safe, deterministic* way to spawn, prove
ownership of, restart, fault-inject and reap a **harness-owned runtime
instance** — and nothing else. Every guarantee here exists so the benchmark can
judge recovery/restart behaviour from observable, durable state (the WAVE-26
run journal) rather than from an agent's self-report, and so the control plane
can never touch a real, production Youtab install.

Safety invariants (all enforced, not documented aspirations):

* **Isolated home, always explicit.** Every launch sets the child's
  ``YOUTAB_AGENT_HOME`` to an isolated temp directory. It is passed explicitly
  because :func:`youtab_constants.get_youtab_home` *silently* falls back to the
  real platform home when the variable is unset — a harness that relied on that
  fallback would operate on the operator's real data.
* **The child refuses to run against a real home.** On startup the child aborts
  if its resolved home equals the platform-default Youtab home. So even a
  hand-set environment cannot point the harness child at a production install.
* **No production bypass.** Fault injection is gated behind an in-code
  ``isolated_benchmark=True`` constructor flag. There is deliberately no
  environment variable that flips production into fault-injection mode.
* **Ownership is proven before any signal.** A kill/restart is refused unless
  ownership is :data:`OwnershipOutcome.OWNED`. Ownership is a conjunction of a
  per-launch token (env → child ack), the ``(pid, start_time)`` cookie (guards
  PID reuse) and a child-held advisory lock. An *ambiguous* result (cannot
  prove start-time) fails **closed** — the harness never signals a process it
  cannot claim, and never signals a process discovered only by a cmdline scan.
* **The ``youtab update`` command is hard-excluded.** ``youtab update`` performs
  ``git reset --hard`` on the real checkout (see RECON R4 HAZARD). This module
  never invokes it, and :meth:`HarnessProcess.launch` refuses any custom command
  whose Youtab subcommand is ``update``.

The default child is a small, self-contained deterministic worker (see
:func:`run_harness_child`) that emits the readiness sentinel, drives a fake run
whose lifecycle/recovery is recorded to the run journal, and applies a
declarative fault plan. Tests operate exclusively on this child + isolated temp
state, so running them can never perturb a real system.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Read-only reuse of the gateway's battle-tested process primitives (RECON R4).
# NEVER use os.kill(pid, 0) on Windows — _pid_exists is the safe liveness probe.
from gateway.status import (
    _pid_exists,
    get_process_start_time,
    terminate_pid,
)
from youtab_constants import _get_platform_default_youtab_home
from youtab_runtime.run_journal import Principal, RunEvent, append_event, list_events
from youtab_runtime.run_states import OwnershipOutcome, ProcessState

# ── Environment contract between harness (parent) and child ──────────────────
ENV_HOME = "YOUTAB_AGENT_HOME"
ENV_LAUNCH_TOKEN = "YOUTAB_HARNESS_LAUNCH_TOKEN"
ENV_ISOLATED = "YOUTAB_HARNESS_ISOLATED"
ENV_READY_FILE = "YOUTAB_HARNESS_READY_FILE"
ENV_RUN_ID = "YOUTAB_HARNESS_RUN_ID"
ENV_TENANT = "YOUTAB_HARNESS_TENANT"
ENV_USER = "YOUTAB_HARNESS_USER"
ENV_FAULT_PLAN = "YOUTAB_HARNESS_FAULT_PLAN"
ENV_MODE = "YOUTAB_HARNESS_CHILD_MODE"
ENV_SENTINEL = "YOUTAB_HARNESS_SENTINEL"
ENV_SET_RESUME = "YOUTAB_HARNESS_SET_RESUME"

CHILD_MODE_RUN_ONCE = "run_once"
CHILD_MODE_IDLE = "idle"

SENTINEL_DASHBOARD = "dashboard"
SENTINEL_BACKEND = "backend"

#: Readiness sentinel the real backend prints (web_server.py). We mirror the
#: exact shape so a benchmark could point the same barrier at the real server.
_READY_SENTINEL_RE = re.compile(
    r"YOUTAB_AGENT_(?:DASHBOARD|BACKEND)_READY port=(?P<port>-?\d+)"
)

#: HARD-EXCLUDED subcommand — see module docstring / RECON R4 HAZARD.
_EXCLUDED_SUBCOMMAND = "update"

_HARNESS_DIRNAME = "harness"
_IS_WINDOWS = sys.platform == "win32"
_WINDOWS_LOCK_OFFSET = 1024 * 1024  # mirror gateway/status.py byte-range choice

if _IS_WINDOWS:  # pragma: no cover - platform-selected import
    import msvcrt
else:  # pragma: no cover - platform-selected import
    import fcntl


# ── Exceptions ───────────────────────────────────────────────────────────────
class HarnessError(RuntimeError):
    """Base class for harness control failures."""


class HarnessOwnershipError(HarnessError):
    """Raised when a kill/restart is refused because ownership is not proven."""

    def __init__(self, outcome: OwnershipOutcome, pid: Optional[int]) -> None:
        self.outcome = outcome
        self.pid = pid
        super().__init__(
            f"refusing to signal pid={pid}: ownership is {outcome!s} (not owned)"
        )


class HarnessFaultDisabled(HarnessError):
    """Raised when fault injection is attempted outside isolated benchmark mode."""


class HarnessUpdateExcluded(HarnessError):
    """Raised if a command would invoke the hard-excluded ``youtab update``."""


class HarnessNotReady(HarnessError):
    """Raised when a child fails to reach readiness within the deadline."""


# ── Advisory lock (child-held; OS auto-releases on death = liveness) ─────────
class _AdvisoryLock:
    """A cross-platform advisory file lock, following gateway/status.py.

    The **child** holds this for its lifetime, so the OS releases it the instant
    the child dies — abrupt kill included — which makes "is the lock held" a
    liveness signal the parent can read as corroborating ownership evidence.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError:
            return False
        try:
            if _IS_WINDOWS:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write("\n")
                    handle.flush()
                handle.seek(_WINDOWS_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            if _IS_WINDOWS:
                handle.seek(_WINDOWS_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


def child_lock_is_active(home: Path) -> bool:
    """Return True when some live process holds the child advisory lock in *home*.

    Non-destructive: attempts a non-blocking acquire on a *separate* handle; if
    that succeeds the lock was free (released → owner is dead), so we release and
    report inactive. Used as corroborating liveness evidence, never as the sole
    ownership proof.
    """
    lock_path = _harness_dir(home) / "harness.lock"
    if not lock_path.exists():
        return False
    probe = _AdvisoryLock(lock_path)
    if probe.acquire():
        probe.release()
        return False
    return True


# ── Path helpers ─────────────────────────────────────────────────────────────
def _harness_dir(home: Path) -> Path:
    return Path(home) / _HARNESS_DIRNAME


def journal_db_path(home: Path) -> Path:
    """Return the run-journal DB path inside an isolated home (matches substrate)."""
    return Path(home) / "runtime" / "run_journal.db"


def _cookie_path(home: Path) -> Path:
    return _harness_dir(home) / "launch_cookie.json"


def _child_ack_path(home: Path) -> Path:
    return _harness_dir(home) / "child_ack.json"


def _resume_marker_path(home: Path) -> Path:
    return _harness_dir(home) / "resume_pending.json"


def _stop_marker_path(home: Path) -> Path:
    return _harness_dir(home) / "stop.request"


def _ready_file_path(home: Path) -> Path:
    return _harness_dir(home) / "ready.json"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_real_platform_home(home: Path) -> bool:
    """True when *home* resolves to the real platform-default Youtab home."""
    try:
        return home.resolve() == _get_platform_default_youtab_home().resolve()
    except OSError:
        # If we cannot resolve, be conservative and treat as real (refuse).
        return True


# ── Instance record ──────────────────────────────────────────────────────────
@dataclass
class HarnessInstance:
    """One spawned, harness-owned child process."""

    pid: int
    launch_token: str
    home: Path
    start_time: Optional[int]
    run_id: str
    mode: str
    process: subprocess.Popen = field(repr=False)


# ── The controller ───────────────────────────────────────────────────────────
class HarnessProcess:
    """Spawn and control a single harness-owned runtime instance.

    Construct with ``isolated_benchmark=True`` to enable launching + fault
    injection. There is intentionally no environment override for this flag.
    Usable as a context manager; :meth:`close` always reaps an owned child.
    """

    def __init__(
        self,
        *,
        isolated_benchmark: bool = False,
        home: Optional[Path] = None,
        principal: Optional[Principal] = None,
        repo_root: Optional[Path] = None,
        ready_timeout: float = 30.0,
    ) -> None:
        if not isolated_benchmark:
            raise HarnessFaultDisabled(
                "HarnessProcess requires isolated_benchmark=True; it exists only "
                "for the offline benchmark and never controls production processes"
            )
        self.isolated_benchmark = True
        self.principal = principal or Principal("harness", "benchmark")
        self.ready_timeout = float(ready_timeout)
        # Repo root that must be importable by the child subprocess.
        self.repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent

        if home is not None:
            self.home = Path(home)
            self._owns_home = False
        else:
            self.home = Path(tempfile.mkdtemp(prefix="youtab-harness-"))
            self._owns_home = True
        if _is_real_platform_home(self.home):
            raise HarnessError(
                "refusing to use the real platform Youtab home as a harness home"
            )
        _harness_dir(self.home).mkdir(parents=True, exist_ok=True)

        self.instance: Optional[HarnessInstance] = None
        self.launch_token: Optional[str] = None
        # Every per-launch token, in order, so process events (which are keyed by
        # launch token pre-run, per contract 3) can be read across restarts.
        self._launch_tokens: List[str] = []
        self._stdout_lines: List[str] = []
        self._stdout_lock = threading.Lock()
        self._sentinel_seen = threading.Event()
        self._reader: Optional[threading.Thread] = None

    # -- context manager --------------------------------------------------------
    def __enter__(self) -> "HarnessProcess":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- launch -----------------------------------------------------------------
    def _default_child_command(self) -> List[str]:
        bootstrap = (
            "import sys; sys.path.insert(0, {root!r}); "
            "from youtab_runtime.harness_process import run_harness_child; "
            "run_harness_child()"
        ).format(root=str(self.repo_root))
        return [sys.executable, "-c", bootstrap]

    @staticmethod
    def _assert_not_update(command: Sequence[str]) -> None:
        """Fail closed if *command* would run the excluded ``youtab update``."""
        tokens = [str(t).lower() for t in command]
        for i, tok in enumerate(tokens):
            base = os.path.basename(tok)
            is_youtab = base in ("youtab", "youtab.exe") or tok.endswith(
                ("youtab_agent_cli.main", "youtab_agent_cli/main.py")
            )
            if is_youtab:
                rest = tokens[i + 1 :]
                if _EXCLUDED_SUBCOMMAND in rest:
                    raise HarnessUpdateExcluded(
                        "the 'youtab update' command is hard-excluded from the "
                        "harness: it runs git reset --hard on the real checkout"
                    )
        # Belt-and-braces: also refuse the bare subcommand pairing anywhere.
        if _EXCLUDED_SUBCOMMAND in tokens and any(
            "youtab" in t for t in tokens
        ):
            raise HarnessUpdateExcluded(
                "refusing a command referencing 'youtab update'"
            )

    def launch(
        self,
        *,
        run_id: Optional[str] = None,
        mode: str = CHILD_MODE_RUN_ONCE,
        fault_plan: Optional[List[Dict[str, Any]]] = None,
        sentinel: str = SENTINEL_DASHBOARD,
        set_resume_pending: bool = False,
        command: Optional[Sequence[str]] = None,
        env_overrides: Optional[Dict[str, str]] = None,
    ) -> HarnessInstance:
        """Spawn one isolated, harness-owned child and record its ownership cookie.

        The child's ``YOUTAB_AGENT_HOME`` is *always* set explicitly to the
        isolated home. A fresh per-launch token is generated and injected. After
        spawn the ``(pid, start_time, launch_token)`` cookie is written to the
        isolated home so ownership survives across controller instances/restarts.
        """
        if self.instance is not None and self.is_alive():
            raise HarnessError("a child is already running; close/kill it first")
        if fault_plan and not self.isolated_benchmark:  # pragma: no cover - guarded above
            raise HarnessFaultDisabled("fault injection requires isolated benchmark mode")

        run_id = run_id or f"harness-run-{secrets.token_hex(8)}"
        token = secrets.token_hex(16)
        cmd = list(command) if command is not None else self._default_child_command()
        self._assert_not_update(cmd)

        # Reset per-launch stdout capture state.
        self._stdout_lines = []
        self._sentinel_seen = threading.Event()

        env = dict(os.environ)
        # ALWAYS set the isolated home explicitly (silent-fallback hazard).
        env[ENV_HOME] = str(self.home)
        env[ENV_LAUNCH_TOKEN] = token
        env[ENV_ISOLATED] = "1"
        env[ENV_READY_FILE] = str(_ready_file_path(self.home))
        env[ENV_RUN_ID] = run_id
        env[ENV_TENANT] = self.principal.tenant
        env[ENV_USER] = self.principal.user
        env[ENV_MODE] = mode
        env[ENV_SENTINEL] = sentinel
        env[ENV_SET_RESUME] = "1" if set_resume_pending else "0"
        env[ENV_FAULT_PLAN] = json.dumps(fault_plan or [])
        # Deterministic, unbuffered child stdout so the readiness barrier is prompt.
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if env_overrides:
            env.update(env_overrides)

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.repo_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        # Read start_time promptly while the process is (almost certainly) alive.
        start_time = get_process_start_time(proc.pid)
        self.launch_token = token
        self._launch_tokens.append(token)
        self.instance = HarnessInstance(
            pid=proc.pid,
            launch_token=token,
            home=self.home,
            start_time=start_time,
            run_id=run_id,
            mode=mode,
            process=proc,
        )
        # HARNESS-owned (pid, start_time) cookie in the isolated home.
        _atomic_write_json(
            _cookie_path(self.home),
            {
                "pid": proc.pid,
                "start_time": start_time,
                "launch_token": token,
                "run_id": run_id,
                "created_at_ns": time.time_ns(),
            },
        )
        self._start_stdout_reader(proc)
        return self.instance

    def _start_stdout_reader(self, proc: subprocess.Popen) -> None:
        def _reader() -> None:
            stream = proc.stdout
            if stream is None:  # pragma: no cover - defensive
                return
            try:
                for line in stream:
                    line = line.rstrip("\n")
                    with self._stdout_lock:
                        self._stdout_lines.append(line)
                    if _READY_SENTINEL_RE.search(line):
                        self._sentinel_seen.set()
            except (ValueError, OSError):
                # Stream closed underneath us during teardown — fine.
                pass
            finally:
                # Stream closed (child exited) — unblock any waiter.
                self._sentinel_seen.set()

        t = threading.Thread(target=_reader, name="harness-stdout", daemon=True)
        t.start()
        self._reader = t

    # -- readiness barrier ------------------------------------------------------
    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """Block until the child signals readiness or the bounded deadline lapses.

        Readiness is proven by *either* the stdout sentinel
        ``YOUTAB_AGENT_{DASHBOARD,BACKEND}_READY port=`` *or* the ready-file the
        child writes — never an arbitrary sleep. Returns False if the child dies
        or the deadline lapses before readiness.
        """
        deadline = time.monotonic() + (self.ready_timeout if timeout is None else timeout)
        inst = self.instance
        if inst is None:
            raise HarnessError("no child launched")
        ready_file = _ready_file_path(self.home)
        while time.monotonic() < deadline:
            if ready_file.exists():
                return True
            if self._sentinel_seen.wait(timeout=0.05):
                # Sentinel line seen (or stream closed). Confirm via ready-file
                # or a re-scan of captured lines so a closed-stream wake without
                # readiness returns the honest answer.
                if ready_file.exists():
                    return True
                with self._stdout_lock:
                    if any(_READY_SENTINEL_RE.search(ln) for ln in self._stdout_lines):
                        return True
                if inst.process.poll() is not None:
                    return ready_file.exists()
            if inst.process.poll() is not None:
                return ready_file.exists()
        return ready_file.exists()

    def require_ready(self, timeout: Optional[float] = None) -> None:
        if not self.wait_ready(timeout=timeout):
            raise HarnessNotReady(
                f"child pid={self.pid} did not reach readiness within deadline"
            )

    # -- ownership --------------------------------------------------------------
    @property
    def pid(self) -> Optional[int]:
        return self.instance.pid if self.instance else None

    def is_alive(self) -> bool:
        pid = self.pid
        return bool(pid is not None and _pid_exists(pid))

    def prove_ownership(self, pid: Optional[int] = None) -> OwnershipOutcome:
        """Prove that *pid* is a harness-owned child before any signal.

        Conjunction of three independent signals (RECON R4):
          1. the per-launch token in the on-disk cookie matches ours;
          2. the pid matches the cookie and the live process's ``start_time``
             still equals the recorded one (guards PID reuse);
          3. (corroborating) the child echoed our token in its ack file.

        Fail-closed: an indeterminate ``start_time`` yields ``AMBIGUOUS`` and a
        mismatched/absent token or reused pid yields ``NOT_OWNED``. A dead pid is
        ``NOT_OWNED`` (nothing to own). Never matches on a cmdline scan.
        """
        target = pid if pid is not None else self.pid
        if target is None:
            return OwnershipOutcome.NOT_OWNED

        cookie = _read_json(_cookie_path(self.home))
        if not cookie:
            return OwnershipOutcome.NOT_OWNED
        expected_token = self.launch_token
        if expected_token is not None and cookie.get("launch_token") != expected_token:
            return OwnershipOutcome.NOT_OWNED
        if cookie.get("pid") != target:
            return OwnershipOutcome.NOT_OWNED

        # Corroborating: the child must have echoed OUR token back. Absence is
        # tolerated (child may not have written it yet); a mismatch is fatal.
        ack = _read_json(_child_ack_path(self.home))
        if ack is not None and ack.get("launch_token") != cookie.get("launch_token"):
            return OwnershipOutcome.NOT_OWNED

        if not _pid_exists(target):
            # Stale/dead handle — there is nothing to own or signal.
            return OwnershipOutcome.NOT_OWNED

        live_start = get_process_start_time(target)
        recorded_start = cookie.get("start_time")
        if live_start is None or recorded_start is None:
            # Cannot prove the pid was not reused → refuse (fail closed).
            return OwnershipOutcome.AMBIGUOUS
        if int(live_start) != int(recorded_start):
            # Same pid, different process — PID reuse. Must not signal.
            return OwnershipOutcome.NOT_OWNED
        return OwnershipOutcome.OWNED

    # -- kill / restart ---------------------------------------------------------
    def kill(self, *, force: bool = True, pid: Optional[int] = None) -> OwnershipOutcome:
        """Terminate the owned child. Refuses unless ownership is proven OWNED.

        A live-but-unowned or ambiguous target raises
        :class:`HarnessOwnershipError` (fail closed). An already-dead target is a
        no-op returning ``NOT_OWNED``.
        """
        target = pid if pid is not None else self.pid
        outcome = self.prove_ownership(target)
        if outcome is OwnershipOutcome.OWNED:
            terminate_pid(int(target), force=force)
            self._emit_process(ProcessState.KILLED, pid=target, extra={"force": force})
            return outcome
        if target is not None and not _pid_exists(target):
            # Nothing to signal; safe no-op.
            return outcome
        raise HarnessOwnershipError(outcome, target)

    def request_graceful_stop(self) -> None:
        """Ask the child to drain and shut down cleanly (writes a stop marker)."""
        _atomic_write_json(
            _stop_marker_path(self.home),
            {"requested_at_ns": time.time_ns(), "by_pid": os.getpid()},
        )

    def restart(
        self,
        *,
        graceful: bool = False,
        mode: str = CHILD_MODE_RUN_ONCE,
        fault_plan: Optional[List[Dict[str, Any]]] = None,
        run_id: Optional[str] = None,
    ) -> HarnessInstance:
        """Restart the owned child against the SAME isolated home.

        The persisted ``resume_pending`` marker (left by an abrupt kill of an
        in-flight run) is claimed by the new child, so recovery is observable in
        the journal. ``graceful=True`` drains first; otherwise the current child
        is owned-killed abruptly.
        """
        inst = self.instance
        prev_run_id = inst.run_id if inst else None
        self._emit_process(
            ProcessState.RESTART_REQUESTED,
            pid=self.pid,
            extra={"graceful": graceful, "prev_run_id": prev_run_id},
        )
        if inst is not None and self.is_alive():
            if graceful:
                self.request_graceful_stop()
                self._wait_exit(timeout=self.ready_timeout)
                if self.is_alive():
                    # Drain window elapsed — fall back to an owned kill.
                    self.kill(force=True)
            else:
                self.kill(force=True)
        self._join_reader()
        # Clear any leftover graceful-stop request so a relaunched idle child is
        # not immediately stopped by a stale marker.
        try:
            _stop_marker_path(self.home).unlink()
        except OSError:
            pass
        return self.launch(
            run_id=run_id or prev_run_id,
            mode=mode,
            fault_plan=fault_plan,
        )

    def _wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_alive():
                return True
            time.sleep(0.05)
        return not self.is_alive()

    def wait_exit(self, timeout: float = 30.0) -> bool:
        """Public bounded wait for the child to exit."""
        return self._wait_exit(timeout)

    # -- journal helpers --------------------------------------------------------
    def _emit_process(
        self,
        state: ProcessState,
        *,
        pid: Optional[int],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[RunEvent]:
        token = self.launch_token
        if token is None:
            return None
        payload: Dict[str, Any] = {
            "launch_token": token,
            "pid": pid,
            "start_time": self.instance.start_time if self.instance else None,
            "youtab_home": str(self.home),
            "marker": str(state),
        }
        if extra:
            payload.update(extra)
        try:
            return append_event(
                token,
                self.principal,
                "process",
                str(state),
                payload,
                db_path=journal_db_path(self.home),
            )
        except Exception:
            # A journal write must never crash process control.
            return None

    def list_process_events(self) -> List[RunEvent]:
        if self.launch_token is None:
            return []
        return list_events(
            self.launch_token,
            self.principal,
            category="process",
            db_path=journal_db_path(self.home),
        )

    def list_all_process_events(self) -> List[RunEvent]:
        """Process events across every launch (each keyed by its launch token).

        ``restart_requested``/``killed`` are recorded under the *outgoing*
        instance's token, while the recovered instance's events use the new
        token; this aggregates them in launch order for cross-restart oracles.
        """
        events: List[RunEvent] = []
        for token in self._launch_tokens:
            events.extend(
                list_events(
                    token,
                    self.principal,
                    category="process",
                    db_path=journal_db_path(self.home),
                )
            )
        return events

    def list_run_events(
        self, run_id: Optional[str] = None, category: Optional[str] = None
    ) -> List[RunEvent]:
        rid = run_id or (self.instance.run_id if self.instance else None)
        if rid is None:
            return []
        return list_events(
            rid,
            self.principal,
            category=category,
            db_path=journal_db_path(self.home),
        )

    def resume_pending_exists(self) -> bool:
        return _resume_marker_path(self.home).exists()

    @property
    def stdout_lines(self) -> List[str]:
        with self._stdout_lock:
            return list(self._stdout_lines)

    # -- cleanup ----------------------------------------------------------------
    def _join_reader(self) -> None:
        reader = self._reader
        if reader is not None:
            reader.join(timeout=2.0)
        self._reader = None

    def close(self) -> None:
        """Reap this controller's direct child and release resources.

        Cleanup reaps via the :class:`subprocess.Popen` handle, not by pid
        number: while we hold the handle the OS cannot reuse the child's PID, so
        this is unambiguously *our* direct child and needs no ownership proof
        (unlike the public :meth:`kill`, which signals by pid and therefore must
        prove ownership first). Reaping first also lets the stdout reader thread
        unblock on EOF instead of deadlocking on ``stdout.close()``. Safe to call
        repeatedly.
        """
        inst = self.instance
        if inst is not None:
            proc = inst.process
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            # Child is dead → pipe is at EOF → the reader thread can finish.
            self._join_reader()
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
        self._join_reader()
        self.instance = None
        if self._owns_home:
            shutil.rmtree(self.home, ignore_errors=True)


# ── The default deterministic child ──────────────────────────────────────────
# Keep this module-level lock alive for the child's lifetime.
_CHILD_LOCK: Optional[_AdvisoryLock] = None


def _child_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def run_harness_child() -> None:
    """Entrypoint for the default deterministic harness child (see module doc).

    Runs ONLY against an isolated home (refuses the real platform home), acquires
    the advisory lock, echoes its launch token, records lifecycle/process events
    to the run journal, drives recovery from a persisted ``resume_pending``
    marker, applies a declarative fault plan, and emits the readiness sentinel.
    """
    global _CHILD_LOCK
    home = Path(_child_env(ENV_HOME))
    token = _child_env(ENV_LAUNCH_TOKEN)
    isolated = _child_env(ENV_ISOLATED) == "1"

    # Hard safety gate: never operate on an unset or real platform home.
    if not str(home) or not token or not isolated:
        sys.stderr.write("harness child: missing isolated home/token; refusing\n")
        os._exit(3)
    if _is_real_platform_home(home):
        sys.stderr.write("harness child: refusing to run against real platform home\n")
        os._exit(3)

    hdir = _harness_dir(home)
    hdir.mkdir(parents=True, exist_ok=True)

    principal = Principal(
        _child_env(ENV_TENANT, "harness"), _child_env(ENV_USER, "benchmark")
    )
    run_id = _child_env(ENV_RUN_ID) or f"harness-run-{secrets.token_hex(8)}"
    mode = _child_env(ENV_MODE, CHILD_MODE_RUN_ONCE)
    sentinel = _child_env(ENV_SENTINEL, SENTINEL_DASHBOARD)
    set_resume = _child_env(ENV_SET_RESUME) == "1"
    try:
        fault_plan = json.loads(_child_env(ENV_FAULT_PLAN, "[]")) or []
    except ValueError:
        fault_plan = []

    db = journal_db_path(home)
    pid = os.getpid()
    start_time = get_process_start_time(pid)

    # Acquire the child advisory lock (OS releases it on death = liveness).
    _CHILD_LOCK = _AdvisoryLock(hdir / "harness.lock")
    if not _CHILD_LOCK.acquire():
        sys.stderr.write("harness child: could not acquire advisory lock\n")
        os._exit(4)

    # Echo the launch token back (mutual binding for ownership proof).
    _atomic_write_json(
        _child_ack_path(home),
        {"launch_token": token, "pid": pid, "start_time": start_time},
    )

    def emit_process(state: ProcessState, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {
            "launch_token": token,
            "pid": pid,
            "start_time": start_time,
            "youtab_home": str(home),
            "marker": str(state),
        }
        if extra:
            payload.update(extra)
        try:
            append_event(token, principal, "process", str(state), payload, db_path=db)
        except Exception:
            pass

    def emit_lifecycle(kind: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {"reason": kind, "terminal_state": None}
        if extra:
            payload.update(extra)
        try:
            append_event(run_id, principal, "lifecycle", kind, payload, db_path=db)
        except Exception:
            pass

    emit_process(ProcessState.SPAWNED)

    # ── Recovery: claim a resume_pending marker left by a prior instance. ──
    resume_marker = _resume_marker_path(home)
    if resume_marker.exists():
        prior = _read_json(resume_marker) or {}
        emit_lifecycle("resume_claimed", {"recovered_from": prior})
        emit_process(ProcessState.RECOVERED, {"recovered_from": prior})
        try:
            resume_marker.unlink()
        except OSError:
            pass

    # ── Start the (fake) run. ──
    emit_lifecycle("run_started")
    if set_resume:
        # Mark an in-flight run so an abrupt kill leaves recoverable state.
        _atomic_write_json(
            resume_marker,
            {"run_id": run_id, "set_by_pid": pid, "set_at_ns": time.time_ns()},
        )
        emit_lifecycle("resume_pending_set", {"terminal_state": None})

    # ── Apply the declarative fault plan (isolated benchmark only). ──
    _apply_fault_plan(fault_plan, run_id, principal, db, home)

    # ── Readiness barrier: write the ready-file AND print the sentinel. ──
    port = 0
    _atomic_write_json(
        _ready_file_path(home),
        {"pid": pid, "port": port, "run_id": run_id, "launch_token": token},
    )
    emit_process(ProcessState.READY, {"port": port})
    stream_name = "BACKEND" if sentinel == SENTINEL_BACKEND else "DASHBOARD"
    sys.stdout.write(f"YOUTAB_AGENT_{stream_name}_READY port={port}\n")
    sys.stdout.flush()

    if mode == CHILD_MODE_IDLE:
        # Simulate an in-flight run: idle until asked to stop (or killed). An
        # abrupt kill here leaves resume_pending set for the next instance.
        stop_marker = _stop_marker_path(home)
        while not stop_marker.exists():
            time.sleep(0.05)
        # Graceful stop: complete the run and clear resume state.
        _complete_run(run_id, principal, db, home, resume_marker)
        emit_process(ProcessState.SHUTDOWN, {"graceful": True})
        os._exit(0)

    # run_once: complete immediately.
    _complete_run(run_id, principal, db, home, resume_marker)
    emit_process(ProcessState.SHUTDOWN, {"graceful": True})
    os._exit(0)


def _complete_run(
    run_id: str, principal: Principal, db: Path, home: Path, resume_marker: Path
) -> None:
    try:
        if resume_marker.exists():
            resume_marker.unlink()
    except OSError:
        pass
    try:
        append_event(
            run_id,
            principal,
            "lifecycle",
            "run_completed",
            {"reason": "run_completed", "terminal_state": "done"},
            db_path=db,
        )
    except Exception:
        pass


def _apply_fault_plan(
    fault_plan: List[Dict[str, Any]],
    run_id: str,
    principal: Principal,
    db: Path,
    home: Path,
) -> None:
    """Apply declarative faults, recording each in its natural journal category.

    Supported fault types:
      * ``provider_timeout`` / ``provider_error`` → a usage ``model_call`` event
        with ``usage_status="unknown"`` (never a fake zero) and a fault marker.
      * ``tool_timeout`` / ``tool_error`` → a tool_call + tool_result pair whose
        result status is ``timeout``/``error``.
      * ``event_interruption`` / ``abrupt_exit`` → emit partial events then
        ``os._exit`` WITHOUT completing the run, so ``resume_pending`` is left
        set and recovery is required (verified by later observable state).
    """
    for i, fault in enumerate(fault_plan):
        ftype = str(fault.get("type", "")).strip()
        if ftype in ("provider_timeout", "provider_error"):
            api_request_id = fault.get("api_request_id") or f"{run_id}:fault:{i}"
            status = "timeout" if ftype == "provider_timeout" else "error"
            try:
                append_event(
                    run_id,
                    principal,
                    "usage",
                    "model_call",
                    {
                        "api_request_id": api_request_id,
                        "provider": fault.get("provider", "harness-fake"),
                        "model": fault.get("model", "harness-fake"),
                        "api_mode": "responses",
                        "input_tokens": None,
                        "output_tokens": None,
                        "total_tokens": None,
                        "usage_status": "unknown",
                        "cost": {"amount_usd": None, "status": "unknown",
                                 "source": "harness_fault", "pricing_version": None},
                        "fault": {"type": ftype, "status": status},
                    },
                    dedupe_key=api_request_id,
                    db_path=db,
                )
            except Exception:
                pass
        elif ftype in ("tool_timeout", "tool_error"):
            tool_call_id = fault.get("tool_call_id") or f"{run_id}:tool:{i}"
            tool_name = fault.get("tool_name", "harness_fault_tool")
            status = "timeout" if ftype == "tool_timeout" else "error"
            try:
                append_event(
                    run_id, principal, "tool_call", tool_name,
                    {"tool_call_id": tool_call_id, "tool_name": tool_name,
                     "args_redacted": {}, "turn_id": None, "api_request_id": None},
                    db_path=db,
                )
                append_event(
                    run_id, principal, "tool_result", tool_name,
                    {"tool_call_id": tool_call_id, "tool_name": tool_name,
                     "status": status, "error_type": fault.get("error_type", ftype),
                     "error_message_redacted": fault.get("error_message"),
                     "duration_ms": fault.get("duration_ms", 0),
                     "result_digest": None},
                    db_path=db,
                )
            except Exception:
                pass
        elif ftype in ("event_interruption", "abrupt_exit"):
            # Emit a partial marker, then die WITHOUT completing → resume_pending
            # (if set) remains for the next instance to recover.
            try:
                append_event(
                    run_id, principal, "lifecycle", "run_failed",
                    {"reason": ftype, "terminal_state": None, "partial": True},
                    db_path=db,
                )
            except Exception:
                pass
            sys.stdout.flush()
            os._exit(int(fault.get("exit_code", 42)))
        # Unknown fault types are ignored (fail-safe: never fabricate behaviour).


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    run_harness_child()
