"""Typed fail-closed contract for managed file effects on a REMOTE backend.

Production managed file operations run over
``tools/file_operations.py::ShellFileOperations``, which can target a LOCAL host
shell OR a REMOTE / container / SSH / modal sandbox via
``terminal_env.execute(shell_command)``.

The host ``os.open`` grant enforcement (:mod:`youtab_runtime.grant_fs`) is valid
ONLY for the local host filesystem: the Runtime holds a real file descriptor to
the host path and can enforce the grant against it. For a remote sandbox the
Runtime cannot ``os.open`` the target at all, so it CANNOT enforce the grant on
the far side. Therefore managed *remote* file mutation MUST fail closed until a
sandbox-side enforcement adapter exists that re-does the grant check where the
bytes actually land.

This module defines that typed contract:

* :class:`RemoteFileEffectRequest` — the frozen, canonical-JSON payload a future
  sandbox-side enforcer receives (mirrors the
  :mod:`youtab_runtime.effect_authorization` / :mod:`youtab_runtime.approval`
  idiom: ``extra="forbid"``, ``frozen``, a ``canonical_payload()`` producing
  sorted-key compact JSON bytes, and the shared ``effect_digest`` shape).
* :class:`RemoteFileEffectResult` — the frozen result such an enforcer returns.
* :class:`ManagedRemoteFileExecutor` — the Protocol the enforcer implements.
* :class:`NullRemoteFileExecutor` — the PRODUCTION DEFAULT: no adapter is wired,
  so ``execute`` ALWAYS raises :class:`RemoteFileAuthorityUnavailableError`
  (status :data:`MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE`).
* :class:`ReferenceRemoteFileExecutor` — a DETERMINISTIC in-process reference
  sandbox that actually performs the op against a supplied reference root so the
  *contract shape* can be exercised in tests. It is labelled
  ``provenance="reference"`` and MUST NOT be selectable as a production / live
  executor.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE",
    "RemoteFileAuthorityUnavailableError",
    "RemoteFileEffectRequest",
    "RemoteFileEffectResult",
    "ManagedRemoteFileExecutor",
    "NullRemoteFileExecutor",
    "ReferenceRemoteFileExecutor",
    "default_remote_executor",
]

#: Explicit fail-closed status string surfaced whenever managed remote file
#: mutation is refused because no sandbox-side enforcement adapter exists.
MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE = (
    "MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE"
)


class RemoteFileAuthorityUnavailableError(Exception):
    """Fail-closed: managed remote file mutation is refused.

    The host grant enforcement cannot reach a remote sandbox filesystem, and no
    sandbox-side enforcer is wired, so no effect may be claimed. Both ``str()``
    and :attr:`status` equal
    :data:`MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE`.
    """

    status: str = MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE

    def __init__(self, message: str = MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE):
        super().__init__(message)
        self.status = MANAGED_REMOTE_FILESYSTEM_AUTHORITY_UNAVAILABLE


class RemoteFileEffectRequest(BaseModel):
    """The typed contract a future sandbox-side enforcer receives.

    Frozen and ``extra="forbid"`` so the payload cannot be silently widened or
    mutated after construction. ``effect_digest`` shares the exact shape produced
    by :func:`youtab_runtime.approval.compute_effect_digest` (hex sha256), so a
    remote enforcer can re-bind the authorization to the same effect identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.remote-file-effect.v1"] = (
        "youtab.remote-file-effect.v1"
    )
    canonical_sandbox_root: str = Field(min_length=1, max_length=4096)
    workspace_id: str = Field(min_length=1, max_length=128)
    grant_ref: str = Field(min_length=1, max_length=256)
    authorization_ref: str = Field(min_length=1, max_length=256)
    operation: Literal["read", "write", "create", "delete", "move"]
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=256)
    result_digest: Optional[str] = Field(default=None, max_length=64)
    receipt_ref: Optional[str] = Field(default=None, max_length=256)
    reconciliation_ref: Optional[str] = Field(default=None, max_length=256)

    def canonical_payload(self) -> bytes:
        """Sorted-key compact JSON bytes, deterministic across processes."""
        payload = self.model_dump(mode="json")
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


class RemoteFileEffectResult(BaseModel):
    """Frozen result a sandbox-side enforcer returns for one effect.

    ``provenance`` is authoritative: ``"live"`` means a real, grant-enforcing
    sandbox adapter performed the effect; ``"reference"`` means the deterministic
    in-process reference sandbox did, and the result MUST NOT be treated as a
    production effect.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed: bool
    result_digest: Optional[str] = Field(default=None, max_length=64)
    provenance: Literal["live", "reference"]
    receipt_ref: Optional[str] = Field(default=None, max_length=256)


@runtime_checkable
class ManagedRemoteFileExecutor(Protocol):
    """A sandbox-side enforcer that performs one managed remote file effect."""

    def execute(self, request: RemoteFileEffectRequest) -> RemoteFileEffectResult:
        ...


class NullRemoteFileExecutor:
    """PRODUCTION DEFAULT — fail closed.

    No sandbox-side enforcement adapter is wired, so every managed remote file
    effect is refused: :meth:`execute` ALWAYS raises
    :class:`RemoteFileAuthorityUnavailableError`. This is the only executor that
    may be selected in production until a real, grant-enforcing sandbox adapter
    exists.
    """

    def execute(self, request: RemoteFileEffectRequest) -> RemoteFileEffectResult:
        raise RemoteFileAuthorityUnavailableError()


def default_remote_executor() -> ManagedRemoteFileExecutor:
    """Return the production default executor (fail closed)."""
    return NullRemoteFileExecutor()


class ReferenceRemoteFileExecutor:
    """DETERMINISTIC in-process reference sandbox — TESTS/CONTRACT ONLY.

    Performs the op against a supplied reference root directory so the CONTRACT
    shape can be exercised end to end. It is NOT a real sandbox and NOT
    grant-enforcing: every result it returns is stamped ``provenance="reference"``
    and it MUST NEVER be selected as a production / live executor. It exists only
    to prove the request/result shapes, never to serve real effects.

    Paths inside the request are treated as relative to ``reference_root``; any
    attempt to escape the reference root fails closed with ``ValueError``.
    """

    #: Marker so callers/tests can assert this is never a production executor.
    is_reference_only: bool = True

    def __init__(self, reference_root: Path):
        self._root = Path(reference_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative: str) -> Path:
        candidate = (self._root / relative).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError("reference path escapes the reference root")
        return candidate

    def execute(self, request: RemoteFileEffectRequest) -> RemoteFileEffectResult:
        # The relative target inside the reference sandbox is carried by
        # ``canonical_sandbox_root`` for this minimal reference (a real adapter
        # would carry a structured path). Keep it minimal and deterministic.
        target = self._resolve(request.canonical_sandbox_root)
        result_digest: Optional[str] = None

        if request.operation in ("write", "create"):
            target.parent.mkdir(parents=True, exist_ok=True)
            # Deterministic content derived from the request digest so the
            # reference is reproducible without external inputs.
            data = request.request_digest.encode("utf-8")
            target.write_bytes(data)
            result_digest = hashlib.sha256(data).hexdigest()
        elif request.operation == "read":
            data = target.read_bytes()
            result_digest = hashlib.sha256(data).hexdigest()
        elif request.operation == "delete":
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        elif request.operation == "move":
            destination = self._resolve(request.idempotency_key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(destination))
            result_digest = hashlib.sha256(
                request.idempotency_key.encode("utf-8")
            ).hexdigest()

        # provenance is HARD-CODED reference; this executor cannot emit "live".
        return RemoteFileEffectResult(
            executed=True,
            result_digest=result_digest,
            provenance="reference",
            receipt_ref=None,
        )
