"""Grant-bound filesystem effects — the join between a Folder Grant, an
approval, the effect ledger and a worker lease.

Every real filesystem side effect is:

  1. authorized by an immutable, workspace-bound grant (no client path is
     authority; every escape vector rejected) — :mod:`folder_grant`;
  2. authorized by a single-use, expiring approval bound to the effect digest
     (operation + canonical path + workspace + content digest) — :mod:`approval`;
  3. registered as a ledgered effect whose id folds run + principal + resolved
     target + content digest + approval, so a retry collapses to the same effect,
     a different content is a different effect, and cross-workspace/principal
     replay is impossible — :mod:`effect_ledger`;
  4. claimed atomically for exactly one worker under a time-bounded lease
     (:mod:`worker_lease`), so only the lease holder settles and an abandoned
     lease is swept to a known state;
  5. re-validated at the actual operation boundary (:func:`open_within_grant`):
     the path is resolved *again* and containment re-checked after the file is
     opened, defeating a symlink/junction swapped in after the first check
     (TOCTOU).

Fail-closed throughout: any binding mismatch, expiry/revocation, missing
permission, escape, or spent/forged approval raises before the side effect is
claimed. Pure-stdlib on top of the ledger; no new runtime dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.approval import (
    ApprovalConsumedError,
    compute_effect_digest,
    content_digest,
    reserve_and_consume_authorization,
)
from youtab_runtime.effect_authorization import EffectAuthorization
from youtab_runtime.folder_grant import (
    FolderGrant,
    GrantRevokedError,
    GrantScopeError,
    assert_no_reparse_ancestors,
    resolve_within_grant,
)
from youtab_runtime.run_journal import Principal
from youtab_runtime.worker_lease import (
    WorkspaceMismatchError,
    assert_lease_holder,
    assert_workspace,
    lease_detail,
)

__all__ = [
    "GrantedFsEffect",
    "claim_granted_fs_effect",
    "settle_committed",
    "settle_unknown",
    "open_within_grant",
]

_OP_TO_ACTION = {"read": "fs.read", "write": "fs.write", "create": "fs.create"}


@dataclass(frozen=True)
class GrantedFsEffect:
    """Result of claiming a grant-bound fs effect.

    ``won`` is True only for the single caller that atomically claimed an
    executable effect; every replay/concurrent caller/restart gets ``won=False``
    with the effect's current ledger ``state`` and MUST NOT perform the side
    effect. ``owner_token`` is the lease the winner must present to settle.
    """

    effect_id: str
    safe_path: str
    won: bool
    state: str
    owner_token: str


def _target_scope(workspace_id: str, safe_path: str, content_sha256: str) -> dict:
    # Dict scope → the ledger canonicalizes it deterministically and folds
    # workspace + canonical path + content digest into the effect id. The
    # authorization is bound to this same (op, path, ws, content) digest, so the
    # binding is captured WITHOUT folding the single-use nonce (which must not
    # change the effect identity, or a re-authorized retry would not dedup).
    return {"ws": workspace_id, "path": safe_path, "content": content_sha256}


def claim_granted_fs_effect(
    grant: FolderGrant,
    requested_path: str,
    *,
    operation: str,
    run_id: str,
    principal: Principal,
    workspace_id: str,
    authorization: EffectAuthorization,
    owner_token: str,
    content: Optional[bytes] = None,
    production: bool = True,
    authority_public_keys: Optional[Mapping[str, str]] = None,
    test_authority_keys: Optional[Mapping[str, str]] = None,
    lease_ttl_seconds: float = 300.0,
    revocation_check: Optional[Callable[[str], bool]] = None,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> GrantedFsEffect:
    """Authorize (signed), register and atomically claim a grant-bound fs effect
    under a worker lease. Perform the real op only when ``won`` is True, then call
    :func:`settle_committed` / :func:`settle_unknown` with the same ``owner_token``.

    ``authorization`` is a signed :class:`EffectAuthorization` from an external
    authority; the Runtime verifies + single-use-consumes it, never mints it.
    """
    import time as _time

    now = _time.time() if now is None else now
    if not isinstance(principal, Principal):
        raise _ledger.EffectLedgerError("principal must be a Principal instance")
    if operation not in _OP_TO_ACTION:
        raise GrantScopeError(f"unknown operation {operation!r}")

    # 1) grant authority (fresh validation; the Principal IS the binding identity)
    safe_path = resolve_within_grant(
        grant, requested_path, operation=operation,
        tenant_id=principal.tenant, principal_id=principal.user,
        workspace_id=workspace_id, now=now,
    )

    # 2) compute the effect identity (folds op + path + ws + content + authorization)
    cdigest = content_digest(content)
    edigest = compute_effect_digest(operation, safe_path, workspace_id, cdigest)
    action = _OP_TO_ACTION[operation]
    scope = _target_scope(workspace_id, safe_path, cdigest)
    effect_id = _ledger.compute_effect_id(run_id, principal, action, scope)

    # 3) a signed authorization authorizes CREATION of the effect, exactly once.
    #    A genuine retry/restart finds the effect already registered and does NOT
    #    re-verify/re-consume; a first attempt must verify + single-use-consume a
    #    valid authorization or fail closed.
    if _ledger.get_effect(effect_id, principal, db_path=db_path) is None:
        # TOCTOU: revocation may have happened between resolution and effect.
        # Checked before spending the single-use authorization.
        if revocation_check is not None and revocation_check(grant.grant_id):
            raise GrantRevokedError("grant revoked before effect claim")
        try:
            reserve_and_consume_authorization(
                authorization, edigest, principal, workspace_id,
                production=production, now=now,
                production_keys=authority_public_keys, test_keys=test_authority_keys,
                db_path=db_path,
            )
        except ApprovalConsumedError:
            # Only acceptable if a concurrent creator already registered THIS
            # effect with the same one-time authorization; otherwise it is spent
            # for a different effect -> fail closed.
            if _ledger.get_effect(effect_id, principal, db_path=db_path) is None:
                raise

    # 4) register (idempotent) + atomically claim under a lease. Workspace is
    #    stored on the effect so receipt/settle/reconcile can bind it (item 2).
    record = _ledger.begin_effect(
        run_id, principal, action, scope,
        correlation_id=grant.grant_id, detail={"workspace": workspace_id},
        db_path=db_path,
    )
    won, record = _ledger.try_claim(
        record.effect_id, principal,
        detail=lease_detail(owner_token, now + float(lease_ttl_seconds)),
        db_path=db_path,
    )
    return GrantedFsEffect(
        effect_id=record.effect_id, safe_path=safe_path,
        won=won, state=record.state.value, owner_token=str(owner_token),
    )


def settle_committed(
    effect: GrantedFsEffect, principal: Principal, *, workspace_id: str,
    db_path: Optional[Path] = None,
) -> str:
    """Record the receipt for a proven side effect (at-most-once). Only the lease
    holder in the bound workspace may settle."""
    assert_workspace(effect.effect_id, principal, workspace_id, db_path=db_path)
    assert_lease_holder(effect.effect_id, principal, effect.owner_token, db_path=db_path)
    return _ledger.mark_committed(effect.effect_id, principal, db_path=db_path).state.value


def settle_unknown(
    effect: GrantedFsEffect, principal: Principal, *, workspace_id: str,
    db_path: Optional[Path] = None,
) -> str:
    """Record an unproven outcome — never blind-retried; left for reconciliation.
    Only the lease holder in the bound workspace may settle."""
    assert_workspace(effect.effect_id, principal, workspace_id, db_path=db_path)
    assert_lease_holder(effect.effect_id, principal, effect.owner_token, db_path=db_path)
    return _ledger.mark_unknown(effect.effect_id, principal, db_path=db_path).state.value


def _same_object(a: os.stat_result, b: os.stat_result) -> bool:
    """True only if two stat results provably refer to the same object.

    Requires a usable (device, inode) identity; a zero identity (unavailable on
    the platform) is treated as *not* verifiable so an effectful write fails
    closed rather than trusting an unverified handle.
    """
    return (
        a.st_ino != 0 and a.st_dev != 0
        and a.st_ino == b.st_ino and a.st_dev == b.st_dev
    )


def _final_path_from_handle(fd: int) -> Optional[str]:
    """The OS-verified final path of the OPEN ``fd``, derived from the kernel
    handle itself — NOT a second path-string ``realpath()`` of the request.

    This is the authoritative containment source: because it comes from the
    already-opened handle, a reparse point / junction / symlink swapped in AFTER
    the open cannot redirect what we validate. Returns a normcased absolute path,
    or ``None`` when the platform cannot supply a handle-derived path (caller then
    falls back to device+inode identity and fails closed for effectful writes).
    """
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            handle = msvcrt.get_osfhandle(fd)
            fn = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            fn.restype = wintypes.DWORD
            fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            volume_name_dos = 0x0
            length = fn(handle, None, 0, volume_name_dos)
            if not length:
                return None
            buf = ctypes.create_unicode_buffer(length)
            got = fn(handle, buf, length, volume_name_dos)
            if not got or got >= length:
                return None
            final = buf.value
            # Strip the \\?\ (and \\?\UNC\) prefix GetFinalPathNameByHandleW adds.
            if final.startswith("\\\\?\\UNC\\"):
                final = "\\\\" + final[len("\\\\?\\UNC\\"):]
            elif final.startswith("\\\\?\\"):
                final = final[len("\\\\?\\"):]
            return os.path.normcase(os.path.abspath(final))
        except (OSError, ValueError, AttributeError):
            return None
    # POSIX: the /proc/self/fd/<fd> magic symlink resolves to the fd's real path.
    try:
        if os.path.isdir("/proc/self/fd"):
            return os.path.normcase(os.path.realpath(f"/proc/self/fd/{fd}"))
    except OSError:
        return None
    return None


def _assert_final_path_within(root_real: str, final_path: str) -> None:
    if not (final_path == root_real or final_path.startswith(root_real + os.sep)):
        raise GrantScopeError("opened handle's final path escapes the grant root")


def open_within_grant(
    grant: FolderGrant,
    requested_path: str,
    *,
    operation: str,
    principal: Principal,
    workspace_id: str,
    now: Optional[float] = None,
    _pre_open_hook: Optional[Callable[[], None]] = None,
) -> int:
    """Re-validate at the actual operation boundary and return an open fd.

    Defence in depth against TOCTOU:
      1. resolve the path *again* (never trust an earlier resolution) —
         realpath containment;
      2. validate that no ancestor component is a reparse point (symlink/junction)
         *before* opening — covers a junctioned parent that ``O_NOFOLLOW`` on the
         leaf would not;
      3. open with ``O_NOFOLLOW`` on the leaf where supported;
      4. *after* opening, re-validate the ancestors and confirm the opened handle
         refers to the same object as the granted path (device+inode); for an
         effectful write this identity is mandatory — if it cannot be verified,
         fail closed.

    ``_pre_open_hook`` is test-only (simulates an attacker swap in the window
    between validation and open). Fails closed (closing the fd) on any escape;
    caller owns closing the returned fd.
    """
    safe_path = resolve_within_grant(
        grant, requested_path, operation=operation,
        tenant_id=principal.tenant, principal_id=principal.user,
        workspace_id=workspace_id, now=now,
    )
    root_real = grant.root_real()
    assert_no_reparse_ancestors(root_real, safe_path)

    if _pre_open_hook is not None:  # test-only injection point
        _pre_open_hook()

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    binary = getattr(os, "O_BINARY", 0)
    if operation == "read":
        flags = os.O_RDONLY | nofollow | binary
    elif operation == "create":
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | binary
    else:  # write
        flags = os.O_WRONLY | os.O_CREAT | nofollow | binary
    fd = os.open(safe_path, flags, 0o600)
    try:
        # Re-validate ancestors post-open: a parent swapped in during the window
        # is now a reparse point and is rejected.
        assert_no_reparse_ancestors(root_real, safe_path)
        # Authoritative containment derived from the OPEN handle itself (Windows
        # GetFinalPathNameByHandleW / POSIX /proc/self/fd), NOT a second
        # path-string realpath() of the request — so a junction/symlink swapped in
        # after the open cannot redirect what we validate.
        final_from_handle = _final_path_from_handle(fd)
        if final_from_handle is not None:
            _assert_final_path_within(root_real, final_from_handle)
        # An effectful write requires a provably-verified handle. The handle-derived
        # final path above is authoritative when available; otherwise fall back to
        # device+inode identity plus a path-string containment cross-check, and fail
        # closed if that identity is unavailable rather than trust an unverified fd.
        if operation in ("write", "create") and final_from_handle is None:
            st_fd = os.fstat(fd)
            st_path = os.stat(safe_path)
            if not _same_object(st_fd, st_path):
                raise GrantScopeError(
                    "cannot verify the opened handle (no final-path-by-handle and "
                    "no usable device/inode identity); refusing effectful write"
                )
            opened_real = os.path.normcase(os.path.realpath(safe_path))
            _assert_final_path_within(root_real, opened_real)
    except Exception:
        os.close(fd)
        raise
    return fd
