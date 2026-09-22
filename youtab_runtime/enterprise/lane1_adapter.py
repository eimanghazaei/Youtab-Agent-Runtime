"""Concrete Lane-1 authority adapter for the Lane-2 governed connector.

Binds the governed enterprise connector to the CURRENT Lane-1 authority model
(the reshape at Lane-1 delivery ``ca89219a``): the Runtime NEVER mints, approves
or self-authorizes an effect. Authority is an externally-signed
:class:`~youtab_runtime.effect_authorization.EffectAuthorization` (Ed25519, from
the Brain / a Simorgh authorization issuer). The Runtime only:

  * ``canonicalize_workspace`` — canonical tenant-scoped workspace;
  * ``consume_authorization`` — verify the signed authorization against the
    trusted keyring (test keys refused in production) and its tenant / principal
    / workspace / capability / effect-digest binding, then **single-use consume**
    it via Lane-1 ``approval.reserve_and_consume_authorization`` (durable
    consumed-authorization ledger);
  * ``acquire_worker_lease`` / ``lease_detail`` / ``assert_lease_holder`` /
    ``sweep_expired_leases`` / ``reconcile_to_terminal`` — the canonical
    worker-lease + evidence-reconcile spine.

There is **no** ``issue_approval`` here and no Runtime-minted approval anywhere:
that API is gone from Lane-1 and must never return. ``is_reference = False``; the
connector accepts only this adapter on the production path.

Lane-1 owns its files; this adapter only *calls* their public APIs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Mapping, Optional

from youtab_runtime import approval as _approval
from youtab_runtime import worker_lease as _lease
from youtab_runtime.effect_authorization import EffectAuthorization
from youtab_runtime.enterprise.authority import (
    AuthorityError,
    CanonicalWorkspace,
    IdempotencyReservation,
    WorkerLease,
)
from youtab_runtime.run_journal import Principal

__all__ = ["Lane1AuthorityAdapter", "ENTERPRISE_EFFECT_OPERATION"]

#: Enterprise commit operations are effectful writes; the signed authorization's
#: ``operation`` Literal (read|write|create) is bound into the effect digest.
ENTERPRISE_EFFECT_OPERATION = "write"


def _target_token(capability_id: str, request_digest: str) -> str:
    """A canonical, non-filesystem target token for an enterprise effect.

    Stands in for ``safe_path`` in ``approval.compute_effect_digest`` so the same
    binding function (operation + target + workspace + content) yields the effect
    digest the signed authorization must carry.
    """
    return f"enterprise:{capability_id}#{request_digest}"


class Lane1AuthorityAdapter:
    """Real Lane-1-backed authority (signed EffectAuthorization model)."""

    is_reference = False

    def __init__(
        self,
        *,
        production: bool,
        authority_public_keys: Optional[Mapping[str, str]] = None,
        test_authority_keys: Optional[Mapping[str, str]] = None,
        lease_ttl_seconds: float = 300.0,
        db_path=None,
        clock=None,
    ) -> None:
        import time as _time

        self._production = production
        self._prod_keys = (
            dict(authority_public_keys) if authority_public_keys is not None else None
        )
        self._test_keys = dict(test_authority_keys or {})
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

    # -- signed-authorization binding ------------------------------------ #
    def effect_digest_for(
        self, capability_id: str, workspace_canonical: str, request_digest: str
    ) -> str:
        """The effect digest a signed authorization for this effect must carry."""
        return _approval.compute_effect_digest(
            ENTERPRISE_EFFECT_OPERATION,
            _target_token(capability_id, request_digest),
            workspace_canonical,
            request_digest,
        )

    def consume_authorization(
        self,
        authorization: EffectAuthorization,
        *,
        capability_id: str,
        operation_id: str,
        request_digest: str,
        principal: Principal,
        workspace: CanonicalWorkspace,
    ) -> str:
        """Verify + single-use consume a signed authorization, or fail closed.

        The Runtime never mints: it resolves the trusted key (a test key is
        refused in production), verifies the Ed25519 signature + expiry, checks
        the tenant / principal / workspace / capability binding + effect digest,
        and atomically consumes the single-use ``authorization_id`` in Lane-1's
        durable ledger. Returns the consumed authorization id.
        """
        if not isinstance(authorization, EffectAuthorization):
            raise AuthorityError("authorization must be a signed EffectAuthorization")
        if authorization.capability != capability_id:
            raise AuthorityError("authorization not bound to this capability")
        expected = self.effect_digest_for(capability_id, workspace.workspace, request_digest)
        try:
            _approval.reserve_and_consume_authorization(
                authorization, expected, principal, workspace.workspace,
                production=self._production, now=self._now(),
                production_keys=self._prod_keys, test_keys=self._test_keys,
                db_path=self._db_path,
            )
        except _approval.ApprovalError as exc:
            raise AuthorityError(f"authorization refused: {type(exc).__name__}") from exc
        except Exception as exc:  # noqa: BLE001 - EffectAuthorizationError family
            raise AuthorityError(f"authorization refused: {type(exc).__name__}") from exc
        return authorization.authorization_id

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
        self, *, operation_id: str, workspace: CanonicalWorkspace, authorization_id: str
    ) -> WorkerLease:
        token = hmac.new(
            os.urandom(16),
            f"{operation_id}:{workspace.workspace}:{authorization_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        return WorkerLease(
            lease_token=token, operation_id=operation_id,
            workspace=workspace.workspace,
            expires_at=int(self._now()) + int(self._ttl),
        )

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
        self,
        effect_id: str,
        principal: Principal,
        *,
        operation_digest: str,
        workspace_id: str,
        outcome: str,
        result_digest: str,
        provenance: str,
    ) -> str:
        """Resolve an ambiguous effect terminal ONLY with verifiable evidence.

        Builds a Lane-1 :class:`EffectEvidence` bound to the effect's identity;
        Lane-1 refuses forged (wrong effect/workspace), mismatched (wrong
        operation digest) or stale evidence, so the Runtime can never mark an
        effect terminal without provider/reference proof.
        """
        evidence = _lease.EffectEvidence(
            effect_id=effect_id, operation_digest=operation_digest,
            workspace_id=workspace_id, outcome=outcome,
            result_digest=result_digest, provenance=provenance,
            verified_at=self._now(),
        )
        return _lease.reconcile_to_terminal(
            effect_id, principal, evidence, workspace_id=workspace_id,
            now=self._now(), db_path=self._db_path,
        )
