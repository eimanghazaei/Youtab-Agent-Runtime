"""Grant-bound filesystem effects — the join between a Folder Grant and the
effect ledger.

A grant proves *authority* over a path (see :mod:`youtab_runtime.folder_grant`);
the effect ledger proves *exactly-once* execution with an auditable receipt (see
:mod:`youtab_runtime.effect_ledger`). This module ties the two together so that
every real filesystem side effect is:

  1. authorized by an immutable, workspace-bound grant (no client path is
     authority, every escape vector rejected);
  2. registered as a ledgered effect whose id folds the run, principal and the
     *resolved* target — so a retry/restart collapses to the same effect and a
     different path is a different effect (never a silent overwrite of another);
  3. claimed atomically for exactly one worker (:func:`try_claim`), so a
     crash-then-requeue cannot double-apply;
  4. settled to a receipt: ``committed`` on success, ``unknown`` on an unproven
     outcome (never blind-retried), leaving reconciliation to the ledger.

The workspace id is folded into the target scope so two grants that differ only
by workspace produce distinct effect ids — cross-workspace replay is impossible.
Pure-stdlib on top of the ledger; no new runtime dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.folder_grant import FolderGrant, resolve_within_grant
from youtab_runtime.run_journal import Principal

__all__ = ["GrantedFsEffect", "claim_granted_fs_effect", "settle_committed", "settle_unknown"]

_OP_TO_ACTION = {"read": "fs.read", "write": "fs.write", "create": "fs.create"}


@dataclass(frozen=True)
class GrantedFsEffect:
    """Result of claiming a grant-bound fs effect.

    ``won`` is True only for the single caller that atomically claimed an
    executable effect; every replay/concurrent caller/restart gets ``won=False``
    with the effect's current ledger ``state`` and MUST NOT perform the side
    effect. ``safe_path`` is the realpath'd, in-grant target.
    """

    effect_id: str
    safe_path: str
    won: bool
    state: str


def _target_scope(workspace_id: str, safe_path: str) -> dict:
    # Dict scope → the ledger canonicalizes it deterministically AND folds the
    # workspace, so an identical path in a different workspace is a distinct
    # effect (no cross-workspace collision or replay).
    return {"ws": workspace_id, "path": safe_path}


def claim_granted_fs_effect(
    grant: FolderGrant,
    requested_path: str,
    *,
    operation: str,
    run_id: str,
    principal: Principal,
    workspace_id: str,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> GrantedFsEffect:
    """Authorize, register and atomically claim a grant-bound fs effect.

    Fail-closed: any grant binding mismatch, expiry/revocation, missing
    permission or path escape raises a ``FolderGrantError`` before the effect is
    ever registered. On success the caller performs the real fs op **only when
    ``won`` is True**, then calls :func:`settle_committed` (success) or
    :func:`settle_unknown` (unproven outcome).
    """
    if not isinstance(principal, Principal):
        raise _ledger.EffectLedgerError("principal must be a Principal instance")
    if operation not in _OP_TO_ACTION:
        # resolve_within_grant also rejects, but fail fast on the action mapping.
        from youtab_runtime.folder_grant import GrantScopeError

        raise GrantScopeError(f"unknown operation {operation!r}")

    # The Principal (tenant,user) IS the grant's binding identity.
    safe_path = resolve_within_grant(
        grant,
        requested_path,
        operation=operation,
        tenant_id=principal.tenant,
        principal_id=principal.user,
        workspace_id=workspace_id,
        now=now,
    )

    logical_action = _OP_TO_ACTION[operation]
    scope = _target_scope(workspace_id, safe_path)
    record = _ledger.begin_effect(
        run_id, principal, logical_action, scope,
        correlation_id=grant.grant_id, db_path=db_path,
    )
    won, record = _ledger.try_claim(
        record.effect_id, principal, db_path=db_path
    )
    return GrantedFsEffect(
        effect_id=record.effect_id, safe_path=safe_path,
        won=won, state=record.state.value,
    )


def settle_committed(
    effect: GrantedFsEffect, principal: Principal, *, db_path: Optional[Path] = None
) -> str:
    """Record the receipt for a proven side effect (at-most-once)."""
    rec = _ledger.mark_committed(effect.effect_id, principal, db_path=db_path)
    return rec.state.value


def settle_unknown(
    effect: GrantedFsEffect, principal: Principal, *, db_path: Optional[Path] = None
) -> str:
    """Record an unproven outcome — never blind-retried; left for reconciliation."""
    rec = _ledger.mark_unknown(effect.effect_id, principal, db_path=db_path)
    return rec.state.value
