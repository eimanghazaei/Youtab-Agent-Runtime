"""Shared profile auth paths and locks; no login or model selection dependencies."""
from __future__ import annotations
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict
from youtab_constants import get_youtab_home

logger = logging.getLogger(__name__)
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
_auth_target_lock_holders: Dict[str, threading.local] = {}
_auth_target_lock_holders_guard = threading.Lock()

def _auth_file_path() -> Path:
    path = get_youtab_home() / "auth.json"
    # Seat belt: if pytest is running and YOUTAB_AGENT_HOME resolves to the real
    # user's auth store, refuse rather than silently corrupt it. This catches
    # tests that forgot to monkeypatch YOUTAB_AGENT_HOME, tests invoked without the
    # hermetic conftest, or sandbox escapes via threads/subprocesses. In
    # production (no PYTEST_CURRENT_TEST) this is a single dict lookup.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real_home_auth = (Path.home() / ".youtab-agent-runtime" / "auth.json").resolve(strict=False)
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = path
        if resolved == real_home_auth:
            raise RuntimeError(
                f"Refusing to touch real user auth store during test run: {path}. "
                "Set YOUTAB_AGENT_HOME to a tmp_path in your test fixture, or run "
                "via scripts/run_tests.sh for hermetic CI-parity env."
            )
    return path


def _auth_lock_holder_for(target_path: Path) -> threading.local:
    """Return a reentrancy tracker keyed to one canonical auth-store path."""
    try:
        key = str(target_path.resolve(strict=False))
    except Exception:
        key = str(target_path)
    with _auth_target_lock_holders_guard:
        return _auth_target_lock_holders.setdefault(key, threading.local())


@contextmanager
def _file_lock(
    lock_path: Path,
    holder: threading.local,
    timeout_seconds: float,
    timeout_message: str,
):
    """Cross-process advisory flock helper.

    Reentrant per-thread via ``holder.depth``. Falls back to a depth-only
    guard when neither ``fcntl`` nor ``msvcrt`` is available (rare).
    Callers supply their own ``threading.local`` so independent locks
    (e.g. profile auth.json vs shared Youtab store) don't share reentrancy
    state — that would let one lock's reentrant acquisition silently skip
    the other's kernel-level flock.
    """
    if getattr(holder, "depth", 0) > 0:
        holder.depth += 1
        try:
            yield
        finally:
            holder.depth -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
        return

    # On Windows, msvcrt.locking needs the file to have content and the
    # file pointer at position 0. Ensure the lock file has at least 1 byte.
    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    with lock_path.open("r+" if msvcrt else "a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while True:
            try:
                if fcntl:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(timeout_message)
                time.sleep(0.05)

        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
            if fcntl:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except (OSError, IOError):
                    logger.debug("Best-effort POSIX auth lock release failed")
            elif msvcrt:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    logger.debug("Best-effort Windows auth lock release failed")


def read_auth_document(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        corrupt_path = path.with_suffix(".json.corrupt")
        try:
            shutil.copy2(path, corrupt_path)
        except OSError:
            logger.debug("Could not preserve corrupt auth document", exc_info=True)
        logger.warning("auth: failed to parse %s (%s); corrupt file preserved at %s", path, exc, corrupt_path)
        return {}
    return raw if isinstance(raw, dict) else {}
