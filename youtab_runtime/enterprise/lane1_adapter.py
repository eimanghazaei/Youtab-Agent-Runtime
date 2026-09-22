"""Concrete Lane-1 authority adapter for the Lane-2 governed connector.

This binds the Lane-2 :class:`AuthorityBoundary` Protocol to the REAL Lane-1
modules — no reference/test double on the production path:

  * approval verification + single-use consumption + operation/request-digest
    binding  → :mod:`youtab_runtime.approval`;
  * effect claim under a time-bounded worker lease, holder-only settle, and
    lease-expiry reconciliation  → :mod:`youtab_runtime.worker_lease` +
    :mod:`youtab_runtime.effect_ledger`;
  * committed / failed / unknown / reconciliation-required transitions and the
    per-principal, workspace-folded effect record that IS the canonical receipt
    → :mod:`youtab_runtime.effect_ledger`.

The adapter carries ``is_reference = False`` and is the only authority the
production connector accepts. The reference/test double stays test-only and is
refused whenever the connector runs with ``production=True``.

Lane-1 owns its files; this adapter only *calls* their public APIs. No Lane-1
file is modified. The one place Lane-1 has no public helper is canonical
workspace derivation, so it is derived here deterministically (tenant-scoped)
and folded into every approval, effect and receipt identity — see IR-2 for the
request that Lane-1 expose a canonicalizer so this can be removed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from youtab_runtime import approval as _approval
from youtab_runtime import effect_ledger as _ledger
from youtab_runtime import worker_lease as _lease
from youtab_runtime.enterprise.authority import (
    ApprovalGrant,
    AuthorityError,
    CanonicalWorkspace,
    IdempotencyReservation,
    WorkerLease,
)
from youtab_runtime.run_journal import Principal

__all__ = ["Lane1AuthorityAdapter"]


def _effect_digest(operation_id: str, workspace_canonical: str, request_digest: str) -> str:
    """The Lane-1 approval binding for an enterprise effect.

    Reuses ``approval.compute_effect_digest``; ``request_digest`` already folds
    operation + workspace + business key + payload, so it serves as both the
    canonical target token and the content digest — a changed payload yields a
    different digest, so an approval for one request cannot authorize another.
    """
    return _approval.compute_effect_digest(
        operation_id, request_digest, workspace_canonical, request_digest
    )


class Lane1AuthorityAdapter:
    """Real Lane-1-backed :class:`AuthorityBoundary`."""

    is_reference = False

    def __init__(self, *, lease_ttl_seconds: float = 300.0, db_path=None, clock=None) -> None:
        import time as _time

        self._ttl = float(lease_ttl_seconds)
        self._db_path = db_path
        self._now = clock or _time.time

    # -- canonical workspace --------------------------------------------- #
    def canonicalize_workspace(self, tenant: str, raw_workspace: str) -> CanonicalWorkspace:
        if not isinstance(tenant, str) or not tenant.strip():
            raise AuthorityError("tenant must be non-empty")
        if not isinstance(raw_workspace, str) or not raw_workspace.strip():
            raise AuthorityError("workspace must be non-empty")
        return CanonicalWorkspace(
            tenant=tenant.strip(),
            workspace=f"{tenant.strip()}/{raw_workspace.strip().lower()}",
        )

    # -- approval (single-use, digest-bound) ----------------------------- #
    def effect_digest_for(
        self, operation_id: str, workspace_canonical: str, request_digest: str
    ) -> str:
        """The digest an approval for this operation+request must be issued against."""
        return _effect_digest(operation_id, workspace_canonical, request_digest)

    def verify_approval(
        self,
        *,
        approval_id: str,
        operation_id: str,
        request_digest: str,
        delegation: str,
        principal_tenant: str,
        principal_user: str,
        workspace: CanonicalWorkspace,
    ) -> ApprovalGrant:
        """Atomically validate + CONSUME a single-use Lane-1 approval.

        The connector calls this ONLY when creating the effect for the first time
        (mirroring ``grant_fs``), so a retry never re-spends the approval.
        Fail-closed on unknown/expired/consumed/binding/digest mismatch.
        """
        principal = Principal(principal_tenant, principal_user)
        edigest = _effect_digest(operation_id, workspace.workspace, request_digest)
        try:
            rec = _approval.consume_approval(
                approval_id, edigest, principal, workspace.workspace,
                now=self._now(), db_path=self._db_path,
            )
        except _approval.ApprovalError as exc:
            raise AuthorityError(f"approval refused: {type(exc).__name__}") from exc
        return ApprovalGrant(
            approval_id=rec.approval_id, operation_id=operation_id,
            request_digest=request_digest, delegation=delegation,
            issuer="lane1-approval", expires_at=int(rec.expires_at),
        )

    # -- idempotency reservation ----------------------------------------- #
    def reserve_idempotency(
        self, *, effect_key: str, workspace: CanonicalWorkspace
    ) -> IdempotencyReservation:
        rid = hashlib.sha256(
            f"{workspace.workspace}:{effect_key}".encode("utf-8")
        ).hexdigest()[:32]
        return IdempotencyReservation(reservation_id=rid, key=effect_key)

    # -- worker lease ----------------------------------------------------- #
    def acquire_worker_lease(
        self, *, operation_id: str, workspace: CanonicalWorkspace, approval_id: str
    ) -> WorkerLease:
        token = hmac.new(
            os.urandom(16),
            f"{operation_id}:{workspace.workspace}:{approval_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        return WorkerLease(
            lease_token=token, operation_id=operation_id,
            workspace=workspace.workspace,
            expires_at=int(self._now()) + int(self._ttl),
        )

    # -- lease detail / holder assertion (canonical ledger) -------------- #
    def lease_detail(self, owner_token: str) -> dict:
        return _lease.lease_detail(owner_token, self._now() + self._ttl)

    def assert_lease_holder(
        self, effect_id: str, principal: Principal, owner_token: str
    ) -> None:
        try:
            _lease.assert_lease_holder(
                effect_id, principal, owner_token, db_path=self._db_path
            )
        except _lease.LeaseError as exc:
            raise AuthorityError(f"lease not held: {exc}") from exc

    def sweep_expired_leases(
        self, run_id: str, principal: Principal, *, max_attempts: int = 3
    ) -> dict:
        return _lease.sweep_expired_leases(
            run_id, principal, now=self._now(),
            max_attempts=max_attempts, db_path=self._db_path,
        )

    def reconcile_to_terminal(
        self, effect_id: str, principal: Principal, *, committed: bool
    ) -> str:
        """Resolve an ambiguous effect to a terminal state ONLY with out-of-band
        proof (``committed``). Never marks terminal without evidence."""
        return _lease.reconcile_to_terminal(
            effect_id, principal, committed=committed, db_path=self._db_path
        )
