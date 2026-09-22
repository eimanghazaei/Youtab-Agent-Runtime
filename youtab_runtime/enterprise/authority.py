"""Typed Lane-1 authority boundary + Runtime idempotency policy (Lane-2 side).

Lane 2 must not edit Lane-1-owned files or reach into private symbols. It
integrates with the Lane-1 authority spine through the typed :class:`AuthorityBoundary`
Protocol here. Until Lane-1 delivers a concrete implementation (IR-2), the
connector uses :class:`UnavailableAuthorityBoundary`, which refuses every call
fail-closed, so any governed operation is reported IMPLEMENTED_NOT_VERIFIED.

:class:`ReferenceAuthorityBoundary` is an explicit **test/reference double**. It
carries ``is_reference = True`` and refuses to be constructed with
``production=True``; the connector additionally refuses any reference boundary
when it is itself in production mode, so a test authority can never be selected
in production.

:class:`RuntimeIdempotencyPolicy` decides provider idempotency from the *verified
manifest* (declared support + semantics), an explicit **Runtime provider
allowlist**, and the request — never from the private effect-ledger symbol
``_PROVIDER_IDEMPOTENCY_SUPPORTED`` and never from a hardcoded unrelated provider
such as ``billing``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from youtab_runtime.enterprise.manifest import (
    Capability,
    IdempotencySemantics,
    canonical_json,
)

__all__ = [
    "AuthorityError",
    "CanonicalWorkspace",
    "ApprovalGrant",
    "IdempotencyReservation",
    "WorkerLease",
    "AuthorityBoundary",
    "UnavailableAuthorityBoundary",
    "ReferenceAuthorityBoundary",
    "RuntimeIdempotencyPolicy",
]


class AuthorityError(RuntimeError):
    """A fail-closed authority-boundary refusal."""


@dataclass(frozen=True)
class CanonicalWorkspace:
    tenant: str
    workspace: str


@dataclass(frozen=True)
class ApprovalGrant:
    approval_id: str
    operation_id: str
    request_digest: str
    delegation: str
    issuer: str
    expires_at: int


@dataclass(frozen=True)
class IdempotencyReservation:
    reservation_id: str
    key: str


@dataclass(frozen=True)
class WorkerLease:
    lease_token: str
    operation_id: str
    workspace: str
    expires_at: int


@runtime_checkable
class AuthorityBoundary(Protocol):
    """The typed Lane-1 authority contract the connector depends on (IR-2)."""

    is_reference: bool

    def canonicalize_workspace(
        self, tenant: str, raw_workspace: str
    ) -> CanonicalWorkspace: ...

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
    ) -> ApprovalGrant: ...

    def reserve_idempotency(
        self, *, effect_key: str, workspace: CanonicalWorkspace
    ) -> IdempotencyReservation: ...

    def acquire_worker_lease(
        self,
        *,
        operation_id: str,
        workspace: CanonicalWorkspace,
        approval_id: str,
    ) -> WorkerLease: ...


class UnavailableAuthorityBoundary:
    """Fail-closed default: refuses every call until Lane-1 integrates (IR-2)."""

    is_reference = False

    _MSG = "Lane-1 authority boundary not integrated (IR-2) — refusing"

    def canonicalize_workspace(self, tenant, raw_workspace):
        raise AuthorityError(self._MSG)

    def verify_approval(self, **_):
        raise AuthorityError(self._MSG)

    def reserve_idempotency(self, **_):
        raise AuthorityError(self._MSG)

    def acquire_worker_lease(self, **_):
        raise AuthorityError(self._MSG)


@dataclass
class _RegisteredApproval:
    operation_id: str
    request_digest: str
    delegation: str
    issuer: str
    expires_at: int
    tenant: str
    workspace: str


class ReferenceAuthorityBoundary:
    """An explicit test/reference authority double — never valid in production.

    Approvals must be pre-registered (bound to operation id + request digest +
    workspace), so tests prove the connector honours approval binding rather than
    trusting any client-supplied authority.
    """

    is_reference = True

    def __init__(self, *, production: bool = False, clock=None, secret: bytes = b"ref-auth") -> None:
        if production:
            raise AuthorityError("ReferenceAuthorityBoundary cannot run in production")
        import time as _time

        self._now = clock or _time.time
        self._secret = secret
        self._approvals: dict[str, _RegisteredApproval] = {}

    # -- test setup ------------------------------------------------------- #
    def register_approval(
        self,
        approval_id: str,
        *,
        operation_id: str,
        request_digest: str,
        tenant: str,
        workspace_canonical: str,
        delegation: str = "direct",
        issuer: str = "reference-approver",
        ttl: int = 3600,
    ) -> None:
        self._approvals[approval_id] = _RegisteredApproval(
            operation_id=operation_id,
            request_digest=request_digest,
            delegation=delegation,
            issuer=issuer,
            expires_at=int(self._now()) + ttl,
            tenant=tenant,
            workspace=workspace_canonical,
        )

    # -- AuthorityBoundary ------------------------------------------------ #
    def canonicalize_workspace(self, tenant, raw_workspace) -> CanonicalWorkspace:
        if not isinstance(tenant, str) or not tenant.strip():
            raise AuthorityError("tenant must be non-empty")
        if not isinstance(raw_workspace, str) or not raw_workspace.strip():
            raise AuthorityError("workspace must be non-empty")
        return CanonicalWorkspace(
            tenant=tenant.strip(),
            workspace=f"{tenant.strip()}/{raw_workspace.strip().lower()}",
        )

    def verify_approval(
        self,
        *,
        approval_id,
        operation_id,
        request_digest,
        delegation,
        principal_tenant,
        principal_user,
        workspace,
    ) -> ApprovalGrant:
        reg = self._approvals.get(approval_id)
        if reg is None:
            raise AuthorityError("unknown approval id")
        if reg.operation_id != operation_id:
            raise AuthorityError("approval not bound to this operation")
        if reg.request_digest != request_digest:
            raise AuthorityError("approval not bound to this request digest")
        if reg.tenant != principal_tenant:
            raise AuthorityError("approval tenant mismatch")
        if reg.workspace != workspace.workspace:
            raise AuthorityError("approval workspace mismatch")
        if reg.delegation != delegation:
            raise AuthorityError("approval delegation mismatch")
        if int(self._now()) >= reg.expires_at:
            raise AuthorityError("approval expired")
        return ApprovalGrant(
            approval_id=approval_id,
            operation_id=operation_id,
            request_digest=request_digest,
            delegation=delegation,
            issuer=reg.issuer,
            expires_at=reg.expires_at,
        )

    def reserve_idempotency(self, *, effect_key, workspace) -> IdempotencyReservation:
        rid = hmac.new(
            self._secret, f"{workspace.workspace}:{effect_key}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        return IdempotencyReservation(reservation_id=rid, key=effect_key)

    def acquire_worker_lease(self, *, operation_id, workspace, approval_id) -> WorkerLease:
        token = hmac.new(
            self._secret,
            f"lease:{operation_id}:{workspace.workspace}:{approval_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        return WorkerLease(
            lease_token=token,
            operation_id=operation_id,
            workspace=workspace.workspace,
            expires_at=int(self._now()) + 300,
        )


class RuntimeIdempotencyPolicy:
    """Runtime-owned provider idempotency decision — no ledger-private coupling.

    A provider idempotency key is derived ONLY when the verified capability
    declares idempotency support with non-``none`` semantics AND the provider is
    in the Runtime's explicit ``allowed_external_providers`` allowlist. Reference
    providers are (deliberately) not in that allowlist: their idempotency is
    enforced in-runtime by the effect ledger's ``effect_id``/``try_claim``, and
    no external exactly-once is ever implied.
    """

    def __init__(self, allowed_external_providers: set[str]) -> None:
        self._allowed = set(allowed_external_providers)

    def provider_accepts_key(self, provider: str) -> bool:
        return provider in self._allowed

    def derive_provider_key(
        self,
        capability: Capability,
        *,
        request_digest: str,
        workspace_canonical: str,
        approval_id: str,
    ) -> Optional[str]:
        if not capability.idempotency_supported:
            return None
        if capability.idempotency_semantics is IdempotencySemantics.NONE:
            return None
        if capability.provider not in self._allowed:
            return None
        material = canonical_json(
            [
                capability.provider,
                capability.capability_id,
                capability.capability_version,
                capability.operation_id,
                request_digest,
                workspace_canonical,
                approval_id,
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
