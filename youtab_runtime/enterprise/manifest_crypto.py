"""Asymmetric (Ed25519) signed enterprise capability manifest (Runtime side).

This is the production-grade counterpart to the HMAC-based manifest in
:mod:`youtab_runtime.enterprise.manifest`. A real Gateway/KMS signs the
capability manifest with an Ed25519 *private* key; the Runtime holds only the
matching *public* keys and verifies the manifest fail-closed. Nothing here
implements custom crypto — signing and verification are delegated entirely to
the locked ``cryptography`` library.

The verified result is a :class:`manifest.CapabilityManifest` of
:class:`manifest.Capability`, byte-for-byte the same typed object the HMAC path
returns, so the enterprise connector consumes either path identically.

Security contract (envelope v2). The manifest carries:

  * an **algorithm** tag that MUST be ``"ed25519"`` (any other value, including a
    downgrade to ``"hmac-sha256"`` or ``"none"``, is rejected before any
    signature is checked — no algorithm confusion);
  * an **issuer** identity and **key id** resolved against a public-key registry;
  * **issued-at** / **expires-at** timestamps;
  * a **tenant scope** and **workspace scope** the caller may pin;
  * a **manifest digest** over its capabilities;
  * an envelope **signature** (Ed25519).

Each capability additionally carries a per-capability Ed25519 **signature** that
binds its id, version, operation id and both schema digests, so a signature
copied onto another capability or version fails.

:class:`CryptoManifestVerifier` rejects, fail-closed: an unsigned manifest, an
unknown issuer/key id, a **revoked** key id, an expired or not-yet-valid
manifest, any modified field/schema/digest, a per-capability signature copied to
another capability/version, an algorithm-confusion/downgrade attempt, a
tenant/workspace scope mismatch against a caller-supplied expectation, and (in
production) a manifest signed by the reserved test-only issuer.
"""

from __future__ import annotations

import base64
import binascii
import time as _time
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from youtab_runtime.enterprise import manifest as _m
from youtab_runtime.enterprise.manifest import (
    MANIFEST_SCHEMA_VERSION,
    TEST_ONLY_ISSUER,
    AllowlistPolicy,
    Capability,
    CapabilityManifest,
    IdempotencySemantics,
    ManifestError,
    Provenance,
    RiskClass,
    TypedSchema,
    canonical_json,
    digest_of,
)

__all__ = [
    "MANIFEST_ALGORITHM",
    "PublicKeyRegistry",
    "Ed25519ManifestSigner",
    "CryptoManifestVerifier",
]

#: The only signature algorithm this verifier accepts. Presenting any other
#: value is treated as an algorithm-confusion / downgrade attempt.
MANIFEST_ALGORITHM = "ed25519"


def _sig_message(payload: Mapping[str, Any]) -> bytes:
    """Deterministic byte string an Ed25519 signature is computed over."""
    return canonical_json(payload).encode("utf-8")


def _decode_signature(raw: Any) -> bytes:
    """Decode a hex or base64 signature string to raw bytes, fail-closed."""
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError("signature must be a non-empty string")
    try:
        return bytes.fromhex(raw)
    except (ValueError, binascii.Error):
        pass
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        raise ManifestError("signature is not valid hex or base64") from None


class PublicKeyRegistry:
    """Issuer -> key-id -> Ed25519 **public** key. Holds no private material.

    A key id may be marked *revoked*; a revoked key id is rejected even while its
    public key is still present, and even if the same issuer holds other,
    non-revoked key ids (key rotation). Unknown issuer/key ids are fail-closed.
    """

    def __init__(
        self,
        keys: Mapping[str, Mapping[str, bytes]],
        *,
        revoked: Optional[Iterable[tuple[str, str]]] = None,
    ) -> None:
        self._keys: dict[str, dict[str, bytes]] = {
            issuer: {kid: bytes(pub) for kid, pub in kmap.items()}
            for issuer, kmap in keys.items()
        }
        self._revoked: set[tuple[str, str]] = set(revoked or ())

    def revoke(self, issuer: str, key_id: str) -> None:
        """Revoke a key id. Idempotent; does not remove the public bytes."""
        self._revoked.add((issuer, key_id))

    def is_revoked(self, issuer: str, key_id: str) -> bool:
        return (issuer, key_id) in self._revoked

    def has(self, issuer: str, key_id: str) -> bool:
        """True only if the key id is present AND not revoked."""
        if (issuer, key_id) in self._revoked:
            return False
        return key_id in self._keys.get(issuer, {})

    def public_key(self, issuer: str, key_id: str) -> Ed25519PublicKey:
        if (issuer, key_id) in self._revoked:
            raise ManifestError("revoked key id")
        try:
            raw = self._keys[issuer][key_id]
        except KeyError:
            raise ManifestError("unknown issuer or key id") from None
        try:
            return Ed25519PublicKey.from_public_bytes(raw)
        except (ValueError, TypeError):
            raise ManifestError("malformed public key material") from None


class Ed25519ManifestSigner:
    """TEST-ONLY signer that holds an Ed25519 private key.

    Real deployments sign in the Gateway/KMS; this class exists purely so tests
    can produce genuine Ed25519-signed manifests. It cannot be used in
    production: pass ``issuer=TEST_ONLY_ISSUER`` for the reserved test issuer a
    production verifier refuses outright. Keeping this signer separate from
    :class:`PublicKeyRegistry` also guarantees no private key ever lives in the
    registry the verifier trusts.
    """

    def __init__(
        self, private_key: Ed25519PrivateKey, issuer: str, key_id: str
    ) -> None:
        self._private_key = private_key
        self._issuer = issuer
        self._key_id = key_id

    @classmethod
    def generate(cls, issuer: str, key_id: str) -> "Ed25519ManifestSigner":
        return cls(Ed25519PrivateKey.generate(), issuer, key_id)

    def public_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def _sign(self, payload: Mapping[str, Any]) -> str:
        return self._private_key.sign(_sig_message(payload)).hex()

    def sign(
        self,
        capabilities: list[Mapping[str, Any]],
        *,
        issued_at: int,
        expires_at: int,
        tenant_scope: str,
        workspace_scope: str,
    ) -> dict:
        signed_caps: list[dict] = []
        for raw in capabilities:
            payload = _m._signed_capability_payload(raw)
            cap = dict(payload)
            cap["signature"] = self._sign(payload)
            signed_caps.append(cap)
        cap_digests = sorted(digest_of(c) for c in signed_caps)
        envelope = {
            "algorithm": MANIFEST_ALGORITHM,
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "issuer": self._issuer,
            "key_id": self._key_id,
            "issued_at": int(issued_at),
            "expires_at": int(expires_at),
            "tenant_scope": tenant_scope,
            "workspace_scope": workspace_scope,
            "manifest_digest": digest_of(cap_digests),
        }
        signed = dict(envelope)
        signed["signature"] = self._sign(envelope)
        signed["capabilities"] = signed_caps
        return signed


class CryptoManifestVerifier:
    """Verifies an Ed25519-signed manifest fail-closed and returns typed caps."""

    def __init__(
        self,
        registry: PublicKeyRegistry,
        allowlist: AllowlistPolicy,
        *,
        production: bool = True,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._registry = registry
        self._allowlist = allowlist
        self._production = production
        self._now: Callable[[], float] = clock or _time.time

    def _verify_sig(
        self, public_key: Ed25519PublicKey, signature: Any, payload: Mapping[str, Any]
    ) -> None:
        sig = _decode_signature(signature)
        try:
            public_key.verify(sig, _sig_message(payload))
        except InvalidSignature:
            raise ManifestError("signature invalid") from None

    def verify(
        self,
        raw: Any,
        *,
        expected_tenant: Optional[str] = None,
        expected_workspace: Optional[str] = None,
    ) -> CapabilityManifest:
        if not isinstance(raw, dict):
            raise ManifestError("manifest must be a mapping")
        for field in (
            "algorithm", "schema_version", "issuer", "key_id", "issued_at",
            "expires_at", "tenant_scope", "workspace_scope", "manifest_digest",
            "signature", "capabilities",
        ):
            if field not in raw:
                raise ManifestError(f"manifest missing '{field}'")

        # Algorithm confusion / downgrade: reject BEFORE any signature work.
        if raw["algorithm"] != MANIFEST_ALGORITHM:
            raise ManifestError("unsupported or downgraded signature algorithm")

        if raw["schema_version"] != MANIFEST_SCHEMA_VERSION:
            raise ManifestError("unsupported manifest schema version")

        issuer = _require_str(raw["issuer"], "issuer")
        key_id = _require_str(raw["key_id"], "key_id")
        if self._production and issuer == TEST_ONLY_ISSUER:
            raise ManifestError("test-only issuer refused in production")

        # Resolves unknown OR revoked key ids fail-closed.
        public_key = self._registry.public_key(issuer, key_id)

        tenant_scope = _require_str(raw["tenant_scope"], "tenant_scope")
        workspace_scope = _require_str(raw["workspace_scope"], "workspace_scope")
        if expected_tenant is not None and tenant_scope != expected_tenant:
            raise ManifestError("tenant scope mismatch")
        if expected_workspace is not None and workspace_scope != expected_workspace:
            raise ManifestError("workspace scope mismatch")

        issued_at, expires_at = raw["issued_at"], raw["expires_at"]
        if not isinstance(issued_at, int) or isinstance(issued_at, bool):
            raise ManifestError("issued_at must be an integer")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise ManifestError("expires_at must be an integer")
        if expires_at <= issued_at:
            raise ManifestError("expires_at must be after issued_at")
        now = int(self._now())
        if now < issued_at:
            raise ManifestError("manifest not yet valid")
        if now >= expires_at:
            raise ManifestError("manifest expired")

        entries = raw["capabilities"]
        if not isinstance(entries, list) or not entries:
            raise ManifestError("capabilities must be a non-empty list")

        verified: dict[str, Capability] = {}
        recomputed_digests: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict) or "signature" not in entry:
                raise ManifestError("capability missing signature")
            payload = _m._signed_capability_payload(entry)
            # Per-capability signature binds id+version+operation+schema digests,
            # so ANY tampered signed field/schema, or a signature copied from
            # another capability/version, fails this Ed25519 verification.
            self._verify_sig(public_key, entry["signature"], payload)
            for dk in ("request_schema_digest", "response_schema_digest"):
                if dk in entry and entry[dk] != payload[dk]:
                    raise ManifestError(f"capability {dk} tampered")
            cap_id = payload["capability_id"]
            cap_ver = payload["capability_version"]
            if not self._allowlist.is_capability_allowed(cap_id, cap_ver):
                raise ManifestError(f"capability {cap_id}@{cap_ver} not allowlisted")
            if not self._allowlist.is_provider_allowed(payload["provider"]):
                raise ManifestError(
                    f"provider {payload['provider']!r} not allowlisted"
                )
            cap = Capability(
                capability_id=cap_id,
                capability_version=cap_ver,
                operation_id=payload["operation_id"],
                request_schema=TypedSchema.from_spec(
                    payload["request_schema"], "request_schema"
                ),
                response_schema=TypedSchema.from_spec(
                    payload["response_schema"], "response_schema"
                ),
                tenant_scope=payload["tenant_scope"],
                workspace_scope=payload["workspace_scope"],
                risk_class=RiskClass(payload["risk_class"]),
                approval_required=payload["approval_required"],
                idempotency_supported=payload["idempotency"]["supported"],
                idempotency_semantics=IdempotencySemantics(
                    payload["idempotency"]["semantics"]
                ),
                receipt_supported=payload["receipt_supported"],
                reconciliation_supported=payload["reconciliation_supported"],
                provenance=Provenance(payload["provenance"]),
                provider=payload["provider"],
            )
            if cap_id in verified:
                raise ManifestError(f"duplicate capability id {cap_id!r}")
            verified[cap_id] = cap
            recomputed_digests.append(digest_of(entry))

        # Envelope binds the exact set of capabilities and every envelope field,
        # so adding/removing/reordering a capability or editing scope/timestamps
        # after signing breaks verification.
        if raw["manifest_digest"] != digest_of(sorted(recomputed_digests)):
            raise ManifestError("manifest_digest does not match capabilities")
        envelope = {
            "algorithm": MANIFEST_ALGORITHM,
            "schema_version": raw["schema_version"],
            "issuer": issuer,
            "key_id": key_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "tenant_scope": tenant_scope,
            "workspace_scope": workspace_scope,
            "manifest_digest": raw["manifest_digest"],
        }
        self._verify_sig(public_key, raw["signature"], envelope)

        return CapabilityManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            issuer=issuer,
            key_id=key_id,
            issued_at=issued_at,
            expires_at=expires_at,
            capabilities=MappingProxyType(verified),
        )


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    return value
