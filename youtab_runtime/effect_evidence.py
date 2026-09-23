"""Trusted evidence-verifier boundary for reconciliation (Owner item 7).

Resolving an ambiguous effect (``unknown`` / ``reconciliation_required``) to a
terminal state must NOT be possible by simply asserting matching field values.
Field equality alone is forgeable: any caller can hand-craft an object whose
``effect_id`` / ``operation_digest`` / ``workspace_id`` line up with the effect
and thereby drive it to ``committed``. This module makes reconciliation evidence
**non-forgeable** by binding it to a trust root:

  * a **LIVE** provider must present a real ``provider_txn_id`` AND an Ed25519
    signature, over the canonical evidence payload, that verifies against a
    *pre-trusted* provider public key. A missing key, a missing signature, an
    unknown signer or a bad signature all fail closed;
  * a **REFERENCE** provider (a deterministic in-runtime reference connector) is
    not signed, but its ``result_digest`` must be reproduced by an independent
    ``recompute_reference`` recomputation supplied by the trusted caller. The
    verified outcome is permanently LABELLED ``reference`` and can NEVER be
    promoted to ``live``.

Every rejection raises a distinct, fail-closed exception so callers (and tests)
can prove *why* a piece of evidence was refused. The Ed25519 + base64 +
canonical-JSON idiom mirrors :mod:`youtab_runtime.effect_authorization`.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ProviderKind",
    "EvidenceVerificationError",
    "EffectIdentityMismatchError",
    "EvidenceWorkspaceMismatchError",
    "OperationDigestMismatchError",
    "StaleEvidenceError",
    "MissingProviderTransactionError",
    "MissingSignatureError",
    "UnknownSignerError",
    "InvalidEvidenceSignatureError",
    "ReferenceRecomputeUnavailableError",
    "ReferenceDigestMismatchError",
    "ReconciliationEvidence",
    "VerifiedOutcome",
    "verify_evidence",
    "ReferenceEvidenceSigner",
]

#: How the evidence was produced. A LIVE provider is signature-backed; a
#: REFERENCE provider is recomputation-backed. The two are never interchangeable
#: and a reference outcome is never promotable to live.
ProviderKind = Literal["live", "reference"]


# --------------------------------------------------------------------------- #
# Fail-closed rejection taxonomy (one class per reason)                        #
# --------------------------------------------------------------------------- #
class EvidenceVerificationError(Exception):
    """Fail-closed base: the evidence could not be trusted for reconciliation."""


class EffectIdentityMismatchError(EvidenceVerificationError):
    """Evidence ``effect_id`` does not match the effect being reconciled."""


class EvidenceWorkspaceMismatchError(EvidenceVerificationError):
    """Evidence ``workspace_id`` does not match the effect's bound workspace."""


class OperationDigestMismatchError(EvidenceVerificationError):
    """Evidence ``operation_digest`` does not match the effect's target scope."""


class StaleEvidenceError(EvidenceVerificationError):
    """Evidence was observed too long ago (or in the future) — reject as stale."""


class MissingProviderTransactionError(EvidenceVerificationError):
    """A LIVE provider presented no ``provider_txn_id`` — cannot be trusted."""


class MissingSignatureError(EvidenceVerificationError):
    """A LIVE provider presented no signature — cannot be trusted."""


class UnknownSignerError(EvidenceVerificationError):
    """No trusted public key is registered for the LIVE provider (unknown signer)."""


class InvalidEvidenceSignatureError(EvidenceVerificationError):
    """The LIVE provider's Ed25519 signature did not verify (forged/tampered)."""


class ReferenceRecomputeUnavailableError(EvidenceVerificationError):
    """REFERENCE evidence was supplied with no recompute function to check it."""


class ReferenceDigestMismatchError(EvidenceVerificationError):
    """The independent reference recomputation did not reproduce ``result_digest``."""


# --------------------------------------------------------------------------- #
# Evidence model                                                              #
# --------------------------------------------------------------------------- #
class ReconciliationEvidence(BaseModel):
    """A validated, signature-or-recomputation-bindable statement that a stranded
    effect reached a terminal outcome.

    The signature (LIVE) covers every field below except ``signature`` itself, so
    a signer commits to the full binding (provider + capability + effect identity
    + workspace + operation digest + provider txn + terminal state + result +
    observation time). Immutable once constructed (``frozen``); unknown fields are
    refused (``extra='forbid'``) so a forger cannot smuggle in extra state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.reconciliation-evidence.v1"] = (
        "youtab.reconciliation-evidence.v1"
    )
    provider: str = Field(min_length=1, max_length=128)
    capability: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=64)
    effect_id: str = Field(min_length=8, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    operation_digest: str = Field(pattern=r"^[0-9a-f]{8,128}$")
    provider_txn_id: Optional[str] = Field(default=None, max_length=256)
    observed_terminal_state: Literal["succeeded", "failed"]
    observed_at: datetime
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: ProviderKind
    signature: Optional[str] = Field(default=None, min_length=40, max_length=256)

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    def canonical_payload(self) -> bytes:
        """Deterministic byte payload the Ed25519 signature is computed over."""
        payload = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


@dataclass(frozen=True)
class VerifiedOutcome:
    """The trusted result of :func:`verify_evidence` — never constructed directly
    by callers, only returned after all fail-closed checks pass."""

    terminal_state: Literal["succeeded", "failed"]
    result_digest: str
    kind: ProviderKind
    provider: str


# --------------------------------------------------------------------------- #
# Verifier boundary                                                            #
# --------------------------------------------------------------------------- #
def verify_evidence(
    evidence: ReconciliationEvidence,
    *,
    expected_effect_id: str,
    expected_workspace_id: str,
    expected_operation_digest: str,
    now: datetime,
    max_age_seconds: float,
    live_provider_keys: Optional[Mapping[str, str]] = None,
    recompute_reference: Optional[Callable[[ReconciliationEvidence], str]] = None,
) -> VerifiedOutcome:
    """Verify reconciliation evidence and return a trusted :class:`VerifiedOutcome`,
    or raise a distinct :class:`EvidenceVerificationError` subclass (fail closed).

    The evidence is accepted ONLY if:

    * ``effect_id`` / ``workspace_id`` / ``operation_digest`` all equal the
      expected values (identity binding — a replay for a *different* effect is
      refused here);
    * ``observed_at`` is within ``max_age_seconds`` of ``now`` and not in the
      future (freshness);
    * for a LIVE provider: ``provider_txn_id`` is present AND an Ed25519 signature
      over :meth:`ReconciliationEvidence.canonical_payload` verifies against the
      pre-trusted ``live_provider_keys[provider]``. A missing key, missing
      signature, unknown signer or bad signature all fail closed;
    * for a REFERENCE provider: ``recompute_reference`` is supplied and its
      returned digest equals ``result_digest``. The outcome is labelled
      ``reference`` and is never promotable to ``live``.
    """
    # 1) identity binding — must match the effect exactly.
    if evidence.effect_id != expected_effect_id:
        raise EffectIdentityMismatchError(
            "evidence effect_id does not match the effect being reconciled"
        )
    if evidence.workspace_id != expected_workspace_id:
        raise EvidenceWorkspaceMismatchError(
            "evidence workspace_id does not match the effect's bound workspace"
        )
    if evidence.operation_digest != expected_operation_digest:
        raise OperationDigestMismatchError(
            "evidence operation_digest does not match the effect's target scope"
        )

    # 2) freshness — reject stale or future-dated observations.
    current = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    delta = (current - evidence.observed_at).total_seconds()
    if delta < 0 or delta > float(max_age_seconds):
        raise StaleEvidenceError("evidence is stale or observed in the future")

    # 3) trust root — signature (LIVE) or independent recomputation (REFERENCE).
    if evidence.provenance == "live":
        if not evidence.provider_txn_id:
            raise MissingProviderTransactionError(
                "live evidence must carry a provider transaction id"
            )
        if not evidence.signature:
            raise MissingSignatureError("live evidence must be signed")
        public_key_b64 = dict(live_provider_keys or {}).get(evidence.provider)
        if not public_key_b64:
            raise UnknownSignerError(
                "no trusted key registered for the live evidence provider"
            )
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(public_key_b64, validate=True)
            )
            public_key.verify(
                base64.b64decode(evidence.signature, validate=True),
                evidence.canonical_payload(),
            )
        except (ValueError, InvalidSignature) as exc:
            raise InvalidEvidenceSignatureError(
                "live evidence signature did not verify"
            ) from exc
    else:  # provenance == "reference" (the only other Literal value)
        if recompute_reference is None:
            raise ReferenceRecomputeUnavailableError(
                "reference evidence requires a recompute function to be trusted"
            )
        recomputed = recompute_reference(evidence)
        if recomputed != evidence.result_digest:
            raise ReferenceDigestMismatchError(
                "reference recomputation did not reproduce the result digest"
            )

    # Provenance is carried through verbatim; a reference outcome can never be
    # relabelled "live" because it is copied straight from the verified evidence.
    return VerifiedOutcome(
        terminal_state=evidence.observed_terminal_state,
        result_digest=evidence.result_digest,
        kind=evidence.provenance,
        provider=evidence.provider,
    )


# --------------------------------------------------------------------------- #
# Test-only signer (deterministic LIVE evidence for tests)                     #
# --------------------------------------------------------------------------- #
class ReferenceEvidenceSigner:
    """Test-only Ed25519 signer that mints validly-signed LIVE evidence.

    Used by tests to produce both a *genuinely* signed live evidence (whose key
    is registered as trusted) and forged/unsigned variants. It intentionally does
    NOT carry a production trust marker: production trust comes only from the
    operator-provisioned ``live_provider_keys`` passed to :func:`verify_evidence`,
    so a test key is trusted only if a test explicitly registers it.
    """

    __test__ = False  # not a pytest test class despite living beside them

    def __init__(self, provider: str = "reference-live"):
        self.provider = provider
        self._sk = Ed25519PrivateKey.generate()

    def public_key_b64(self) -> str:
        from cryptography.hazmat.primitives import serialization

        raw = self._sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")

    def keyring(self) -> dict[str, str]:
        """The trusted ``{provider: public_key_b64}`` map to pass as
        ``live_provider_keys``."""
        return {self.provider: self.public_key_b64()}

    def mint(
        self,
        *,
        capability: str,
        capability_version: str,
        effect_id: str,
        workspace_id: str,
        operation_digest: str,
        provider_txn_id: str,
        observed_terminal_state: Literal["succeeded", "failed"],
        observed_at: datetime,
        result_digest: str,
    ) -> ReconciliationEvidence:
        """Return a LIVE :class:`ReconciliationEvidence` signed by this signer."""
        unsigned = ReconciliationEvidence(
            provider=self.provider,
            capability=capability,
            capability_version=capability_version,
            effect_id=effect_id,
            workspace_id=workspace_id,
            operation_digest=operation_digest,
            provider_txn_id=provider_txn_id,
            observed_terminal_state=observed_terminal_state,
            observed_at=observed_at,
            result_digest=result_digest,
            provenance="live",
            signature="0" * 64,
        )
        sig = self._sk.sign(unsigned.canonical_payload())
        return unsigned.model_copy(
            update={"signature": base64.b64encode(sig).decode("ascii")}
        )
