"""Runtime-owned worker/subprocess boundary for enterprise execution.

The connector never calls a provider in-process. It crosses this boundary, which
spawns a real Runtime worker subprocess, sends a typed JSON request, enforces a
deadline (cancellation) with **process-tree containment**, and returns ONLY the
typed result data. The parent mints the receipt itself, so nothing the subprocess
writes can forge one.

Fail-closed outcomes the boundary distinguishes (mapped by the connector to an
``unknown`` / reconciliation-required effect, never a silent success):
  * :class:`WorkerTimeout` — the deadline elapsed; the whole process tree is killed.
  * :class:`WorkerCrash`   — non-zero exit or no parseable envelope.
  * :class:`WorkerMalformed` — output is not the typed ``{ok, result}`` envelope.
  * :class:`WorkerRefused` — the worker returned ``{ok: false, error}``.

Process-tree containment: a timeout terminates the COMPLETE child process tree,
not just the direct python child, so a worker that spawned a grandchild cannot
leave an orphan running. On Windows a Job Object (pywin32 ``win32job``, a locked
dependency) is used; if pywin32 is unavailable at runtime the boundary falls back
to ``CREATE_NEW_PROCESS_GROUP`` + ``taskkill /T /F /PID``. On POSIX the child is
its own session leader (``start_new_session=True``) and the whole process group is
killed with ``os.killpg``.

Lease heartbeat / settlement: :class:`LeaseHeartbeat` renews a long-running
worker's lease atomically (holder-only) via the Lane-1 ``worker_lease`` module,
and :func:`settle_with_lease` refuses to settle when the lease has been lost —
both are thin CALLERS of Lane-1 and never mutate a Lane-1-owned file.

The child environment is scrubbed to a minimal allowlist (no inherited process
secrets reach a reference provider).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime import worker_lease as _lease
from youtab_runtime.run_journal import Principal
from youtab_runtime.run_states import EffectState

__all__ = [
    "WorkerBoundaryError",
    "WorkerTimeout",
    "WorkerCrash",
    "WorkerMalformed",
    "WorkerRefused",
    "WorkerResult",
    "WorkerBoundary",
    "LeaseHeartbeat",
    "SettlementError",
    "settle_with_lease",
    "win32_job_available",
]

_IS_WINDOWS = sys.platform == "win32"


class WorkerBoundaryError(RuntimeError):
    """Base class for a worker-boundary failure (outcome unproven → fail-closed)."""


class WorkerTimeout(WorkerBoundaryError):
    """The worker exceeded its deadline and the whole tree was killed."""


class WorkerCrash(WorkerBoundaryError):
    """The worker exited non-zero or produced no parseable envelope."""


class WorkerMalformed(WorkerBoundaryError):
    """The worker's output was not the typed {ok, result} envelope."""


class WorkerRefused(WorkerBoundaryError):
    """The worker returned {ok: false, error}."""


class SettlementError(WorkerBoundaryError):
    """Fail-closed: settlement was attempted without a held/valid lease."""


@dataclass(frozen=True)
class WorkerResult:
    result: Mapping[str, Any]
    pid: int
    exit_code: int


#: Env vars a reference worker legitimately needs (no secrets). Windows needs
#: SystemRoot/COMSPEC for the interpreter to start.
_ENV_ALLOWLIST = ("PYTHONPATH", "PYTHONHASHSEED", "SystemRoot", "COMSPEC", "PATH", "TEMP", "TMP")


def win32_job_available() -> bool:
    """True when the pywin32 Job-Object primitives are importable on Windows.

    When False on Windows the boundary uses the ``CREATE_NEW_PROCESS_GROUP`` +
    ``taskkill /T /F`` fallback for tree containment.
    """
    if not _IS_WINDOWS:
        return False
    try:
        import win32api  # noqa: F401
        import win32con  # noqa: F401
        import win32job  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure means "not usable"
        return False
    return True


# --------------------------------------------------------------------------- #
# Process-tree containment                                                     #
# --------------------------------------------------------------------------- #
class _JobHandle:
    """Owns the Windows Job Object a child (and its descendants) is assigned to.

    Terminating the job kills the COMPLETE tree; KILL_ON_JOB_CLOSE guarantees the
    tree also dies if the parent crashes without an explicit terminate.
    """

    def __init__(self) -> None:
        import win32job

        self._win32job = win32job
        self._job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            self._job, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            self._job, win32job.JobObjectExtendedLimitInformation, info
        )

    def assign(self, pid: int) -> None:
        import win32api
        import win32con

        rights = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
        hproc = win32api.OpenProcess(rights, False, pid)
        try:
            self._win32job.AssignProcessToJobObject(self._job, hproc)
        finally:
            hproc.Close()

    def terminate(self) -> None:
        try:
            self._win32job.TerminateJobObject(self._job, 1)
        except Exception:  # noqa: BLE001 — best-effort; handle close still kills
            pass

    def close(self) -> None:
        if self._job is not None:
            self._job.Close()
            self._job = None


def _spawn(argv: Sequence[str], *, env: Mapping[str, str], cwd: str):
    """Spawn the worker with the platform tree-containment primitive.

    Returns ``(proc, killer)`` where ``killer()`` terminates the whole tree.
    """
    if _IS_WINDOWS:
        if win32_job_available():
            job = _JobHandle()
            proc = subprocess.Popen(  # noqa: S603 — argv list, shell=False
                list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=dict(env), cwd=cwd, shell=False,
            )
            # Assign the child (and every descendant it spawns hereafter) to the
            # job. The worker defers spawning any grandchild until after it has
            # read its request, so the assignment always wins the race.
            try:
                job.assign(proc.pid)
            except Exception:  # noqa: BLE001 — fall back to taskkill on assign fail
                job.close()
                return proc, _taskkill_tree(proc)

            def _job_killer() -> None:
                job.terminate()
                job.close()

            return proc, _job_killer
        # No pywin32: new process group + taskkill /T /F tree kill.
        proc = subprocess.Popen(  # noqa: S603
            list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=dict(env), cwd=cwd, shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return proc, _taskkill_tree(proc)

    # POSIX: child becomes its own session leader; kill the whole group.
    proc = subprocess.Popen(  # noqa: S603
        list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=dict(env), cwd=cwd, shell=False,
        start_new_session=True,
    )

    def _posix_killer() -> None:
        # POSIX-only os primitives (guarded: this branch never runs on Windows).
        killpg = getattr(os, "killpg")
        getpgid = getattr(os, "getpgid")
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            killpg(getpgid(proc.pid), sigkill)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    return proc, _posix_killer


def _taskkill_tree(proc: "subprocess.Popen") -> Callable[[], None]:
    def killer() -> None:
        try:
            subprocess.run(  # noqa: S603 — fixed argv, shell=False
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10, shell=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    return killer


class WorkerBoundary:
    def __init__(
        self,
        *,
        python_argv: Optional[Sequence[str]] = None,
        repo_root: Optional[str] = None,
        env_overrides: Optional[Mapping[str, str]] = None,
        max_request_bytes: int = 1_000_000,
        max_response_bytes: int = 4_000_000,
    ) -> None:
        self._argv = list(
            python_argv or [sys.executable, "-m", "youtab_runtime.enterprise.worker"]
        )
        self._repo_root = repo_root or os.getcwd()
        self._env_overrides = dict(env_overrides or {})
        self._max_request_bytes = int(max_request_bytes)
        self._max_response_bytes = int(max_response_bytes)

    def _child_env(self) -> dict:
        env = {}
        for k in _ENV_ALLOWLIST:
            if k in os.environ:
                env[k] = os.environ[k]
        # Ensure the worker can import youtab_runtime.
        existing = env.get("PYTHONPATH", "")
        parts = [self._repo_root] + ([existing] if existing else [])
        env["PYTHONPATH"] = os.pathsep.join(parts)
        env.update(self._env_overrides)
        return env

    def run(self, request: Mapping[str, Any], *, deadline_seconds: float) -> WorkerResult:
        payload = json.dumps(request)
        # Bounded request size: refuse an oversized request before spawning.
        if len(payload.encode("utf-8")) > self._max_request_bytes:
            raise WorkerBoundaryError("worker request exceeds size bound")

        # shell=False (argv is a list), scrubbed env, controlled cwd. The child is
        # spawned inside a process-tree container (Job Object / process group), so
        # a timeout kills the COMPLETE tree — a grandchild cannot orphan.
        proc, kill_tree = _spawn(
            self._argv, env=self._child_env(), cwd=self._repo_root
        )
        try:
            out_b, err_b = proc.communicate(
                input=payload.encode("utf-8"), timeout=deadline_seconds
            )
        except subprocess.TimeoutExpired as exc:
            kill_tree()
            # Reap the direct child so no zombie/handle leaks after the tree kill.
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
            raise WorkerTimeout(
                f"worker exceeded deadline {deadline_seconds}s"
            ) from exc
        finally:
            if _IS_WINDOWS and win32_job_available():
                # Closing the job handle (KILL_ON_JOB_CLOSE) is idempotent and
                # guarantees no descendant survives a normal return either.
                kill_tree()

        returncode = proc.returncode
        stderr = (err_b or b"").decode("utf-8", "replace")
        if returncode != 0:
            raise WorkerCrash(f"worker exited {returncode}: {stderr[:400]}")
        out = (out_b or b"").decode("utf-8", "replace").strip()
        if not out:
            raise WorkerCrash("worker produced no output")
        # Bounded response size.
        if len(out.encode("utf-8")) > self._max_response_bytes:
            raise WorkerMalformed("worker response exceeds size bound")
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError as exc:
            raise WorkerMalformed(f"worker output not JSON: {out[:200]}") from exc
        if not isinstance(envelope, dict) or "ok" not in envelope:
            raise WorkerMalformed("worker output missing 'ok'")
        if envelope["ok"] is not True:
            raise WorkerRefused(str(envelope.get("error", "worker refused")))
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise WorkerMalformed("worker 'result' must be an object")
        # Only the typed result crosses back; any other top-level field (e.g. a
        # forged 'receipt') is deliberately ignored here.
        return WorkerResult(result=result, pid=proc.pid, exit_code=returncode)


# --------------------------------------------------------------------------- #
# Lease heartbeat / renewal (Lane-1 caller)                                    #
# --------------------------------------------------------------------------- #
class LeaseHeartbeat:
    """Renew a long-running worker's lease while work is active.

    Thin caller of the Lane-1 ``worker_lease.renew_lease`` (atomic, holder-only
    compare-and-set). Each renewal rotates the owner token and pushes the expiry
    forward, so a stale holder is atomically invalidated. Use :meth:`renew_now`
    for a synchronous renewal or :meth:`start`/:meth:`stop` for a background
    thread. ``current_token`` is the only token that can settle after a renewal.
    """

    def __init__(
        self,
        effect_id: str,
        principal: Principal,
        initial_token: str,
        *,
        lease_seconds: float,
        clock: Callable[[], float] = time.time,
        token_factory: Optional[Callable[[], str]] = None,
        db_path: Optional[Path] = None,
    ) -> None:
        self._effect_id = effect_id
        self._principal = principal
        self._lease_seconds = float(lease_seconds)
        self._clock = clock
        self._db_path = db_path
        self._token = str(initial_token)
        self._counter = 0
        self._token_factory = token_factory or self._default_token
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _default_token(self) -> str:
        self._counter += 1
        return f"{self._token}-r{self._counter}"

    @property
    def current_token(self) -> str:
        with self._lock:
            return self._token

    def renew_now(self) -> str:
        """Atomically rotate to a fresh token + expiry; return the new token."""
        with self._lock:
            new_token = self._token_factory()
            _lease.renew_lease(
                self._effect_id, self._principal, self._token, new_token,
                new_expires_at=self._clock() + self._lease_seconds,
                db_path=self._db_path,
            )
            self._token = new_token
            return new_token

    def start(self, *, interval_seconds: float) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat already started")
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(interval_seconds):
                try:
                    self.renew_now()
                except _lease.LeaseError:
                    # Lease lost (swept/reacquired elsewhere): stop renewing so
                    # settlement fails closed rather than a stale holder winning.
                    break

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def settle_with_lease(
    effect_id: str,
    principal: Principal,
    owner_token: str,
    *,
    detail: Optional[Mapping[str, Any]] = None,
    db_path: Optional[Path] = None,
) -> str:
    """Commit an effect ONLY if ``owner_token`` still holds a valid lease.

    Fail-closed (:class:`SettlementError`) when the caller is not the lease
    holder (Lane-1 ``assert_lease_holder``) or the effect is no longer
    ``in_progress`` (e.g. swept to ``reconciliation_required`` / dead-lettered).
    Returns the resulting terminal state value.
    """
    try:
        _lease.assert_lease_holder(effect_id, principal, owner_token, db_path=db_path)
    except _lease.LeaseError as exc:
        raise SettlementError(f"lease not held; refusing to settle: {exc}") from exc
    rec = _ledger.get_effect(effect_id, principal, db_path=db_path)
    if rec is None:
        raise SettlementError("effect not found or not owned")
    if rec.state != EffectState.IN_PROGRESS:
        # A sweep moved it out of in_progress -> the lease is logically lost.
        raise SettlementError(
            f"effect not in progress (state={rec.state.value}); lease lost"
        )
    committed = _ledger.mark_committed(
        effect_id, principal, detail=dict(detail or {}), db_path=db_path
    )
    return committed.state.value
