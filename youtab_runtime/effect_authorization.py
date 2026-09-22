"""Signed authority for a single filesystem effect.

The Runtime must NOT mint, self-approve, or silently promote a request into an
approved operation. Authority to perform an effect is a signed statement from an
external authority (the Brain / an effect-authorization issuer): Ed25519, keyed
by ``key_id``, and the engine holds only the public keyring — it can never forge
one. The Runtime may only *verify*, *reserve* and *consume* such authority.

``EffectAuthorization`` binds, and the signature covers:
  issuer, key_id, tenant, principal (user), canonical workspace, the grant it
  descends from (command_id), capability + operation, the normalized effect
  digest (operation + canonical path + workspace + content digest), expiry, and
  a single-use authorization id (nonce).

For deterministic tests, :class:`TestEffectAuthority` mints signed authorizations
with a key id registered ONLY as test material; such a key is refused in
production (:func:`resolve_authority_public_key`).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Literal, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from youtab_runtime.managed_execution import load_brain_public_keys

__all__ = [
    "EffectAuthorizationError",
    "UntrustedIssuerError",
    "TestAuthorityInProductionError",
    "InvalidAuthorizationSignatureError",
    "EffectAuthorization",
    "resolve_authority_public_key",
    "TestEffectAuthority",
    "TEST_AUTHORITY_KEY_PREFIX",
]

#: Any key id with this prefix is test-only material and is refused in production.
TEST_AUTHORITY_KEY_PREFIX = "test-authority:"


class EffectAuthorizationError(Exception):
    """Fail-closed base."""


class UntrustedIssuerError(EffectAuthorizationError):
    pass


class TestAuthorityInProductionError(EffectAuthorizationError):
    __test__ = False  # not a pytest test class despite the name
    pass


class InvalidAuthorizationSignatureError(EffectAuthorizationError):
    pass


class EffectAuthorization(BaseModel):
    """A signed authorization for exactly one filesystem effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.effect-authorization.v1"] = "youtab.effect-authorization.v1"
    issuer: str = Field(min_length=1, max_length=128)
    audience: Literal["youtab-agent-runtime"] = "youtab-agent-runtime"
    authorization_id: str = Field(min_length=16, max_length=256)  # single-use nonce
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=8, max_length=128)  # grant this descends from
    capability: str = Field(min_length=1, max_length=128)
    operation: Literal["read", "write", "create"]
    effect_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime
    key_id: str = Field(min_length=3, max_length=128)
    signature: str = Field(min_length=40, max_length=256)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("authorization times must be timezone-aware")
        return value.astimezone(UTC)

    def canonical_payload(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def verify(self, public_key_b64: str, *, now: Optional[datetime] = None) -> None:
        """Verify expiry + Ed25519 signature. Raises on any failure (fail closed)."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if current < self.issued_at:
            raise InvalidAuthorizationSignatureError("authorization is not yet valid")
        if current >= self.expires_at:
            raise InvalidAuthorizationSignatureError("authorization expired")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(public_key_b64, validate=True)
            )
            public_key.verify(
                base64.b64decode(self.signature, validate=True), self.canonical_payload()
            )
        except (ValueError, InvalidSignature) as exc:
            raise InvalidAuthorizationSignatureError("invalid authorization signature") from exc


def resolve_authority_public_key(
    key_id: str,
    *,
    production: bool,
    production_keys: Optional[Mapping[str, str]] = None,
    test_keys: Optional[Mapping[str, str]] = None,
) -> str:
    """Return the trusted public key for ``key_id`` or fail closed.

    In production, only ``production_keys`` (the Brain keyring) are trusted and a
    test-only key id is refused outright. Outside production, test keys are also
    accepted so deterministic tests can mint signed authorizations.
    """
    prod = dict(production_keys if production_keys is not None else load_brain_public_keys())
    is_test_key = key_id.startswith(TEST_AUTHORITY_KEY_PREFIX)
    if production:
        if is_test_key:
            raise TestAuthorityInProductionError("test authority is not permitted in production")
        pub = prod.get(key_id)
        if not pub:
            raise UntrustedIssuerError("authorization key id is not trusted")
        return pub
    # non-production: production keys first, then explicit test keys
    pub = prod.get(key_id) or (dict(test_keys or {})).get(key_id)
    if not pub:
        raise UntrustedIssuerError("authorization key id is not trusted")
    return pub


class TestEffectAuthority:
    """Deterministic test-only issuer. Its ``key_id`` carries the test prefix so
    it can never be selected in production."""

    __test__ = False  # not a pytest test class despite the name

    def __init__(self, key_id: str = TEST_AUTHORITY_KEY_PREFIX + "local", issuer: str = "test-authority"):
        if not key_id.startswith(TEST_AUTHORITY_KEY_PREFIX):
            raise ValueError("test authority key id must carry the test prefix")
        self.key_id = key_id
        self.issuer = issuer
        self._sk = Ed25519PrivateKey.generate()

    def public_key_b64(self) -> str:
        from cryptography.hazmat.primitives import serialization

        raw = self._sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")

    def keyring(self) -> dict[str, str]:
        return {self.key_id: self.public_key_b64()}

    def mint(
        self,
        *,
        authorization_id: str,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        command_id: str,
        capability: str,
        operation: str,
        effect_digest: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> EffectAuthorization:
        unsigned = EffectAuthorization(
            issuer=self.issuer, authorization_id=authorization_id,
            tenant_id=tenant_id, user_id=user_id, workspace_id=workspace_id,
            command_id=command_id, capability=capability, operation=operation,
            effect_digest=effect_digest, issued_at=issued_at, expires_at=expires_at,
            key_id=self.key_id, signature="0" * 64,
        )
        sig = self._sk.sign(unsigned.canonical_payload())
        return unsigned.model_copy(update={"signature": base64.b64encode(sig).decode("ascii")})
