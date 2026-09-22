"""Runtime-owned worker/subprocess boundary for enterprise execution.

The connector never calls a provider in-process. It crosses this boundary, which
spawns a real Runtime worker subprocess, sends a typed JSON request, enforces a
deadline (cancellation), and returns ONLY the typed result data. The parent mints
the receipt itself, so nothing the subprocess writes can forge one.

Fail-closed outcomes the boundary distinguishes (mapped by the connector to an
``unknown`` / reconciliation-required effect, never a silent success):
  * :class:`WorkerTimeout` — the deadline elapsed; the process is killed.
  * :class:`WorkerCrash`   — non-zero exit or no parseable envelope.
  * :class:`WorkerMalformed` — output is not the typed ``{ok, result}`` envelope.
  * :class:`WorkerRefused` — the worker returned ``{ok: false, error}``.

The child environment is scrubbed to a minimal allowlist (no inherited process
secrets reach a reference provider).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "WorkerBoundaryError",
    "WorkerTimeout",
    "WorkerCrash",
    "WorkerMalformed",
    "WorkerRefused",
    "WorkerResult",
    "WorkerBoundary",
]


class WorkerBoundaryError(RuntimeError):
    """Base class for a worker-boundary failure (outcome unproven → fail-closed)."""


class WorkerTimeout(WorkerBoundaryError):
    """The worker exceeded its deadline and was killed (cancellation reached it)."""


class WorkerCrash(WorkerBoundaryError):
    """The worker exited non-zero or produced no parseable envelope."""


class WorkerMalformed(WorkerBoundaryError):
    """The worker's output was not the typed {ok, result} envelope."""


class WorkerRefused(WorkerBoundaryError):
    """The worker returned {ok: false, error}."""


@dataclass(frozen=True)
class WorkerResult:
    result: Mapping[str, Any]
    pid: int
    exit_code: int


#: Env vars a reference worker legitimately needs (no secrets). Windows needs
#: SystemRoot/COMSPEC for the interpreter to start.
_ENV_ALLOWLIST = ("PYTHONPATH", "PYTHONHASHSEED", "SystemRoot", "COMSPEC", "PATH", "TEMP", "TMP")


class WorkerBoundary:
    def __init__(
        self,
        *,
        python_argv: Optional[Sequence[str]] = None,
        repo_root: Optional[str] = None,
        env_overrides: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._argv = list(
            python_argv or [sys.executable, "-m", "youtab_runtime.enterprise.worker"]
        )
        self._repo_root = repo_root or os.getcwd()
        self._env_overrides = dict(env_overrides or {})

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
        try:
            proc = subprocess.run(
                self._argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=deadline_seconds,
                env=self._child_env(),
                cwd=self._repo_root,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkerTimeout(
                f"worker exceeded deadline {deadline_seconds}s"
            ) from exc
        if proc.returncode != 0:
            raise WorkerCrash(
                f"worker exited {proc.returncode}: {proc.stderr[:400]}"
            )
        out = (proc.stdout or "").strip()
        if not out:
            raise WorkerCrash("worker produced no output")
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
        return WorkerResult(result=result, pid=-1, exit_code=proc.returncode)
