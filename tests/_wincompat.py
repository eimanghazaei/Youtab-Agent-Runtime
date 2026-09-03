"""Shared cross-platform test-capability guards (WAVE-23).

Every skip in the runtime/CLI test suite that is genuinely unsupported on a
platform is expressed here as a *capability* check — never a blanket
``skipif(sys.platform == "win32")``. Each marker names the specific missing
capability so the skip is narrow and auditable, and every capability that backs
a production feature has a paired Windows *fail-closed* test proving the
production code degrades safely rather than crashing.

The capabilities:

* ``requires_symlink`` — the OS/session can actually create a symbolic link.
  On Windows this needs SeCreateSymbolicLinkPrivilege (Developer Mode or an
  elevated/again-privileged token); an unprivileged session raises
  ``OSError`` (WinError 1314). This is a *session privilege* probe, not a
  platform check — a privileged Windows session runs these tests.
* ``requires_os_attr(name)`` — ``os`` exposes a POSIX-only syscall
  (``chown``/``mkfifo``/``geteuid``/``fork``/``getuid``…). Absent on native
  Windows.
* ``requires_module(name)`` — a POSIX-only stdlib module imports
  (``fcntl``/``termios``/``pty``/``curses``/``pwd``/``grp``…).
* ``requires_posix_permissions`` — POSIX mode bits (``0o600`` etc.) are
  enforced by the filesystem. On Windows ``os.chmod`` only toggles the
  read-only bit and ``stat().st_mode`` is synthesised, so mode-bit assertions
  are meaningless; access there is governed by ACLs.
* ``requires_proc`` — ``/proc`` is present (Linux process introspection).
* ``requires_posix`` — a POSIX host in general. Reserved for integration paths
  that are irreducibly POSIX (a shell pipeline, an ``execvp`` handoff) AND that
  have a separate Windows fail-closed test; do NOT reach for it to paper over a
  fixable path/encoding assumption.
"""
from __future__ import annotations

import os
import sys

import pytest

WINDOWS = os.name == "nt"


def _can_symlink() -> bool:
    """True when this process can actually create a symlink right now.

    Probes once by creating a link in a throwaway temp dir; caches the result.
    """
    global _SYMLINK_OK
    try:
        return _SYMLINK_OK  # type: ignore[name-defined]
    except NameError:
        pass
    ok = False
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t")
            link = os.path.join(d, "l")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.symlink(target, link)
            ok = os.path.islink(link)
    except (OSError, NotImplementedError, AttributeError):
        ok = False
    _SYMLINK_OK = ok
    return ok


requires_symlink = pytest.mark.skipif(
    not _can_symlink(),
    reason="session cannot create symlinks (needs SeCreateSymbolicLinkPrivilege / "
    "Developer Mode on Windows); the symlink-safety behaviour is proven by the "
    "paired Windows fail-closed test",
)

requires_posix_permissions = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits (0o600/0o644…) are only filesystem-enforced on POSIX; "
    "Windows synthesises st_mode and governs access via ACLs",
)

requires_proc = pytest.mark.skipif(
    not os.path.isdir("/proc"),
    reason="/proc process introspection is Linux-only",
)

requires_posix = pytest.mark.skipif(
    WINDOWS,
    reason="irreducibly POSIX integration path (shell/exec/FIFO); Windows behaviour "
    "is covered by a paired fail-closed test",
)


def requires_os_attr(name: str):
    """Skip when ``os`` lacks POSIX-only syscall ``name`` (e.g. ``chown``)."""
    return pytest.mark.skipif(
        not hasattr(os, name),
        reason=f"os.{name} is POSIX-only and absent on this platform; production "
        f"degrades via a paired Windows fail-closed test",
    )


def requires_module(name: str):
    """Skip when POSIX-only stdlib module ``name`` cannot be imported."""
    import importlib.util

    return pytest.mark.skipif(
        importlib.util.find_spec(name) is None,
        reason=f"the {name!r} stdlib module is POSIX-only and unavailable here",
    )


def _has_working_bash() -> bool:
    """True when ``bash`` on PATH is a functioning POSIX shell.

    A plain ``bash`` probe is not enough on Windows: on GitHub ``windows-latest``
    the ``bash`` first on PATH is the WSL launcher stub in System32, which — with
    no distro installed — prints a "Windows Subsystem for Linux has no installed
    distributions" notice (in UTF-16) and exits non-zero rather than running the
    script. Probe by actually executing a command and checking the output, so the
    guard reflects a real bash, not merely the presence of a ``bash`` name.
    """
    global _BASH_OK
    try:
        return _BASH_OK  # type: ignore[name-defined]
    except NameError:
        pass
    ok = False
    try:
        import subprocess

        r = subprocess.run(
            ["bash", "-c", "echo YT_BASH_OK"],
            capture_output=True, timeout=15,
        )
        ok = r.returncode == 0 and b"YT_BASH_OK" in r.stdout
    except (OSError, subprocess.SubprocessError):
        ok = False
    _BASH_OK = ok
    return ok


requires_working_bash = pytest.mark.skipif(
    not _has_working_bash(),
    reason="no functioning POSIX bash on PATH (e.g. windows-latest's `bash` is the "
    "WSL launcher stub with no distro); the generated-script syntax is validated on "
    "the Linux/macOS CI jobs where a real bash runs",
)
