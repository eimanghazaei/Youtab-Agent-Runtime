"""Durable interrupted-turn markers for the desktop/TUI auto-continue path.

A running turn's progress lives only in process memory (the agent flushes to
SQLite at turn end, not mid-turn), so an app/backend/machine death mid-turn
leaves no durable trace of the interrupted prompt. This sidecar is that
trace: a marker is written when a turn starts running and cleared when the
turn concludes — success, handled error, or interrupt all clear it, so only
a process death leaves one behind. ``session.resume`` reads the marker to
decide whether to auto-continue the interrupted turn (see
``_maybe_schedule_auto_continue`` in ``tui_gateway/server.py``).

Markers are stored per ``YOUTAB_AGENT_HOME`` (callers pass the session's home so
profile sessions keep their state in their own profile directory) and the
file is bounded: writes prune entries older than ``_MAX_AGE_SECS`` and cap
the total count, so an unlucky streak of crashes can't grow it unboundedly.

Marker writes for an already running turn remain best-effort. The initial
acceptance write can request strict failure reporting so prompt.submit never
acknowledges a first prompt that has no durable recovery record.
"""

from __future__ import annotations

import json
import errno
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MARKER_DIR = "desktop"
_MARKER_FILE = "interrupted_turns.json"
_MAX_AGE_SECS = 24 * 3600
_MAX_ENTRIES = 32
# Enough to re-submit any realistic prompt; guards the sidecar against a
# pathological multi-megabyte paste being journaled on every turn.
_MAX_PROMPT_CHARS = 64_000

_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8

_lock = threading.Lock()


def _marker_path(home: Path | str) -> Path:
    return Path(home) / _MARKER_DIR / _MARKER_FILE


def _load(path: Path, *, strict: bool = False) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        if strict:
            raise
        logger.debug("unreadable turn-marker file %s; starting fresh", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"invalid turn-marker file: {path}")
        return {}
    if strict and any(not isinstance(entry, dict) for entry in data.values()):
        raise ValueError(f"invalid turn-marker entries: {path}")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _prune(entries: dict[str, dict], now: float, max_age: float = _MAX_AGE_SECS) -> dict[str, dict]:
    fresh = {
        key: entry
        for key, entry in entries.items()
        if now - float(entry.get("started_at") or 0) <= max_age
    }
    return fresh


def _fsync_dir(directory: Path) -> None:
    """Persist a directory entry change (POSIX). Windows uses write-through."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # platforms/filesystems that cannot open a directory
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            raise
    finally:
        os.close(fd)


def _durable_replace(src: str, dst: Path) -> None:
    """Atomically replace ``dst`` with ``src`` and persist the rename itself.

    Flushing the file is not enough: without persisting the directory entry a
    power loss can leave the old file (or none) in place. Windows:
    MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) returns only
    after the move is flushed to disk. POSIX: os.replace, then fsync the
    containing directory.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        move = ctypes.windll.kernel32.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(src), str(dst), _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH):
            raise ctypes.WinError()
        return
    os.replace(src, dst)
    _fsync_dir(dst.parent)


def _store(path: Path, entries: dict[str, dict]) -> None:
    if not entries:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".turn-marker-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f)
            # A recovery record must survive power loss, not only a process
            # exit: land the bytes before the rename publishes them.
            f.flush()
            os.fsync(f.fileno())
        _durable_replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_turn_start(
    home: Path | str,
    session_key: str,
    prompt: str,
    *,
    attempts: int = 0,
    pending: dict[str, Any] | None = None,
    strict: bool = False,
    reclaim_older_than: float | None = None,
    retain_seconds: float | None = None,
) -> None:
    """Persist the marker for a turn that is about to run.

    ``attempts`` counts how many auto-continues led to this run: 0 for a
    user-initiated turn, N for the Nth automatic re-run — the crash-loop
    breaker reads it back on the next resume.

    ``pending`` (``{"text", "images"}``) marks a prompt that was accepted but
    whose turn has not started: nothing ran, so recovery replays the original
    input instead of an "interrupted" note. Re-recording when the turn starts
    (without ``pending``) drops it.

    ``reclaim_older_than`` (seconds): when the journal is full, markers older
    than this can no longer be recovered (session.resume discards them), so
    they are dropped to make room. Fresh markers are never evicted; a full
    journal of fresh markers refuses the new one.

    ``retain_seconds``: the configured recovery window. Routine pruning never
    drops a marker younger than it (nor younger than 24 hours), so a write for
    one session cannot delete another session's still-recoverable prompt.
    """
    if not session_key or not prompt:
        if strict:
            raise ValueError("a durable turn marker requires a session and prompt")
        return
    if strict and (
        len(prompt) > _MAX_PROMPT_CHARS
        or (isinstance(pending, dict) and len(str(pending.get("text") or "")) > _MAX_PROMPT_CHARS)
    ):
        raise ValueError("prompt exceeds durable turn-marker capacity")
    now = time.time()
    entry: dict[str, Any] = {
        "attempts": max(0, int(attempts)),
        "prompt": prompt[:_MAX_PROMPT_CHARS],
        "started_at": now,
    }
    if isinstance(pending, dict) and isinstance(pending.get("text"), str):
        entry["pending"] = {
            "text": pending["text"][:_MAX_PROMPT_CHARS],
            "images": [str(i) for i in (pending.get("images") or []) if i],
        }
    try:
        with _lock:
            path = _marker_path(home)
            max_age = max(_MAX_AGE_SECS, float(retain_seconds or 0))
            entries = _prune(_load(path, strict=strict), now, max_age)
            if session_key not in entries and len(entries) >= _MAX_ENTRIES and reclaim_older_than is not None:
                entries = {
                    key: value
                    for key, value in entries.items()
                    if now - float(value.get("started_at") or 0) <= reclaim_older_than
                }
            if session_key not in entries and len(entries) >= _MAX_ENTRIES:
                raise RuntimeError("durable turn-marker capacity reached")
            entries[session_key] = entry
            _store(path, entries)
    except Exception:
        if strict:
            raise
        logger.debug("failed to record turn marker for %s", session_key, exc_info=True)


def clear_turn_marker(home: Path | str, session_key: str) -> None:
    """Remove the marker once its turn concluded (any outcome the client saw)."""
    if not session_key:
        return
    try:
        with _lock:
            path = _marker_path(home)
            entries = _load(path)
            if session_key not in entries:
                return
            del entries[session_key]
            _store(path, entries)
    except Exception:
        logger.debug("failed to clear turn marker for %s", session_key, exc_info=True)


def pending_turn_keys(home: Path | str, *, max_age_s: float | None = None) -> list[str]:
    """Session keys that still hold an unconcluded turn marker.

    ``max_age_s`` drops markers older than the auto-continue freshness window,
    which session.resume would discard instead of recovering.
    """
    try:
        with _lock:
            entries = _load(_marker_path(home))
    except Exception:
        return []
    now = time.time()
    keys: list[str] = []
    for key, entry in entries.items():
        if not key or not isinstance(entry, dict):
            continue
        if not str(entry.get("prompt") or "").strip():
            continue
        try:
            started_at = float(entry.get("started_at") or 0)
        except (TypeError, ValueError):
            continue
        if max_age_s is not None and now - started_at > max_age_s:
            continue
        keys.append(key)
    return keys


def read_turn_marker(home: Path | str, session_key: str) -> dict[str, Any] | None:
    """The marker left by a turn that never concluded, or None."""
    if not session_key:
        return None
    try:
        with _lock:
            entry = _load(_marker_path(home)).get(session_key)
    except Exception:
        return None
    if not isinstance(entry, dict):
        return None
    prompt = str(entry.get("prompt") or "")
    if not prompt.strip():
        return None
    try:
        started_at = float(entry.get("started_at") or 0)
        attempts = max(0, int(entry.get("attempts") or 0))
    except (TypeError, ValueError):
        return None
    marker: dict[str, Any] = {"attempts": attempts, "prompt": prompt, "started_at": started_at}
    pending = entry.get("pending")
    if isinstance(pending, dict) and isinstance(pending.get("text"), str):
        images = pending.get("images")
        marker["pending"] = {
            "text": pending["text"],
            "images": [str(i) for i in images] if isinstance(images, list) else [],
        }
    return marker
