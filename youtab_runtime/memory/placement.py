"""Signed scope-placement verification (Runtime consumes only VERIFIED placements).

Implements the Runtime side of `youtab.scope-placement.v1`
(`docs/architecture/gateway_signed_scope_placement.schema.json`). JSON-schema
validation alone is NOT authorization: a placement authorizes scope only after
:class:`PlacementVerifier` checks the algorithm allowlist, issuer, expiry, the
cross-checks against the command envelope, the Ed25519 signature, and replay.

The verifier returns a distinct :class:`VerifiedPlacement` type; only that type
may build a live-authorized :class:`MemoryScope`. Gateway is NOT modified here.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from youtab_runtime.contracts import BrainCommandEnvelope

from .scope import MemoryScope

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"
_ALG_ALLOWLIST = frozenset({"ed25519"})


class PlacementError(RuntimeError):
    """Raised when a placement cannot be verified (fail closed)."""


class SignedScopePlacement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.scope-placement.v1"]
    algorithm: Literal["ed25519"] = "ed25519"
    issuer: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=8, max_length=128)
    trace_id: str = Field(min_length=8, max_length=128)
    tenant_id: str = Field(min_length=3, max_length=128)
    principal_id: str = Field(min_length=3, max_length=128)
    organization_id: str = Field(pattern=_ID)
    workspace_id: str = Field(pattern=_ID)
    agent_id: str = Field(pattern=_ID)
    run_id: str = Field(pattern=_ID)
    purpose: str = Field(default="default", pattern=_ID)
    delegation_authority_ref: str = Field(min_length=1, max_length=256)
    nonce: str = Field(min_length=16, max_length=256)
    issued_at: datetime
    expires_at: datetime
    key_id: str = Field(min_length=3, max_length=128)
    signature: str = Field(min_length=40, max_length=256)

    def canonical_payload(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


class VerifiedPlacement(BaseModel):
    """Only produced by PlacementVerifier.verify — never constructed by callers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: MemoryScope
    verified: Literal[True] = True

    @classmethod
    def _issue(cls, scope: MemoryScope) -> "VerifiedPlacement":
        return cls(scope=scope)


class PlacementVerifier:
    def __init__(
        self,
        public_keys_b64: dict[str, str],
        *,
        expected_issuer: str = "youtab-gateway",
    ) -> None:
        self._keys = public_keys_b64
        self._issuer = expected_issuer
        self._seen: set[tuple[str, str]] = set()

    def verify(
        self,
        placement: SignedScopePlacement,
        *,
        envelope: BrainCommandEnvelope,
        now: datetime | None = None,
    ) -> VerifiedPlacement:
        if placement.algorithm not in _ALG_ALLOWLIST:
            raise PlacementError("algorithm not in allowlist")
        if placement.issuer != self._issuer:
            raise PlacementError("unexpected issuer")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if current < placement.issued_at:
            raise PlacementError("placement not yet valid")
        if current >= placement.expires_at:
            raise PlacementError("placement expired")
        # cross-check against the signed command envelope
        if placement.command_id != envelope.command_id or placement.trace_id != envelope.trace_id:
            raise PlacementError("placement does not match command/trace")
        if placement.tenant_id != envelope.tenant_id or placement.principal_id != envelope.user_id:
            raise PlacementError("placement crosses tenant/principal of the envelope")
        key_b64 = self._keys.get(placement.key_id)
        if key_b64 is None:
            raise PlacementError("unknown key_id")
        try:
            pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64, validate=True))
            pub.verify(base64.b64decode(placement.signature, validate=True), placement.canonical_payload())
        except (ValueError, InvalidSignature) as exc:
            raise PlacementError("invalid placement signature") from exc
        replay_key = (placement.tenant_id, placement.nonce)
        if replay_key in self._seen:
            raise PlacementError("replayed placement nonce")
        self._seen.add(replay_key)
        scope = MemoryScope(
            tenant_id=placement.tenant_id,
            organization_id=placement.organization_id,
            workspace_id=placement.workspace_id,
            principal_id=placement.principal_id,
            agent_id=placement.agent_id,
            run_id=placement.run_id,
            purpose=placement.purpose,
        )
        return VerifiedPlacement._issue(scope)
