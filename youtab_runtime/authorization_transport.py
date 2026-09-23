"""Runtime-side effect-authorization transport (Owner item 5).

The Runtime NEVER mints authority. When ``AuthorityBoundary.decide_tool`` refuses
an external effect it emits an :class:`~youtab_runtime.contracts.EffectProposal`
(ADR-0002 DP4-4b); the Brain (Simorgh) evaluates it and returns a signed
:class:`~youtab_runtime.effect_authorization.EffectAuthorization`. This module is
the Runtime side of that round-trip.

It records which proposals it emitted, and accepts an inbound signed authorization
ONLY when the authorization correlates to a **still-pending** proposal for the
same command + tool + request digest — rejecting:

  * an **unsolicited** authorization (references no proposal the Runtime emitted);
  * an authorization for an **already-changed / superseded** proposal (a newer
    proposal for the same ``(command_id, tool_name)`` supersedes the older one,
    and a re-used ``proposal_id`` whose request digest no longer matches is
    refused);
  * an authorization whose proposal was **already consumed**.

Only then does it delegate key / issuer / workspace / principal / effect-digest /
expiry / single-use verification to
:func:`youtab_runtime.approval.reserve_and_consume_authorization`, and it returns
the authorization provenance to persist in the receipt.

Production stays **fail-closed**: with no recorded proposal, or no inbound signed
authorization, nothing is accepted. A cross-repo Simorgh issuer implements the
signing side against the published ``EffectAuthorization`` contract; until an
approved Simorgh SHA does, the managed effect path denies exactly as today. A
Runtime signer is never inserted as a substitute.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from youtab_runtime.approval import reserve_and_consume_authorization
from youtab_runtime.contracts import EffectProposal
from youtab_runtime.effect_authorization import EffectAuthorization
from youtab_runtime.effect_ledger import _immediate_txn
from youtab_runtime.run_journal import Principal, default_db_path

__all__ = [
    "AuthorizationTransportError",
    "UnsolicitedAuthorizationError",
    "ProposalChangedError",
    "ProposalSupersededError",
    "AuthorizationProvenance",
    "record_proposal",
    "correlate_proposal",
    "mark_proposal_consumed",
    "accept_authorization",
]


class AuthorizationTransportError(Exception):
    """Fail-closed base — the authorization could not be correlated + trusted."""


class UnsolicitedAuthorizationError(AuthorizationTransportError):
    """No pending proposal matches this authorization — it was never solicited."""


class ProposalChangedError(AuthorizationTransportError):
    """The referenced proposal's request digest no longer matches (request changed)."""


class ProposalSupersededError(AuthorizationTransportError):
    """The referenced proposal was superseded by a newer one or already consumed."""


@dataclass(frozen=True)
class AuthorizationProvenance:
    """The provenance of a consumed authorization, to persist in the receipt so the
    authorization id + signer are auditable after the fact."""

    authorization_id: str
    proposal_id: str
    command_id: str
    key_id: str
    issuer: str
    signature: str


def _ensure_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pending_proposals (
             proposal_id     TEXT PRIMARY KEY,
             command_id      TEXT NOT NULL,
             tool_name       TEXT NOT NULL,
             tenant          TEXT NOT NULL,
             request_digest  TEXT NOT NULL,
             epoch           INTEGER NOT NULL,
             status          TEXT NOT NULL,      -- 'pending' | 'consumed' | 'superseded'
             recorded_at     REAL NOT NULL
           )"""
    )


def record_proposal(
    proposal: EffectProposal,
    *,
    authorization_epoch: int,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> None:
    """Record an emitted proposal so a later authorization can be correlated to it.

    Re-recording the same ``proposal_id`` is idempotent. Recording a *newer*
    proposal for the same ``(command_id, tool_name)`` supersedes the older pending
    one(s), so an authorization arriving for the stale proposal is refused as
    superseded (the "already-changed proposal" case).
    """
    now = time.time() if now is None else now
    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_table(conn)
        # A new proposal for the same command+tool supersedes older pending ones.
        conn.execute(
            "UPDATE pending_proposals SET status='superseded' "
            "WHERE command_id=? AND tool_name=? AND proposal_id<>? AND status='pending'",
            (proposal.command_id, proposal.tool_name, proposal.proposal_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO pending_proposals "
            "(proposal_id, command_id, tool_name, tenant, request_digest, epoch, status, recorded_at) "
            "VALUES (?,?,?,?,?,?, 'pending', ?)",
            (proposal.proposal_id, proposal.command_id, proposal.tool_name,
             proposal.tenant_id, proposal.arguments_digest,
             int(authorization_epoch), float(now)),
        )


def correlate_proposal(
    auth: EffectAuthorization,
    *,
    principal: Principal,
    db_path: Optional[Path] = None,
) -> None:
    """Correlate an authorization to a still-pending proposal — WITHOUT consuming
    anything and WITHOUT verifying the signature (the effect claim does the crypto
    + single-use authorization consume).

    Fails closed on an unsolicited authorization (no proposal reference, or no
    matching pending proposal for this command/tenant), a changed request digest,
    or a superseded/consumed proposal. Use this when a downstream step (e.g.
    ``grant_fs.claim_granted_fs_effect``) performs the cryptographic verify +
    single-use consume, so the authorization is not consumed twice.
    """
    if not isinstance(principal, Principal):
        raise AuthorizationTransportError("principal must be a Principal instance")
    if auth.proposal_id is None or auth.request_digest is None:
        raise UnsolicitedAuthorizationError(
            "authorization carries no proposal reference (unsolicited)"
        )
    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT command_id, tenant, request_digest, status "
            "FROM pending_proposals WHERE proposal_id=?",
            (auth.proposal_id,),
        ).fetchone()
        if row is None:
            raise UnsolicitedAuthorizationError(
                "no proposal was emitted for this authorization"
            )
        p_command_id, p_tenant, p_request_digest, p_status = row
        if p_command_id != auth.command_id or p_tenant != principal.tenant:
            raise UnsolicitedAuthorizationError(
                "authorization does not match the proposal's command/tenant"
            )
        if p_request_digest != auth.request_digest:
            raise ProposalChangedError("authorization is for a changed request")
        if p_status != "pending":
            raise ProposalSupersededError("proposal already superseded or consumed")


def mark_proposal_consumed(
    proposal_id: str, *, db_path: Optional[Path] = None
) -> None:
    """Atomically mark a pending proposal consumed — only the first caller wins.
    Raises :class:`ProposalSupersededError` if it was not still pending."""
    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE pending_proposals SET status='consumed' "
            "WHERE proposal_id=? AND status='pending'",
            (proposal_id,),
        )
        if cur.rowcount != 1:
            raise ProposalSupersededError("proposal was consumed concurrently")


def accept_authorization(
    auth: EffectAuthorization,
    *,
    principal: Principal,
    workspace_id: str,
    expected_effect_digest: str,
    production: bool,
    now: float,
    production_keys: Optional[Mapping[str, str]] = None,
    test_keys: Optional[Mapping[str, str]] = None,
    db_path: Optional[Path] = None,
) -> AuthorizationProvenance:
    """Correlate, verify and single-use-consume an inbound signed authorization in
    one step (the all-in-one used where no separate effect-claim consumes it).

    Fails closed on an unsolicited authorization, a changed request digest, a
    superseded/consumed proposal, and any key/issuer/workspace/principal/
    effect-digest/expiry/single-use failure. On success the proposal is marked
    consumed and the provenance is returned for the receipt.
    """
    correlate_proposal(auth, principal=principal, db_path=db_path)
    # correlate_proposal raised if these were None; narrow for the type checker.
    assert auth.proposal_id is not None and auth.request_digest is not None
    reserve_and_consume_authorization(
        auth, expected_effect_digest, principal, workspace_id,
        production=production, now=now,
        production_keys=production_keys, test_keys=test_keys, db_path=db_path,
    )
    mark_proposal_consumed(auth.proposal_id, db_path=db_path)
    return AuthorizationProvenance(
        authorization_id=auth.authorization_id,
        proposal_id=auth.proposal_id,
        command_id=auth.command_id,
        key_id=auth.key_id,
        issuer=auth.issuer,
        signature=auth.signature,
    )
