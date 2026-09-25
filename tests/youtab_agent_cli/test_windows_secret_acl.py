"""Native-Windows owner-only DACL tests for secret files (WAVE-26 #7b).

Covers :mod:`youtab_agent_cli.windows_acl`:

* effective access control — only the current user + SYSTEM appear in the DACL
  (Everyone / Users / Authenticated Users / Administrators cannot read);
* ``SE_DACL_PROTECTED`` — the file's DACL is protected so the broad inheritable
  ACEs from ``%USERPROFILE%`` / ``%LOCALAPPDATA%`` are NOT merged in;
* no-plaintext-leak — the secret is only present on disk once the file is
  already locked down; and
* a PAIRED POSIX test proving the ``0o600`` behaviour and the platform contract
  (the DACL helpers are no-ops-by-refusal on POSIX) are preserved.

Native bits are guarded by a *capability* check (native Windows AND pywin32
importable), never a blanket ``skipif(win32)``. The POSIX behaviour is proven by
the paired ``requires_posix_permissions`` test below.
"""

from __future__ import annotations

import importlib.util
import os
import stat

import pytest

from tests._wincompat import requires_posix_permissions

from youtab_agent_cli import windows_acl


_PYWIN32_MODULES = ("win32security", "win32api", "win32con", "ntsecuritycon")
_HAVE_WINDOWS_ACL = os.name == "nt" and all(
    importlib.util.find_spec(m) is not None for m in _PYWIN32_MODULES
)

# Capability guard: native Windows + pywin32. This is NOT a blanket skipif(win32)
# — it names the exact capability (pywin32 DACL APIs) and is paired with the
# POSIX 0o600 test at the bottom of this module.
windows_acl_native = pytest.mark.skipif(
    not _HAVE_WINDOWS_ACL,
    reason="native Windows + pywin32 required to apply/verify owner-only DACLs; "
    "the POSIX 0o600 contract is covered by the paired requires_posix_permissions test",
)

SECRET = b"super-secret-runtime-value-\x00-with-nul-and-unicode-\xe2\x9c\x93"


def _allow_ace_sid_strings(path: str) -> set[str]:
    """Enumerate the string SIDs of every non-zero allow-ACE on ``path``'s DACL."""
    import win32security

    info = win32security.DACL_SECURITY_INFORMATION
    descriptor = win32security.GetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT, info
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None, "a null DACL grants everyone access"
    allow_types = {
        win32security.ACCESS_ALLOWED_ACE_TYPE,
        win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
    }
    sids: set[str] = set()
    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)
        ace_type = ace[0][0]
        mask = ace[1]
        sid = ace[-1]
        if ace_type in allow_types and mask:
            sids.add(win32security.ConvertSidToStringSid(sid))
    return sids


def _expected_allowed_sids() -> set[str]:
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    me = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    system = win32security.ConvertStringSidToSid("S-1-5-18")
    return {
        win32security.ConvertSidToStringSid(me),
        win32security.ConvertSidToStringSid(system),
    }


def _is_protected(path: str) -> bool:
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    control = descriptor.GetSecurityDescriptorControl()[0]
    return bool(control & win32security.SE_DACL_PROTECTED)


# --------------------------------------------------------------------------
# native Windows: effective access + protection
# --------------------------------------------------------------------------


@windows_acl_native
def test_secure_write_grants_only_current_user_and_system(tmp_path):
    path = tmp_path / "secret.bin"
    windows_acl.secure_write_secret_file(path, SECRET)

    assert path.read_bytes() == SECRET
    # Only the current user + SYSTEM may access the file.
    assert _allow_ace_sid_strings(str(path)) <= _expected_allowed_sids()
    # And the module's own verifier agrees.
    assert windows_acl.verify_owner_only_dacl(path) is True


@windows_acl_native
def test_secure_write_marks_dacl_protected(tmp_path):
    path = tmp_path / "secret.bin"
    windows_acl.secure_write_secret_file(path, SECRET)
    # SE_DACL_PROTECTED must be set so parent (%LOCALAPPDATA%) inheritable ACEs —
    # which admit Administrators / broader groups — are not merged in.
    assert _is_protected(str(path)) is True


@windows_acl_native
def test_apply_strips_inherited_broad_aces_from_plain_file(tmp_path):
    # A file created the ordinary way inherits the parent's broad ACEs and is NOT
    # protected, so the verifier must reject it...
    plain = tmp_path / "plain.bin"
    fd = os.open(str(plain), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b"x")
    os.close(fd)
    assert windows_acl.verify_owner_only_dacl(plain) is False

    # ...and applying the owner-only protected DACL brings it into compliance.
    windows_acl.apply_owner_only_dacl(plain)
    assert windows_acl.verify_owner_only_dacl(plain) is True
    assert _allow_ace_sid_strings(str(plain)) <= _expected_allowed_sids()
    assert _is_protected(str(plain)) is True


@windows_acl_native
def test_no_plaintext_written_under_permissive_dacl(tmp_path, monkeypatch):
    # Prove the secret bytes are only written AFTER the DACL is applied+verified:
    # wrap apply so that, at the moment protection is applied, the file on disk is
    # still empty (no secret yet).
    real_apply = windows_acl.apply_owner_only_dacl
    observed_sizes = []

    def _spy_apply(p):
        observed_sizes.append(os.path.getsize(os.fspath(p)))
        return real_apply(p)

    monkeypatch.setattr(windows_acl, "apply_owner_only_dacl", _spy_apply)
    path = tmp_path / "secret.bin"
    windows_acl.secure_write_secret_file(path, SECRET)

    assert observed_sizes == [0], "secret bytes must not exist before the DACL is applied"
    assert path.read_bytes() == SECRET


@windows_acl_native
def test_verify_rejects_null_and_broad_dacl(tmp_path):
    # Sanity: verify_owner_only_dacl is a real check, not a constant True.
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"hello")
    assert windows_acl.verify_owner_only_dacl(plain) is False


# --------------------------------------------------------------------------
# paired POSIX test: the 0o600 contract and platform refusals are preserved
# --------------------------------------------------------------------------


@requires_posix_permissions
def test_posix_secure_write_is_owner_only_0600(tmp_path):
    path = tmp_path / "secret.bin"
    windows_acl.secure_write_secret_file(path, SECRET)
    assert path.read_bytes() == SECRET
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    # No group/other access bits at all.
    assert not (mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH))


@requires_posix_permissions
def test_dacl_helpers_refuse_on_posix(tmp_path):
    path = tmp_path / "secret.bin"
    path.write_bytes(b"x")
    # The DACL concept does not exist on POSIX; the helpers must refuse loudly so
    # a mis-guarded caller fails fast rather than silently no-op'ing security.
    with pytest.raises(NotImplementedError):
        windows_acl.apply_owner_only_dacl(path)
    with pytest.raises(NotImplementedError):
        windows_acl.verify_owner_only_dacl(path)
    assert windows_acl.is_windows() is False
    assert windows_acl.pywin32_available() is False
