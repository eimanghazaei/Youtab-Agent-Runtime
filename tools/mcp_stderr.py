"""Real subprocess stderr descriptor with mandatory redaction before disk."""

from __future__ import annotations

import os
import logging
from pathlib import Path
import re
import select
import stat
import threading
import time
from typing import TextIO

from agent.redact import redact_sensitive_text

MAX_STDERR_LINE_BYTES = 64 * 1024
_READ_BYTES = 4096
_CLOSE_DRAIN_BYTES = 1024 * 1024
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END[A-Z ]*PRIVATE KEY-----")
logger = logging.getLogger(__name__)


def _open_log(path: Path) -> TextIO:
    """Reject special files/symlinks; restrict POSIX files to their owner."""
    if path.is_symlink():
        raise OSError("MCP stderr log must not be a symlink")
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("MCP stderr log must be a regular file")
        if os.name == "posix":
            if metadata.st_uid != os.getuid():
                raise OSError("MCP stderr log belongs to another user")
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8", errors="replace", buffering=1)
    except BaseException:
        os.close(fd)
        raise


class RedactedStderrLog:
    """One shared pipe/pump, with complete bounded lines as redaction units.

    Reuse across subprocess/reconnect lifetimes. Close after child shutdown;
    it drains queued data (at most 1 MiB/one second) then releases the pump.
    Incomplete oversized lines and private-key blocks are never persisted.
    """

    def __init__(self, path: Path):
        self._sink = _open_log(path)
        self._sink_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._stopping = threading.Event()
        self._pending = bytearray()
        self._dropping = False
        self._private_key = False
        self._discarding_output = False
        self.error: str | None = None
        self.closed = False
        try:
            self._read_fd, write_fd = os.pipe()
            self._writer = os.fdopen(write_fd, "wb", buffering=0)
            self._peek = self._windows_peek() if os.name == "nt" else None
            self._thread = threading.Thread(
                target=self._pump, name="mcp-stderr-redaction", daemon=True,
            )
            self._thread.start()
        except BaseException:
            if hasattr(self, "_writer"):
                self._writer.close()
            if hasattr(self, "_read_fd"):
                os.close(self._read_fd)
            self._sink.close()
            raise

    def _windows_peek(self):
        # Python 3.11 has no nonblocking anonymous-pipe API on Windows.
        # PeekNamedPipe lets the pump poll without an uninterruptible read.
        import ctypes
        from ctypes import wintypes
        import msvcrt

        peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        peek.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        peek.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(self._read_fd)

        def available() -> int:
            count = wintypes.DWORD()
            if not peek(handle, None, 0, None, ctypes.byref(count), None):
                if ctypes.get_last_error() == 109:  # ERROR_BROKEN_PIPE / EOF
                    return -1
                raise OSError("MCP stderr pipe polling failed")
            return count.value

        return available

    def fileno(self) -> int:
        return self._writer.fileno()

    def _capture_failed(self) -> None:
        self.error = "MCP stderr capture failed; output omitted"
        if not self._discarding_output:
            self._discarding_output = True
            logger.warning("MCP stderr capture failed; child diagnostics discarded")

    def write(self, text: str) -> int:
        """Headers follow the same mandatory redaction boundary."""
        if not self._discarding_output:
            try:
                safe = redact_sensitive_text(text, force=True, redact_url_credentials=True)
                with self._sink_lock:
                    self._sink.write(safe)
            except Exception:
                # Keep draining children if disk/redaction fails: block neither
                # startup nor JSON-RPC, and never persist raw output elsewhere.
                self._capture_failed()
        return len(text)

    def flush(self) -> None:
        try:
            with self._sink_lock:
                self._sink.flush()
        except Exception:
            self._capture_failed()

    def _read(self, wait: bool) -> bytes | None:
        if self._peek is not None:
            available = self._peek()
            if available < 0:
                return b""
            if not available:
                if wait:
                    self._stopping.wait(0.05)
                return None
            return os.read(self._read_fd, min(available, _READ_BYTES))
        if select.select([self._read_fd], [], [], 0.05 if wait else 0)[0]:
            return os.read(self._read_fd, _READ_BYTES)
        return None

    def _line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        if _PRIVATE_KEY_BEGIN.search(text):
            self._private_key = not bool(_PRIVATE_KEY_END.search(text))
            self.write("[REDACTED PRIVATE KEY]\n")
        elif self._private_key:
            if _PRIVATE_KEY_END.search(text):
                self._private_key = False
        else:
            self.write(text)

    def _consume(self, chunk: bytes) -> None:
        # A chunk is bounded, as is pending. Never flush a raw partial line:
        # token prefixes and their secret suffix may arrive in separate writes.
        for part in chunk.splitlines(keepends=True):
            ending = part.endswith(b"\n")
            if not self._dropping:
                if len(self._pending) + len(part) > MAX_STDERR_LINE_BYTES:
                    # Remember a PEM start in the retained prefix before dropping.
                    prefix = bytes(self._pending).decode("utf-8", errors="replace")
                    if _PRIVATE_KEY_BEGIN.search(prefix):
                        self._private_key = True
                    self._pending.clear()
                    self._dropping = True
                else:
                    self._pending.extend(part)
            if ending:
                if self._dropping:
                    self.write("[MCP stderr oversized line omitted]\n")
                else:
                    self._line(bytes(self._pending))
                self._pending.clear()
                self._dropping = False

    def _pump(self) -> None:
        try:
            while not self._stopping.is_set():
                chunk = self._read(wait=True)
                if chunk == b"":
                    break
                if chunk is not None:
                    self._consume(chunk)
            deadline = time.monotonic() + 1.0
            remaining = _CLOSE_DRAIN_BYTES
            while remaining > 0 and time.monotonic() < deadline:
                chunk = self._read(wait=False)
                if not chunk:
                    break
                self._consume(chunk)
                remaining -= len(chunk)
            if self._pending and not self._dropping:
                self._line(bytes(self._pending))
        except Exception:
            # No raw stderr or exception-text fallback on sink/redactor failure.
            self._capture_failed()
        finally:
            os.close(self._read_fd)
            with self._sink_lock:
                try:
                    self._sink.close()
                except Exception:
                    self._capture_failed()

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self._stopping.set()
            self._writer.close()
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError("MCP stderr pump did not stop")
            self.closed = True
