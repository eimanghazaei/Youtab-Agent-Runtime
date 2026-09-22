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
from typing import Callable, Optional

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.approval import (
    ApprovalConsumedError,
    compute_effect_digest,
    consume_approval,
    content_digest,
)
from youtab_runtime.folder_grant import (
    FolderGrant,
    GrantRevokedError,
    GrantScopeError,
    resolve_within_grant,
)
from youtab_runtime.run_journal import Principal
from youtab_runtime.worker_lease import assert_lease_holder, lease_detail

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


def _target_scope(workspace_id: str, safe_path: str, content_sha256: str, approval_id: str) -> dict:
    # Dict scope → the ledger canonicalizes it deterministically and folds
    # workspace + content digest + approval binding into the effect id.
    return {
        "ws": workspace_id, "path": safe_path,
        "content": content_sha256, "approval": approval_id,
    }


def claim_granted_fs_effect(
    grant: FolderGrant,
    requested_path: str,
    *,
    operation: str,
    run_id: str,
    principal: Principal,
    workspace_id: str,
    approval_id: str,
    owner_token: str,
    content: Optional[bytes] = None,
    lease_ttl_seconds: float = 300.0,
    revocation_check: Optional[Callable[[str], bool]] = None,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> GrantedFsEffect:
    """Authorize, approval-bind, register and atomically claim a grant-bound fs
    effect under a worker lease. Perform the real op only when ``won`` is True,
    then call :func:`settle_committed` / :func:`settle_unknown` with the same
    ``owner_token``.
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

    # 2) compute the effect identity (folds op + path + ws + content + approval)
    cdigest = content_digest(content)
    edigest = compute_effect_digest(operation, safe_path, workspace_id, cdigest)
    action = _OP_TO_ACTION[operation]
    scope = _target_scope(workspace_id, safe_path, cdigest, approval_id)
    effect_id = _ledger.compute_effect_id(run_id, principal, action, scope)

    # 3) approval authorizes CREATION of the effect, exactly once. A genuine
    #    retry/restart finds the effect already registered and does NOT re-spend
    #    the approval; a first attempt must consume a valid, unexpired,
    #    digest-bound, single-use approval or fail closed.
    if _ledger.get_effect(effect_id, principal, db_path=db_path) is None:
        # TOCTOU: revocation may have happened between resolution and effect.
        # Checked before spending the single-use approval.
        if revocation_check is not None and revocation_check(grant.grant_id):
            raise GrantRevokedError("grant revoked before effect claim")
        try:
            consume_approval(
                approval_id, edigest, principal, workspace_id,
                now=now, db_path=db_path,
            )
        except ApprovalConsumedError:
            # Only acceptable if a concurrent creator already registered THIS
            # effect with the same one-time approval; otherwise the approval is
            # spent for a different effect -> fail closed.
            if _ledger.get_effect(effect_id, principal, db_path=db_path) is None:
                raise

    # 4) register (idempotent) + atomically claim under a lease
    record = _ledger.begin_effect(
        run_id, principal, action, scope,
        correlation_id=grant.grant_id, db_path=db_path,
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
    effect: GrantedFsEffect, principal: Principal, *, db_path: Optional[Path] = None
) -> str:
    """Record the receipt for a proven side effect (at-most-once). Only the lease
    holder may settle."""
    assert_lease_holder(effect.effect_id, principal, effect.owner_token, db_path=db_path)
    return _ledger.mark_committed(effect.effect_id, principal, db_path=db_path).state.value


def settle_unknown(
    effect: GrantedFsEffect, principal: Principal, *, db_path: Optional[Path] = None
) -> str:
    """Record an unproven outcome — never blind-retried; left for reconciliation.
    Only the lease holder may settle."""
    assert_lease_holder(effect.effect_id, principal, effect.owner_token, db_path=db_path)
    return _ledger.mark_unknown(effect.effect_id, principal, db_path=db_path).state.value


def open_within_grant(
    grant: FolderGrant,
    requested_path: str,
    *,
    operation: str,
    principal: Principal,
    workspace_id: str,
    now: Optional[float] = None,
) -> int:
    """Re-validate at the actual operation boundary and return an open fd.

    Resolves the path *again* (never trusting an earlier resolution), opens it
    with ``O_NOFOLLOW`` on the final component where the platform supports it,
    then re-checks that the opened target's real path is still inside the grant —
    defeating a symlink/junction/reparse point swapped in after an earlier check.
    Fails closed (closing the fd) on any escape. Caller owns closing the fd.
    """
    safe_path = resolve_within_grant(
        grant, requested_path, operation=operation,
        tenant_id=principal.tenant, principal_id=principal.user,
        workspace_id=workspace_id, now=now,
    )
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
        root_real = grant.root_real()
        opened_real = os.path.normcase(os.path.realpath(safe_path))
        if not (opened_real == root_real or opened_real.startswith(root_real + os.sep)):
            raise GrantScopeError("opened path escapes the grant root")
    except Exception:
        os.close(fd)
        raise
    return fd
