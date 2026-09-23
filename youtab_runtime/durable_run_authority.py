"""Exclusive run-execution authority for the durable ``/v1/runs`` server.

Server durable mode is a **store-enforced singleton**: exactly one Runtime
process instance may admit, execute and commit ``/v1/runs`` work against a
given durable store. The authority is acquired before the server is ready and
held for the whole effectful lifetime.

- PostgreSQL: a session-level advisory lock on a dedicated, supervised
  connection (see ``durable_run_store_pg.PostgresInstanceAuthority``).
- SQLite: an exclusive OS file lock beside the database file.

Every acquisition bumps a durable ``runtime_authority.epoch`` and records a
fresh random ``instance_id``. Runs are owner-stamped ``inst:<instance_id>:<epoch>``
instead of a numeric PID, so neither PID reuse nor a PID-namespace collision
can make a dead prior instance look alive. Only after exclusive authority is
proven may startup move prior owner-stamped nonterminal runs to UNKNOWN.

While an authority is attached to a store, every durable write verifies it in
the same transaction (fencing). A process whose authority is lost or superseded
can no longer admit, progress or commit a terminal result; the server then
fail-stops (see ``APIServerAdapter._on_run_authority_lost``).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from typing import Callable, List, Optional

from youtab_runtime.durable_run_store import DurableRunError

logger = logging.getLogger(__name__)

# The one authority scope for the shipped /v1/runs server. A store holds exactly
# one scope: startup reconciliation treats every other owner of an
# ``operation='run'`` row as a prior instance.
RUNS_AUTHORITY_SCOPE = "v1_runs"
OWNER_PREFIX = "inst:"


class RunAuthorityError(DurableRunError):
    """Base class for run-execution authority failures (fail-closed)."""


class AuthorityHeld(RunAuthorityError):
    """Another live instance holds the authority; this instance must not start."""


class AuthorityLost(RunAuthorityError):
    """This instance no longer holds the authority; no further durable effect."""


class InstanceAuthority:
    """A held (or formerly held) exclusive authority. Backend-neutral part.

    ``ensure_held()`` is the in-process fast path; stores additionally verify the
    authority durably inside each write transaction.
    """

    def __init__(self, *, scope: str, instance_id: str, epoch: int):
        self.scope = scope
        self.instance_id = instance_id
        self.epoch = int(epoch)
        self._lock = threading.Lock()
        self._lost_reason: Optional[str] = None
        self._released = False
        self._listeners: List[Callable[[str], None]] = []

    @property
    def owner(self) -> str:
        """The lease_owner stamped on runs admitted under this authority."""
        return f"{OWNER_PREFIX}{self.instance_id}:{self.epoch}"

    @property
    def held(self) -> bool:
        return self._lost_reason is None and not self._released

    @property
    def lost_reason(self) -> Optional[str]:
        return self._lost_reason

    def ensure_held(self) -> None:
        if self._released:
            raise AuthorityLost("run authority released")
        if self._lost_reason is not None:
            raise AuthorityLost(f"run authority lost: {self._lost_reason}")

    def add_loss_listener(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._listeners.append(callback)
            already = self._lost_reason
        if already is not None:
            callback(already)

    def mark_lost(self, reason: str) -> None:
        """Record authority loss once and notify listeners (fail-stop hooks).

        A graceful ``release()`` is not a loss and never notifies."""
        with self._lock:
            if self._lost_reason is not None or self._released:
                return
            self._lost_reason = reason
            listeners = list(self._listeners)
        logger.critical(
            "durable run authority LOST (scope=%s epoch=%d instance=%s): %s",
            self.scope, self.epoch, self.instance_id, reason,
        )
        for cb in listeners:
            try:
                cb(reason)
            except Exception:
                logger.exception("run authority loss listener failed")

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        try:
            self._release_backend()
        except Exception:
            logger.debug("run authority release failed", exc_info=True)

    def _release_backend(self) -> None:  # pragma: no cover - overridden
        pass


def new_instance_id() -> str:
    return uuid.uuid4().hex


def holder_metadata() -> dict:
    """Diagnostic identity recorded with the epoch (never used for liveness)."""
    return {"host": socket.gethostname(), "os_pid": os.getpid()}


class FileInstanceAuthority(InstanceAuthority):
    """SQLite backend authority: an exclusive, non-blocking OS file lock that the
    OS releases only when this process closes it or dies."""

    def __init__(self, *, scope: str, instance_id: str, epoch: int, handle):
        super().__init__(scope=scope, instance_id=instance_id, epoch=epoch)
        self._handle = handle

    def _release_backend(self) -> None:
        release_file_lock(self._handle)


def acquire_file_lock(path: str):
    """Open ``path`` and take an exclusive non-blocking lock; raise AuthorityHeld."""
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AuthorityHeld(
            "another Runtime instance holds the durable run authority for this store"
        ) from None
    except Exception:
        handle.close()
        raise
    return handle


def release_file_lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
