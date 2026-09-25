"""PID-incarnation identity for safe liveness + cancellation (WAVE-30H R7 core).

A bare PID is not a safe handle: the OS recycles PID numbers, so a process that
exited can have its PID reused by an unrelated process. Checking ``pid exists``
then acting (kill / "still running") can therefore (a) read a dead worker as
alive, or (b) — far worse — signal/kill the innocent process that now owns that
PID number.

An *incarnation token* binds the PID to the process's start time
(``(pid, start_time)`` uniquely identifies one process instance on a host), so a
recycled PID yields a different token and is never mistaken for the original.
This module is the single primitive both R5 (reaping crash-leaked concurrency
permits) and R7 (incarnation-safe cancellation) build on.

Self-contained (no ``gateway`` import — that would invert the youtab_runtime
layering): ``psutil`` is already a hard dependency and gives cross-platform
existence + creation time, with a fast ``/proc`` path for start time on Linux.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Sentinel used when the OS would not tell us a start time; two unknowns must
# NEVER compare equal, so we never treat an unverifiable pair as "same process".
_UNKNOWN = "?"


def process_start_time(pid: int) -> Optional[int]:
    """Stable per-process start-time fingerprint, or None if unavailable/dead.

    Linux: field 22 of ``/proc/<pid>/stat`` (clock ticks since boot). Elsewhere:
    ``psutil.Process(pid).create_time()`` quantized to centiseconds for stable
    integer equality. The two sources are never mixed on one host, and the value
    is only ever compared against another reading on the SAME host, so differing
    units across platforms are irrelevant.
    """
    if not pid or pid <= 0:
        return None
    try:
        return int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        pass
    try:
        import psutil  # type: ignore

        return int(round(psutil.Process(pid).create_time() * 100))
    except Exception:  # noqa: BLE001 - no such process / access / psutil absent
        return None


def pid_exists(pid: int) -> bool:
    """True if a process with ``pid`` currently exists (any state).

    Uses ``psutil.pid_exists`` (safe on Windows, unlike ``os.kill(pid, 0)`` which
    Python maps to ``CTRL_C_EVENT`` there). Treats a zombie as non-existent where
    detectable so a post-exit/pre-reap worker is not read as alive.
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        if not psutil.pid_exists(int(pid)):
            return False
        try:
            proc = psutil.Process(int(pid))
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
        except Exception:  # noqa: BLE001 - gone between check and status()
            return False
        return True
    except Exception:  # noqa: BLE001 - psutil unavailable: fall back to POSIX kill(0)
        if os.name == "posix":
            try:
                os.kill(int(pid), 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True  # exists, owned by another user
            except OSError:
                return False
        return False


def incarnation_token(pid: int) -> str:
    """A ``"pid:start_time"`` token identifying THIS incarnation of ``pid``.

    If the start time cannot be read the token ends in ``:?`` — two such tokens
    never compare equal (see :func:`same_incarnation`), so an unverifiable
    process is never treated as the same incarnation as any other.
    """
    st = process_start_time(pid)
    return f"{int(pid)}:{st if st is not None else _UNKNOWN}"


def current_incarnation() -> str:
    """The incarnation token for the current process."""
    return incarnation_token(os.getpid())


def same_incarnation(pid: int, token: Optional[str]) -> bool:
    """True iff ``pid`` is alive AND its current incarnation token == ``token``.

    Fail-closed: a missing token, a dead PID, or an unreadable start time
    (``:?``) all return False — a recycled PID (same number, new start time)
    yields a different token and is correctly rejected.
    """
    if not token:
        return False
    if token.endswith(f":{_UNKNOWN}"):
        return False
    if not pid_exists(pid):
        return False
    return incarnation_token(pid) == token


def is_alive_incarnation(pid: Optional[int], token: Optional[str]) -> bool:
    """Reaper-facing liveness: is the recorded ``(pid, token)`` owner still alive?

    Used as ``execution_tree_budget.reap_dead_agents(is_alive=...)`` so a permit
    is reclaimed only when its owning incarnation is genuinely gone — never when a
    recycled PID happens to be live under a different incarnation.
    """
    if pid is None:
        return False
    return same_incarnation(int(pid), token)
