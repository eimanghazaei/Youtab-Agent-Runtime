"""Windows Job Object owned-process-tree supervisor (ctypes, no pywin32).

Scope (honest): a Job Object supervises PROCESSES, not threads. The delegate
child runs as a THREAD in the parent process (DaemonThreadPoolExecutor), so a Job
Object does NOT and cannot force-terminate it — forced termination of in-process
delegate work stays OPEN (a Python thread cannot be force-killed). This supervisor
is for the SUBPROCESS surfaces (e.g. MCP servers, any owned child+grandchild
tree): it terminates the whole owned tree atomically on cancel / execution
deadline, leaves unrelated processes untouched, and reaps children when the job
handle closes (KILL_ON_JOB_CLOSE) so a parent death cannot orphan the tree.

A killed Job-owned tree is TERMINATION, not resumability.
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _CREATE_SUSPENDED = 0x00000004

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
        ]


class JobSupervisorUnavailable(RuntimeError):
    pass


class WindowsJobSupervisor:
    """Owns a Job Object whose processes (and their descendants) are terminated as
    one tree. Non-Windows callers get JobSupervisorUnavailable (use POSIX
    killpg/process-group cleanup there instead)."""

    def __init__(self):
        if not _IS_WINDOWS:
            raise JobSupervisorUnavailable("WindowsJobSupervisor requires Windows (os.name=='nt')")
        self._job = _kernel32.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            self._job, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(self._job)
            self._job = None
            raise ctypes.WinError(err)
        self._pids: List[int] = []
        self._closed = False

    def assign(self, proc: "subprocess.Popen") -> None:
        """Assign a Popen process to the job; descendants it spawns AFTER this
        call join the same job automatically."""
        if self._closed:
            raise JobSupervisorUnavailable("supervisor already closed")
        handle = int(proc._handle)  # type: ignore[attr-defined]
        if not _kernel32.AssignProcessToJobObject(self._job, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self._pids.append(proc.pid)

    def spawn(self, args: List[str]) -> int:
        """Launch a process CREATE_SUSPENDED, assign it to the job, then resume —
        so it (and every descendant it later spawns) is in the job from the very
        first instruction. Returns the pid. Reliable owned-tree membership."""
        if self._closed:
            raise JobSupervisorUnavailable("supervisor already closed")
        cmd_buf = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(_STARTUPINFOW)
        pi = _PROCESS_INFORMATION()
        ok = _kernel32.CreateProcessW(
            None, cmd_buf, None, None, False, _CREATE_SUSPENDED, None, None,
            ctypes.byref(si), ctypes.byref(pi),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _kernel32.AssignProcessToJobObject(self._job, pi.hProcess):
                raise ctypes.WinError(ctypes.get_last_error())
            if _kernel32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            self._pids.append(int(pi.dwProcessId))
            return int(pi.dwProcessId)
        finally:
            _kernel32.CloseHandle(pi.hThread)
            _kernel32.CloseHandle(pi.hProcess)

    def terminate(self, exit_code: int = 1) -> None:
        """Terminate the WHOLE owned tree atomically (only this job's processes;
        unrelated processes are untouched)."""
        if self._closed or not self._job:
            return
        _kernel32.TerminateJobObject(self._job, exit_code)

    def close(self) -> None:
        """Close the job handle. With KILL_ON_JOB_CLOSE, any still-running owned
        processes are terminated — a parent death cannot orphan the tree."""
        if self._closed:
            return
        self._closed = True
        if self._job:
            _kernel32.CloseHandle(self._job)
            self._job = None

    @property
    def pids(self) -> List[int]:
        return list(self._pids)

    def __enter__(self) -> "WindowsJobSupervisor":
        return self

    def __exit__(self, *exc) -> None:
        self.terminate()
        self.close()


def supervisor_available() -> bool:
    return _IS_WINDOWS
