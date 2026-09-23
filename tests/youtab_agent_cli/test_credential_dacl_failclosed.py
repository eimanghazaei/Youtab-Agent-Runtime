"""Fail-closed guarantees for the secure secret-file writer (WAVE-26 #7b).

:func:`youtab_agent_cli.windows_acl.secure_write_secret_file` must never leave a
plaintext secret on disk when it cannot prove the file is protected. These tests
drive the Windows fail-closed control flow **portably** by forcing the Windows
branch (via monkeypatch) so the residue-cleanup logic is exercised on every CI
platform, plus a native-Windows check where pywin32 is present and a POSIX
behaviour check.

Failure modes covered:

* DACL *apply* raises            -> file removed, error propagates.
* DACL *verify* returns ``False`` -> file removed, error propagates.
* pywin32 unavailable on Windows  -> refuses before creating the file (no residue).
* POSIX ``O_EXCL`` on an existing file -> refuses, and the PRE-EXISTING secret is
  left intact (we never delete a file we did not create).
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from youtab_agent_cli import windows_acl


def _force_windows_branch(monkeypatch):
    """Make secure_write_secret_file take the Windows path on any host."""
    monkeypatch.setattr(windows_acl, "is_windows", lambda: True)
    monkeypatch.setattr(windows_acl, "pywin32_available", lambda: True)


# --------------------------------------------------------------------------
# portable: the fail-closed control flow leaves no residue
# --------------------------------------------------------------------------


def test_failclosed_when_apply_raises(tmp_path, monkeypatch):
    _force_windows_branch(monkeypatch)

    def _boom(_path):
        raise OSError("simulated SetNamedSecurityInfo failure")

    monkeypatch.setattr(windows_acl, "apply_owner_only_dacl", _boom)

    path = tmp_path / "cred.json"
    with pytest.raises(OSError):
        windows_acl.secure_write_secret_file(path, b"top-secret-token")

    assert not path.exists(), "no plaintext must be left behind on ACL apply failure"


def test_failclosed_when_verify_fails(tmp_path, monkeypatch):
    _force_windows_branch(monkeypatch)
    # apply "succeeds" but verification reports the DACL is not owner-only.
    monkeypatch.setattr(windows_acl, "apply_owner_only_dacl", lambda _p: None)
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)

    path = tmp_path / "cred.json"
    with pytest.raises(OSError):
        windows_acl.secure_write_secret_file(path, b"top-secret-token")

    assert not path.exists(), "no plaintext must be left behind on verify failure"


def test_failclosed_when_pywin32_unavailable_on_windows(tmp_path, monkeypatch):
    # Force the Windows branch but declare pywin32 unusable: we cannot protect the
    # secret, so we must refuse WITHOUT ever creating the file.
    monkeypatch.setattr(windows_acl, "is_windows", lambda: True)
    monkeypatch.setattr(windows_acl, "pywin32_available", lambda: False)

    path = tmp_path / "cred.json"
    with pytest.raises(OSError):
        windows_acl.secure_write_secret_file(path, b"top-secret-token")

    assert not path.exists(), "no file may be created when the secret cannot be protected"


def test_no_secret_bytes_recoverable_after_failclosed(tmp_path, monkeypatch):
    # Belt-and-suspenders: after a fail-closed write, the secret must not be
    # discoverable anywhere under the target directory (no stray temp file).
    _force_windows_branch(monkeypatch)
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)
    monkeypatch.setattr(windows_acl, "apply_owner_only_dacl", lambda _p: None)

    secret = b"do-not-persist-this-value-9f83a"
    path = tmp_path / "cred.json"
    with pytest.raises(OSError):
        windows_acl.secure_write_secret_file(path, secret)

    for root, _dirs, files in os.walk(tmp_path):
        for name in files:
            data = open(os.path.join(root, name), "rb").read()
            assert secret not in data, f"secret leaked into {name}"
    assert not path.exists()


# --------------------------------------------------------------------------
# native Windows: a real verify failure removes the real file
# --------------------------------------------------------------------------

_PYWIN32_MODULES = ("win32security", "win32api", "win32con", "ntsecuritycon")
_HAVE_WINDOWS_ACL = os.name == "nt" and all(
    importlib.util.find_spec(m) is not None for m in _PYWIN32_MODULES
)

windows_acl_native = pytest.mark.skipif(
    not _HAVE_WINDOWS_ACL,
    reason="native Windows + pywin32 required; POSIX residue behaviour is covered "
    "by the paired POSIX test",
)


@windows_acl_native
def test_native_failclosed_on_verify_false(tmp_path, monkeypatch):
    # Real apply runs, but we force the post-apply verification to fail; the file
    # created on the real filesystem must be removed.
    monkeypatch.setattr(windows_acl, "verify_owner_only_dacl", lambda _p: False)
    path = tmp_path / "cred.json"
    with pytest.raises(OSError):
        windows_acl.secure_write_secret_file(path, b"real-secret")
    assert not path.exists()


# --------------------------------------------------------------------------
# POSIX: O_EXCL refuses to clobber, and never deletes a file it did not create
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX O_EXCL create semantics")
def test_posix_oexcl_preserves_existing_secret(tmp_path):
    path = tmp_path / "cred.json"
    windows_acl.secure_write_secret_file(path, b"first-value")
    assert path.read_bytes() == b"first-value"

    # A non-overwrite write must refuse rather than clobber, and must NOT delete
    # the pre-existing secret it did not create.
    with pytest.raises(FileExistsError):
        windows_acl.secure_write_secret_file(path, b"second-value")
    assert path.read_bytes() == b"first-value"

    # Explicit overwrite is honoured.
    windows_acl.secure_write_secret_file(path, b"third-value", overwrite=True)
    assert path.read_bytes() == b"third-value"
