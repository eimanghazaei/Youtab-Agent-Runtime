"""Owner-only DACL helpers for secret files on native Windows (WAVE-26 #7b).

On POSIX a secret file is protected with mode ``0o600`` (owner read/write only).
Windows has no POSIX mode bits — ``os.chmod`` only toggles the read-only flag and
``stat().st_mode`` is synthesised — so the equivalent protection is a *DACL*
(Discretionary Access Control List) that grants ``FILE_ALL_ACCESS`` to ONLY the
current user and ``SYSTEM`` and is marked **protected** (``SE_DACL_PROTECTED``) so
the broad inheritable ACEs from ``%USERPROFILE%`` / ``%LOCALAPPDATA%`` (which by
default also admit ``Administrators``) are NOT merged in.

This module generalises the owner-only DACL helper that already exists, wired only
to the SSH runtime, in :mod:`youtab_agent_cli.windows_ssh_runtime`
(``_security_attributes`` / ``_verify_security``) into a reusable public API:

* :func:`apply_owner_only_dacl` — set an owner-only *protected* DACL on a path.
* :func:`verify_owner_only_dacl` — return ``True`` iff a path already has one.
* :func:`secure_write_secret_file` — create a secret file with **no readable
  exposure window** and **fail closed** (on any ACL apply/verify failure the
  partially-written file is removed, leaving no plaintext behind).

Platform contract:

* All three functions are native-Windows features. On POSIX
  :func:`apply_owner_only_dacl` and :func:`verify_owner_only_dacl` raise
  :class:`NotImplementedError` — a caller must not reach them there; the POSIX
  ``0o600``/``0o700`` behaviour is owned by the caller and is left unchanged.
* :func:`secure_write_secret_file` IS cross-platform: on POSIX it uses the same
  ``O_EXCL`` + ``0o600`` create the existing writers use; on Windows it uses the
  DACL path. Either way it fails closed and leaves no residue on failure.
* pywin32 is the native binding used. If it is unavailable on a Windows host the
  DACL cannot be applied or proven, so :func:`secure_write_secret_file` fails
  closed (refuses to write an unprotectable secret) and :func:`apply_owner_only_dacl`
  raises; :func:`verify_owner_only_dacl` raises so callers can decide (the
  read-side check guards with :func:`pywin32_available` to avoid a regression on
  hosts that never had this check).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Set, Tuple, Union

_PathLike = Union[str, "os.PathLike[str]"]

# NT SID of the local SYSTEM account (well-known, locale-independent).
_SYSTEM_SID_STRING = "S-1-5-18"


def is_windows() -> bool:
    """True on native Windows (``os.name == 'nt'``)."""
    return os.name == "nt"


def pywin32_available() -> bool:
    """True when the pywin32 modules needed for DACL work can be imported.

    Callers that must not regress on a Windows host lacking pywin32 (e.g. the
    secret-file read-side check) guard with this before calling
    :func:`verify_owner_only_dacl`.
    """
    if not is_windows():
        return False
    try:
        import ntsecuritycon  # noqa: F401
        import win32api  # noqa: F401
        import win32con  # noqa: F401
        import win32security  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure means "not usable"
        return False
    return True


def _win32() -> Tuple[Any, ...]:
    """Import the pywin32 modules, raising a clear error when unusable."""
    if not is_windows():
        raise NotImplementedError(
            "owner-only DACLs are a native-Windows feature; on POSIX use 0o600"
        )
    try:
        import ntsecuritycon
        import win32api
        import win32con
        import win32security
    except Exception as exc:  # noqa: BLE001
        raise OSError(
            "pywin32 is required to apply/verify owner-only DACLs on Windows"
        ) from exc
    return ntsecuritycon, win32api, win32con, win32security


def _current_sid(win32api: Any, win32con: Any, win32security: Any) -> Any:
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    return win32security.GetTokenInformation(token, win32security.TokenUser)[0]


def _system_sid(win32security: Any) -> Any:
    return win32security.ConvertStringSidToSid(_SYSTEM_SID_STRING)


def _allowed_sid_strings(win32api: Any, win32con: Any, win32security: Any) -> Set[str]:
    return {
        win32security.ConvertSidToStringSid(_current_sid(win32api, win32con, win32security)),
        win32security.ConvertSidToStringSid(_system_sid(win32security)),
    }


def _build_owner_only_security_descriptor() -> Any:
    """Build a SECURITY_DESCRIPTOR: owner = current user, DACL = {user, SYSTEM}
    FILE_ALL_ACCESS, DACL protected (inherited broad ACEs are not merged)."""
    ntsecuritycon, win32api, win32con, win32security = _win32()
    owner = _current_sid(win32api, win32con, win32security)
    system = _system_sid(win32security)
    acl = win32security.ACL()
    # No inheritance flags (0): each ACE grants exactly the named SID full
    # control over THIS object only.
    for sid in (owner, system):
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, 0, ntsecuritycon.FILE_ALL_ACCESS, sid
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(owner, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    # SE_DACL_PROTECTED stops the inheritable ACEs on %USERPROFILE% /
    # %LOCALAPPDATA% (which admit Administrators and sometimes broader groups)
    # from being merged into this object's effective DACL.
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
    )
    return descriptor


def build_owner_only_security_attributes() -> Any:
    """A ``SECURITY_ATTRIBUTES`` carrying the owner-only protected descriptor.

    Suitable to pass to ``win32file.CreateFile`` / ``CreateDirectory`` so the
    object is born with the correct DACL (no post-create exposure window). This is
    the generalised form of ``windows_ssh_runtime._security_attributes``.
    """
    _, _, _, win32security = _win32()
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = _build_owner_only_security_descriptor()
    return attributes


def apply_owner_only_dacl(path: _PathLike) -> None:
    """Set an owner-only, protected DACL on an existing ``path`` (Windows only).

    Grants ``FILE_ALL_ACCESS`` to only the current user + ``SYSTEM``, sets the
    owner to the current user, and marks the DACL protected so inherited broad
    ACEs are stripped. Raises :class:`NotImplementedError` on POSIX and
    :class:`OSError` when pywin32 is unavailable or the call fails.
    """
    ntsecuritycon, win32api, win32con, win32security = _win32()
    owner = _current_sid(win32api, win32con, win32security)
    system = _system_sid(win32security)
    acl = win32security.ACL()
    for sid in (owner, system):
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, 0, ntsecuritycon.FILE_ALL_ACCESS, sid
        )
    info = (
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION
    )
    try:
        win32security.SetNamedSecurityInfo(
            os.fspath(path),
            win32security.SE_FILE_OBJECT,
            info,
            owner,
            None,
            acl,
            None,
        )
    except Exception as exc:  # noqa: BLE001 — normalise pywintypes.error to OSError
        raise OSError(f"failed to apply owner-only DACL to {os.fspath(path)!r}: {exc}") from exc


def secure_directory_owner_only(path: _PathLike) -> None:
    """Create ``path`` (if absent) and give it an owner-only *inheritable* DACL.

    Grants ``FILE_ALL_ACCESS`` to only the current user + ``SYSTEM``, marks the
    DACL protected (so broad inherited ACEs from ``%LOCALAPPDATA%`` are stripped),
    and tags each ACE ``CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE`` so **every
    file created inside is born owner-only by inheritance** — closing the
    permissive-creation window for files (e.g. a SQLite DB and its ``-wal`` /
    ``-shm`` sidecars) that are opened later inside the directory.

    Native Windows only. Raises :class:`NotImplementedError` on POSIX (the caller
    owns ``0o700`` there) and :class:`OSError` when pywin32 is unavailable or the
    call fails, so a caller can decide whether to fail closed.
    """
    ntsecuritycon, win32api, win32con, win32security = _win32()
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    owner = _current_sid(win32api, win32con, win32security)
    system = _system_sid(win32security)
    inherit = (
        win32security.CONTAINER_INHERIT_ACE | win32security.OBJECT_INHERIT_ACE
    )
    acl = win32security.ACL()
    for sid in (owner, system):
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, inherit, ntsecuritycon.FILE_ALL_ACCESS, sid
        )
    info = (
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION
    )
    try:
        win32security.SetNamedSecurityInfo(
            os.fspath(target),
            win32security.SE_FILE_OBJECT,
            info,
            owner,
            None,
            acl,
            None,
        )
    except Exception as exc:  # noqa: BLE001 — normalise pywintypes.error to OSError
        raise OSError(
            f"failed to apply owner-only inheritable DACL to {os.fspath(target)!r}: {exc}"
        ) from exc


def verify_owner_only_dacl(path: _PathLike) -> bool:
    """Return ``True`` iff ``path`` has an owner-only, protected DACL (Windows only).

    Proves: owner is the current user or SYSTEM; the DACL is present (not null);
    it is marked ``SE_DACL_PROTECTED`` (no inherited broad ACEs); and every
    allow-ACE with a non-zero access mask names only the current user or SYSTEM
    (so Everyone / Users / Authenticated Users / Administrators cannot read).

    Raises :class:`NotImplementedError` on POSIX and :class:`OSError` when
    pywin32 is unavailable (a Windows caller that must not regress guards with
    :func:`pywin32_available`).
    """
    _, win32api, win32con, win32security = _win32()
    allowed = _allowed_sid_strings(win32api, win32con, win32security)
    info = (
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
    )
    try:
        descriptor = win32security.GetNamedSecurityInfo(
            os.fspath(path), win32security.SE_FILE_OBJECT, info
        )
    except Exception as exc:  # noqa: BLE001
        raise OSError(f"cannot read security info for {os.fspath(path)!r}: {exc}") from exc

    owner = descriptor.GetSecurityDescriptorOwner()
    if owner is None or win32security.ConvertSidToStringSid(owner) not in allowed:
        return False

    control = descriptor.GetSecurityDescriptorControl()[0]
    if not (control & win32security.SE_DACL_PROTECTED):
        # Inheritable ACEs from the parent could be merged -> not owner-only.
        return False

    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        # A null DACL grants everyone full access.
        return False

    allow_types = {
        win32security.ACCESS_ALLOWED_ACE_TYPE,
        win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
        getattr(win32security, "ACCESS_ALLOWED_CALLBACK_ACE_TYPE", 9),
        getattr(win32security, "ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE", 11),
    }
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        ace_type = ace[0][0]
        mask = ace[1]
        sid = ace[-1]
        if ace_type in allow_types and mask:
            if win32security.ConvertSidToStringSid(sid) not in allowed:
                return False
    return True


def secure_write_secret_file(
    path: _PathLike,
    data: Union[bytes, str],
    *,
    overwrite: bool = False,
    posix_mode: int = 0o600,
) -> Path:
    """Write ``data`` to ``path`` as a secret file with no readable exposure window.

    Cross-platform and fail-closed:

    * POSIX: create with ``O_EXCL`` (or truncate when ``overwrite``) at
      ``posix_mode`` (default ``0o600``) — the existing writer contract, unchanged.
    * Windows: create the file **empty** with ``O_EXCL``, apply and *verify* the
      owner-only protected DACL while it is still empty, and only THEN write the
      secret bytes — so the secret is never present on disk under a permissive
      DACL. If the DACL cannot be applied/verified (including because pywin32 is
      unavailable), the file is removed and an error is raised: no plaintext is
      left behind.

    On any failure a file this call created is unlinked before the exception
    propagates. Returns the resolved :class:`~pathlib.Path` on success.
    """
    target = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    if not is_windows():
        return _secure_write_posix(target, payload, overwrite=overwrite, mode=posix_mode)

    if not pywin32_available():
        # Fail closed: we cannot protect the secret, so we must not write it.
        raise OSError(
            "cannot securely write secret on Windows: pywin32 is unavailable "
            "to apply an owner-only DACL"
        )

    if overwrite and target.exists():
        target.unlink()

    created = False
    fd = None
    try:
        # 1) Create the file EMPTY (no secret bytes yet). O_EXCL ensures we are
        #    the creator and there is no pre-existing handle/content.
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.close(fd)
        fd = None
        # 2) Lock the empty file down and PROVE it before any secret is written.
        apply_owner_only_dacl(target)
        if not verify_owner_only_dacl(target):
            raise OSError("owner-only DACL verification failed after apply")
        # 3) Now the secret bytes land into an already-protected file.
        fd = os.open(str(target), os.O_WRONLY)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return target
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created:
            try:
                target.unlink()
            except OSError:
                pass
        raise


def _secure_write_posix(target: Path, payload: bytes, *, overwrite: bool, mode: int) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    created = False
    fd = None
    try:
        fd = os.open(str(target), flags, mode)
        created = True
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # umask can only clear bits from ``mode``; re-assert owner-only.
        try:
            os.chmod(target, mode)
        except OSError:
            pass
        return target
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # Only remove residue we created here (O_EXCL guarantees that for the
        # non-overwrite path; for overwrite we truncated an existing file, so we
        # leave it rather than deleting a pre-existing secret).
        if created and not overwrite:
            try:
                target.unlink()
            except OSError:
                pass
        raise
