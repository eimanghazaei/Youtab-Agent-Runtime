"""Managed filesystem effect gate — the single chokepoint every managed
filesystem side effect must pass through.

It joins the admission/authority chain (the Ed25519 Simorgh grant, sealed into an
:class:`AdmittedCommand`) to the grant-bound fs spine (folder grant → signed
effect authorization → ledgered effect → worker lease → TOCTOU-safe open →
receipt). Identity (tenant, principal, canonical workspace) is read ONLY from the
verified admitted grant — never from caller arguments — so a caller cannot widen
its own authority. The workspace's canonical root is server-supplied.

Design note: ``AuthorityBoundary.decide_tool`` deliberately refuses to authorize
an external WRITE inside the runtime and instead emits an ``EffectProposal`` for
the Brain to authorize (ADR-0002). This gate is the *authorized-effect execution*
path: it runs only once the Brain has returned a signed
:class:`EffectAuthorization` for that proposal, which the gate verifies and
single-use-consumes. It does not, and must not, let the runtime self-authorize.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Tuple

from youtab_runtime import grant_fs
from youtab_runtime.admission import AdmittedCommand
from youtab_runtime.effect_authorization import EffectAuthorization
from youtab_runtime.folder_grant import FolderGrant
from youtab_runtime.grant_fs import GrantedFsEffect
from youtab_runtime.run_journal import Principal

__all__ = ["ManagedFsGateError", "enforce_managed_fs_effect", "settle"]

WORKSPACE_UNSCOPED = "-"


class ManagedFsGateError(Exception):
    """Fail-closed: the admitted context is missing or unusable."""


def _identity(admitted: AdmittedCommand) -> Tuple[Principal, str]:
    if not isinstance(admitted, AdmittedCommand):
        raise ManagedFsGateError("a verified AdmittedCommand is required")
    admitted.verify_proof()  # reject a smuggled/forged/binding-swapped handle
    env = admitted.envelope
    ws = getattr(env, "workspace_id", WORKSPACE_UNSCOPED) or WORKSPACE_UNSCOPED
    return Principal(env.tenant_id, env.user_id), ws


def _grant_for(admitted: AdmittedCommand, workspace_root, operation: str) -> Tuple[FolderGrant, Principal, str]:
    principal, ws = _identity(admitted)
    grant = FolderGrant(
        grant_id=admitted.envelope.command_id,
        tenant_id=principal.tenant, principal_id=principal.user, workspace_id=ws,
        canonical_root=str(workspace_root), permissions=frozenset({operation}),
    )
    return grant, principal, ws


def enforce_managed_fs_effect(
    admitted: AdmittedCommand,
    *,
    operation: str,
    requested_path: str,
    workspace_root,
    run_id: str,
    authorization: EffectAuthorization,
    owner_token: str,
    content: Optional[bytes] = None,
    production: bool = True,
    authority_public_keys: Optional[Mapping[str, str]] = None,
    test_authority_keys: Optional[Mapping[str, str]] = None,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> Tuple[Optional[int], GrantedFsEffect]:
    """Authorize and claim a managed fs effect, returning ``(fd_or_None, effect)``.

    Identity comes from the verified admitted grant. Any grant/authorization
    failure raises BEFORE any file is opened (no side effect, no ledger row for a
    pre-admission rejection). ``fd`` is ``None`` when the claim did not win
    (replay / in progress / already committed) — the caller performs the real IO
    only when ``fd`` is not None, then calls :func:`settle`. The caller owns
    closing a returned fd.
    """
    grant, principal, ws = _grant_for(admitted, workspace_root, operation)
    effect = grant_fs.claim_granted_fs_effect(
        grant, requested_path, operation=operation, run_id=run_id,
        principal=principal, workspace_id=ws, authorization=authorization,
        owner_token=owner_token, content=content, production=production,
        authority_public_keys=authority_public_keys,
        test_authority_keys=test_authority_keys, db_path=db_path, now=now,
    )
    if not effect.won:
        return None, effect
    fd = grant_fs.open_within_grant(
        grant, requested_path, operation=operation,
        principal=principal, workspace_id=ws, now=now,
    )
    return fd, effect


def settle(
    effect: GrantedFsEffect, admitted: AdmittedCommand, *, committed: bool,
    db_path: Optional[Path] = None,
) -> str:
    """Settle the effect's receipt (committed on proof, unknown otherwise), binding
    the admitted principal + workspace and the lease holder."""
    principal, ws = _identity(admitted)
    if committed:
        return grant_fs.settle_committed(effect, principal, workspace_id=ws, db_path=db_path)
    return grant_fs.settle_unknown(effect, principal, workspace_id=ws, db_path=db_path)
