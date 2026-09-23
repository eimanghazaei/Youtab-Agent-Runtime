"""Signed, typed enterprise capability manifest (Runtime side, vendor-neutral).

The Gateway issues a *signed* capability manifest; the Runtime verifies it and
executes enterprise operations (CRM/ERP/SAP/CAD) through one generic boundary.
The Runtime imports no vendor SDK — a capability carries only typed metadata.

Security contract (Review v1.0 item 2). Each manifest carries:

  * a **manifest schema version**;
  * an **issuer** identity and **key id**;
  * **issued-at** / **expires-at** timestamps;
  * a **manifest digest** over its capabilities;
  * a cryptographic **signature** (envelope-level).

Each capability carries:

  * **capability id** and **capability version**;
  * a typed **operation id** (e.g. ``crm.contact.update.commit``);
  * **request/response schema** + their **digests**;
  * **tenant/workspace scope rules**;
  * a **risk class**;
  * an **approval requirement**;
  * **idempotency** support + semantics;
  * **receipt/reconciliation** support;
  * **LIVE|REFERENCE provenance**;
  * a per-capability cryptographic **signature** binding all of the above.

:class:`ManifestVerifier` rejects, fail-closed: an unsigned manifest, an unknown
issuer/key id, any modified field/schema/digest, an expired or not-yet-valid
manifest, an unallowlisted capability/version or provider, a signature copied to
another capability or version, and (in production) a manifest signed by the
explicit test-only signer. **Server-side allowlist status** and issuer trust are
decided here, never taken from a client field.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

__all__ = [
    "ManifestError",
    "ManifestSchemaVersion",
    "Provenance",
    "RiskClass",
    "IdempotencySemantics",
    "TypedSchema",
    "Capability",
    "CapabilityManifest",
    "KeyRegistry",
    "AllowlistPolicy",
    "ManifestSigner",
    "ManifestVerifier",
    "TEST_ONLY_ISSUER",
    "canonical_json",
    "digest_of",
]

MANIFEST_SCHEMA_VERSION = "youtab.enterprise.manifest/v1"

#: The single issuer id reserved for the in-repo test signer. A production
#: verifier refuses it outright, so a test manifest can never be selected in
#: production even if its key somehow leaked into the registry.
TEST_ONLY_ISSUER = "youtab-test-signer/DO-NOT-USE-IN-PRODUCTION"

_ALLOWED_FIELD_TYPES: frozenset[str] = frozenset(
    {"str", "int", "float", "bool", "dict", "list"}
)
_TYPE_TOKEN_TO_PY: Mapping[str, tuple[type, ...]] = MappingProxyType(
    {
        "str": (str,),
        "int": (int,),
        "float": (int, float),
        "bool": (bool,),
        "dict": (dict,),
        "list": (list,),
    }
)


class ManifestError(ValueError):
    """A fail-closed manifest contract or signature-verification failure."""


class ManifestSchemaVersion(str, Enum):
    V1 = MANIFEST_SCHEMA_VERSION


class Provenance(str, Enum):
    LIVE = "LIVE"
    REFERENCE = "REFERENCE"

    @classmethod
    def parse(cls, raw: Any) -> "Provenance":
        try:
            return cls(raw)
        except ValueError:
            raise ManifestError(f"invalid provenance {raw!r}") from None


class RiskClass(str, Enum):
    READ = "read"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def parse(cls, raw: Any) -> "RiskClass":
        try:
            return cls(raw)
        except ValueError:
            raise ManifestError(f"invalid risk_class {raw!r}") from None


class IdempotencySemantics(str, Enum):
    NONE = "none"
    AT_LEAST_ONCE = "at_least_once"
    EXACTLY_ONCE = "exactly_once"

    @classmethod
    def parse(cls, raw: Any) -> "IdempotencySemantics":
        try:
            return cls(raw)
        except ValueError:
            raise ManifestError(f"invalid idempotency semantics {raw!r}") from None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=repr,
    )


def digest_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    return value


def _validate_schema_map(raw: Any, where: str) -> Mapping[str, str]:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be a mapping")
    parsed: dict[str, str] = {}
    for name, token in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(f"{where} field name must be a non-empty string")
        if not isinstance(token, str) or token not in _ALLOWED_FIELD_TYPES:
            raise ManifestError(f"{where} field {name!r} has unknown type {token!r}")
        parsed[name] = token
    return MappingProxyType(dict(parsed))


@dataclass(frozen=True)
class TypedSchema:
    """A typed field schema for one direction (request or response)."""

    fields: Mapping[str, str]

    @classmethod
    def from_spec(cls, raw: Any, where: str) -> "TypedSchema":
        return cls(fields=_validate_schema_map(raw, where))

    @property
    def digest(self) -> str:
        return digest_of({"fields": dict(sorted(self.fields.items()))})

    def validate_payload(self, payload: Any, where: str) -> Mapping[str, Any]:
        if not isinstance(payload, dict):
            raise ManifestError(f"{where} must be a mapping")
        declared, present = set(self.fields), set(payload)
        missing = declared - present
        if missing:
            raise ManifestError(f"{where} missing field(s): {sorted(missing)}")
        extra = present - declared
        if extra:
            raise ManifestError(f"{where} unexpected field(s): {sorted(extra)}")
        for name, token in self.fields.items():
            value = payload[name]
            if token != "bool" and isinstance(value, bool):
                raise ManifestError(f"{where} field {name!r} must be {token}, got bool")
            if not isinstance(value, _TYPE_TOKEN_TO_PY[token]):
                raise ManifestError(
                    f"{where} field {name!r} must be {token}, got {type(value).__name__}"
                )
        return payload


@dataclass(frozen=True)
class Capability:
    """One verified enterprise capability."""

    capability_id: str
    capability_version: str
    operation_id: str
    request_schema: TypedSchema
    response_schema: TypedSchema
    tenant_scope: str
    workspace_scope: str
    risk_class: RiskClass
    approval_required: bool
    idempotency_supported: bool
    idempotency_semantics: IdempotencySemantics
    receipt_supported: bool
    reconciliation_supported: bool
    provenance: Provenance
    provider: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.capability_id, self.capability_version)


def _signed_capability_payload(raw: Mapping[str, Any]) -> dict:
    """The exact, order-independent structure a capability signature covers.

    Recomputes the schema digests from the schemas so a tampered digest OR a
    tampered schema both break verification.
    """
    req = TypedSchema.from_spec(raw["request_schema"], "request_schema")
    res = TypedSchema.from_spec(raw["response_schema"], "response_schema")
    idem = raw["idempotency"]
    if not isinstance(idem, dict) or set(idem) != {"supported", "semantics"}:
        raise ManifestError("idempotency must be {supported, semantics}")
    if not isinstance(idem["supported"], bool):
        raise ManifestError("idempotency.supported must be a bool")
    return {
        "capability_id": _require_non_empty_str(raw["capability_id"], "capability_id"),
        "capability_version": _require_non_empty_str(
            raw["capability_version"], "capability_version"
        ),
        "operation_id": _require_non_empty_str(raw["operation_id"], "operation_id"),
        "request_schema": dict(sorted(req.fields.items())),
        "response_schema": dict(sorted(res.fields.items())),
        "request_schema_digest": req.digest,
        "response_schema_digest": res.digest,
        "tenant_scope": _require_non_empty_str(raw["tenant_scope"], "tenant_scope"),
        "workspace_scope": _require_non_empty_str(
            raw["workspace_scope"], "workspace_scope"
        ),
        "risk_class": RiskClass.parse(raw["risk_class"]).value,
        "approval_required": bool(raw["approval_required"]),
        "idempotency": {
            "supported": idem["supported"],
            "semantics": IdempotencySemantics.parse(idem["semantics"]).value,
        },
        "receipt_supported": bool(raw["receipt_supported"]),
        "reconciliation_supported": bool(raw["reconciliation_supported"]),
        "provenance": Provenance.parse(raw["provenance"]).value,
        "provider": _require_non_empty_str(raw["provider"], "provider"),
    }


@dataclass(frozen=True)
class CapabilityManifest:
    """An immutable, verified, id-keyed set of capabilities."""

    schema_version: str
    issuer: str
    key_id: str
    issued_at: int
    expires_at: int
    capabilities: Mapping[str, Capability]

    def get(self, capability_id: str) -> Capability:
        try:
            return self.capabilities[capability_id]
        except KeyError:
            raise ManifestError(f"unknown capability {capability_id!r}") from None

    def __iter__(self):
        return iter(self.capabilities.values())

    def __len__(self) -> int:
        return len(self.capabilities)


class KeyRegistry:
    """Issuer → key-id → shared secret. Unknown issuer/key is fail-closed.

    HMAC-SHA256 is used as a stand-in for the Gateway/KMS signing algorithm (see
    IR-4). A production registry never contains :data:`TEST_ONLY_ISSUER`.
    """

    def __init__(self, keys: Mapping[str, Mapping[str, bytes]]) -> None:
        self._keys = {
            iss: dict(kmap) for iss, kmap in keys.items()
        }

    def secret(self, issuer: str, key_id: str) -> bytes:
        try:
            return self._keys[issuer][key_id]
        except KeyError:
            raise ManifestError("unknown issuer or key id") from None

    def has(self, issuer: str, key_id: str) -> bool:
        return key_id in self._keys.get(issuer, {})


class AllowlistPolicy:
    """Server-side allowlist of (capability_id, version) and providers."""

    def __init__(
        self,
        capabilities: set[tuple[str, str]],
        providers: set[str],
    ) -> None:
        self._caps = set(capabilities)
        self._providers = set(providers)

    def is_capability_allowed(self, capability_id: str, version: str) -> bool:
        return (capability_id, version) in self._caps

    def is_provider_allowed(self, provider: str) -> bool:
        return provider in self._providers


def _sign(secret: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(
        secret, canonical_json(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()


class ManifestSigner:
    """Signs a manifest for tests / a trusted issuer. NOT selectable in prod.

    A real deployment signs in the Gateway/KMS; this class exists so tests have
    an explicit, clearly-labelled signer. Pass ``issuer=TEST_ONLY_ISSUER`` for
    the test-only signer a production verifier refuses.
    """

    def __init__(self, registry: KeyRegistry, issuer: str, key_id: str) -> None:
        self._registry = registry
        self._issuer = issuer
        self._key_id = key_id

    def sign(
        self,
        capabilities: list[Mapping[str, Any]],
        *,
        issued_at: int,
        expires_at: int,
    ) -> dict:
        secret = self._registry.secret(self._issuer, self._key_id)
        signed_caps = []
        for raw in capabilities:
            payload = _signed_capability_payload(raw)
            cap = dict(payload)
            cap["signature"] = _sign(secret, payload)
            signed_caps.append(cap)
        cap_digests = sorted(digest_of(c) for c in signed_caps)
        envelope = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "issuer": self._issuer,
            "key_id": self._key_id,
            "issued_at": int(issued_at),
            "expires_at": int(expires_at),
            "manifest_digest": digest_of(cap_digests),
        }
        manifest = dict(envelope)
        manifest["signature"] = _sign(secret, envelope)
        manifest["capabilities"] = signed_caps
        return manifest


class ManifestVerifier:
    """Verifies a signed manifest fail-closed and returns typed capabilities."""

    def __init__(
        self,
        registry: KeyRegistry,
        allowlist: AllowlistPolicy,
        *,
        production: bool = True,
        clock: Optional[Any] = None,
    ) -> None:
        self._registry = registry
        self._allowlist = allowlist
        self._production = production
        import time as _time

        self._now = clock or _time.time

    def verify(self, raw: Any) -> CapabilityManifest:
        if not isinstance(raw, dict):
            raise ManifestError("manifest must be a mapping")
        for field in (
            "schema_version", "issuer", "key_id", "issued_at", "expires_at",
            "manifest_digest", "signature", "capabilities",
        ):
            if field not in raw:
                raise ManifestError(f"manifest missing '{field}'")
        if raw["schema_version"] != MANIFEST_SCHEMA_VERSION:
            raise ManifestError("unsupported manifest schema version")
        issuer = _require_non_empty_str(raw["issuer"], "issuer")
        key_id = _require_non_empty_str(raw["key_id"], "key_id")
        if self._production and issuer == TEST_ONLY_ISSUER:
            raise ManifestError("test-only issuer refused in production")
        if not self._registry.has(issuer, key_id):
            raise ManifestError("unknown issuer or key id")
        secret = self._registry.secret(issuer, key_id)

        issued_at, expires_at = raw["issued_at"], raw["expires_at"]
        if not isinstance(issued_at, int) or not isinstance(expires_at, int):
            raise ManifestError("issued_at/expires_at must be integers")
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
        recomputed_digests = []
        for entry in entries:
            if not isinstance(entry, dict) or "signature" not in entry:
                raise ManifestError("capability missing signature")
            sig = entry["signature"]
            payload = _signed_capability_payload(entry)
            # Per-capability signature binds id+version+operation+schema digests,
            # so a signature copied onto another capability/version fails here.
            expected = _sign(secret, payload)
            # The signature is over the recomputed payload, so ANY tampered
            # signed field or schema changes payload and breaks this equality
            # (an attacker without the secret cannot forge a matching signature),
            # and a signature copied from another capability/version fails too.
            if not hmac.compare_digest(str(sig), expected):
                raise ManifestError("capability signature invalid")
            # A digest field carried in the entry must match the digest actually
            # recomputed from the schema, so a modified digest is rejected even
            # though the trusted value is always the recomputed one.
            for dk in ("request_schema_digest", "response_schema_digest"):
                if dk in entry and entry[dk] != payload[dk]:
                    raise ManifestError(f"capability {dk} tampered")
            cap_id, cap_ver = payload["capability_id"], payload["capability_version"]
            if not self._allowlist.is_capability_allowed(cap_id, cap_ver):
                raise ManifestError(f"capability {cap_id}@{cap_ver} not allowlisted")
            if not self._allowlist.is_provider_allowed(payload["provider"]):
                raise ManifestError(f"provider {payload['provider']!r} not allowlisted")
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

        # Envelope: manifest_digest must match the capabilities actually present,
        # and the envelope signature must verify — so a capability cannot be
        # added/removed/reordered after signing.
        if raw["manifest_digest"] != digest_of(sorted(recomputed_digests)):
            raise ManifestError("manifest_digest does not match capabilities")
        envelope = {
            "schema_version": raw["schema_version"],
            "issuer": issuer,
            "key_id": key_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "manifest_digest": raw["manifest_digest"],
        }
        if not hmac.compare_digest(str(raw["signature"]), _sign(secret, envelope)):
            raise ManifestError("manifest signature invalid")

        return CapabilityManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            issuer=issuer,
            key_id=key_id,
            issued_at=issued_at,
            expires_at=expires_at,
            capabilities=MappingProxyType(verified),
        )
