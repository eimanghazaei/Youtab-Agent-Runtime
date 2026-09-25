"""Managed-execution trust mode + Simorgh execution-grant admission (WAVE-30H R3).

Two composed authentication layers gate a managed run (ADR-0002 DP1):

  1. **Transport HMAC** — ``runtime_command_auth`` proves which connector sent
     this exact, fresh, unreplayed request.
  2. **Execution grant (this module)** — the Ed25519 grant minted ONLY by
     Simorgh is the canonical *execution authority*. Managed runs REQUIRE it;
     HMAC is not an authority substitute. The engine never creates, widens,
     replaces or infers a missing grant — a run without a valid grant is refused.

Trust mode is an EXPLICIT configuration switch, never inferred from the presence
or absence of a grant:

  * ``managed``          — a valid Simorgh grant is MANDATORY on create/cancel/retry.
  * ``local-standalone`` — the local CLI trust mode. No grant is required, and a
    managed grant offered here is REFUSED (standalone must not accept managed
    credentials implicitly). It must not claim to be Simorgh-managed.

The default is ``local-standalone`` so existing single-operator / benchmark
deployments keep working unchanged; ``managed`` is opt-in via
``YOUTAB_RUNTIME_TRUST_MODE=managed`` and additionally requires the Brain public
keyring (``YOUTAB_BRAIN_PUBLIC_KEYS``, ADR-0002 DP6).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from pydantic import ValidationError

from .admission import AdmittedCommand, CapabilityBinding
from .contracts import BrainCommandEnvelopeV2
from .policy import AdmissionError, AuthorityBoundary

TRUST_MODE_ENV = "YOUTAB_RUNTIME_TRUST_MODE"
BRAIN_PUBLIC_KEYS_ENV = "YOUTAB_BRAIN_PUBLIC_KEYS"
# The header the base64 grant JSON rides on (managed create/cancel/retry).
GRANT_HEADER = "x-youtab-execution-grant"


class TrustMode(StrEnum):
    MANAGED = "managed"
    LOCAL_STANDALONE = "local-standalone"


class ManagedAdmissionError(Exception):
    """Fail-closed managed-admission failure with a router-mappable status.

    ``code`` is a stable machine-readable class; ``http_status`` is what the
    endpoint should surface. ``message`` never echoes key material or signatures.
    """

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def current_trust_mode(env: Mapping[str, str] | None = None) -> TrustMode:
    """Resolve the explicit trust mode. Default ``local-standalone``.

    An unrecognised value fails closed (raises) rather than guessing — a
    misconfigured trust mode must never silently fall into the weaker posture.
    """
    raw = (env or os.environ).get(TRUST_MODE_ENV)
    if raw is None or not raw.strip():
        return TrustMode.LOCAL_STANDALONE
    value = raw.strip().lower()
    try:
        return TrustMode(value)
    except ValueError as exc:
        raise ManagedAdmissionError(
            "invalid_trust_mode",
            f"unrecognised {TRUST_MODE_ENV}={value!r}",
            500,
        ) from exc


def load_brain_public_keys(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Parse the Brain public keyring ``{key_id: base64_ed25519_pub}`` (DP6).

    Verify-only material; the engine never holds a Brain private key. A malformed
    keyring fails closed.
    """
    raw = (env or os.environ).get(BRAIN_PUBLIC_KEYS_ENV) or ""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManagedAdmissionError(
            "authority_unavailable", "brain public keyring is malformed", 503
        ) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ManagedAdmissionError(
            "authority_unavailable",
            "brain public keyring must be a {key_id: b64} object",
            503,
        )
    return parsed


@dataclass(frozen=True)
class AdmissionIdentity:
    """The gateway-verified transport identity a grant is cross-checked against."""

    tenant: str
    user: str
    workspace: str = "-"


def decode_grant_header(value: str) -> BrainCommandEnvelopeV2:
    """base64(compact JSON) -> validated v2 grant. Malformed -> fail closed 400."""
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManagedAdmissionError(
            "grant_malformed", "execution grant is not valid base64", 400
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedAdmissionError(
            "grant_malformed", "execution grant is not valid JSON", 400
        ) from exc
    try:
        return BrainCommandEnvelopeV2.model_validate(payload)
    except ValidationError as exc:
        raise ManagedAdmissionError(
            "grant_malformed", "execution grant failed schema validation", 400
        ) from exc


def _resolve_key(public_keys: Mapping[str, str], key_id: str) -> str:
    pub = public_keys.get(key_id)
    if not pub:
        raise ManagedAdmissionError(
            "unknown_grant_key", "execution grant key id is not trusted", 401
        )
    return pub


def _check_binding(envelope: BrainCommandEnvelopeV2, identity: AdmissionIdentity) -> None:
    """The grant must be for THIS transport principal — never widen it."""
    if envelope.tenant_id != identity.tenant or envelope.user_id != identity.user:
        raise ManagedAdmissionError(
            "grant_identity_mismatch",
            "execution grant does not match the request identity",
            403,
        )
    # Workspace binding: when the request is workspace-scoped the grant must
    # name the same workspace. An unscoped request ("-") accepts a grant whose
    # workspace is also unscoped.
    if identity.workspace != envelope.workspace_id:
        raise ManagedAdmissionError(
            "grant_workspace_mismatch",
            "execution grant does not match the request workspace",
            403,
        )


def admit_managed_run(
    *,
    grant_header: str | None,
    identity: AdmissionIdentity,
    boundary: AuthorityBoundary,
    public_keys: Mapping[str, str],
    now=None,
    capability_binding: CapabilityBinding | None = None,
) -> AdmittedCommand:
    """Ingress admission for a managed run. Returns the sealed AdmittedCommand.

    Fail-closed order: grant present -> trusted key -> valid signature/expiry/
    scope + single-use nonce (via ``boundary.admit``) -> identity/workspace
    binding. Any failure raises :class:`ManagedAdmissionError`; the run is never
    created without a valid grant.

    ``capability_binding`` (correction 2) is the per-run authorized tool manifest
    the caller froze from the live registry + agent ACL; it is sealed into the
    admitted context so ``"*"`` cannot later authorize a newly registered tool.
    The caller builds it (youtab_runtime does not import the tool registry).
    """
    if not grant_header or not grant_header.strip():
        raise ManagedAdmissionError(
            "grant_required",
            "managed execution requires a Simorgh execution grant",
            401,
        )
    if not public_keys:
        # Managed mode configured but no verify keys — cannot establish authority.
        raise ManagedAdmissionError(
            "authority_unavailable",
            "no Brain public keys configured for grant verification",
            503,
        )
    envelope = decode_grant_header(grant_header)
    public_key = _resolve_key(public_keys, envelope.key_id)
    try:
        admitted = boundary.admit(
            envelope, public_key, now=now, capability_binding=capability_binding
        )
    except AdmissionError as exc:
        # Durable replay store unavailable — unavailable, not rejected.
        raise ManagedAdmissionError(
            "authority_unavailable", "grant admission store unavailable", 503
        ) from exc
    except ValueError as exc:
        # Invalid signature, expired, replayed, or forbidden scope — rejected.
        raise ManagedAdmissionError(
            "grant_rejected", "execution grant rejected", 401
        ) from exc
    _check_binding(envelope, identity)
    return admitted


def reject_grant_in_standalone(grant_header: str | None) -> None:
    """In local-standalone mode a managed grant must NOT be accepted implicitly.

    A grant offered to a standalone engine is a configuration/authority error:
    the standalone path has its own (documented) local trust boundary and must
    not masquerade as Simorgh-managed. Fail closed rather than silently ignore.
    """
    if grant_header and grant_header.strip():
        raise ManagedAdmissionError(
            "grant_not_accepted_in_standalone",
            "local-standalone runtime does not accept managed execution grants",
            400,
        )


def re_admit_worker_grant(
    *,
    grant_header: str,
    boundary: AuthorityBoundary,
    public_keys: Mapping[str, str],
    now=None,
    capability_binding: CapabilityBinding | None = None,
) -> AdmittedCommand:
    """Worker-side re-admission of the persisted grant (see AuthorityBoundary.re_admit).

    Re-verifies signature/expiry/scope and re-seals the AdmittedCommand in the
    worker process WITHOUT re-burning the nonce (ingress already consumed it).

    ``capability_binding`` (correction 2) is reconstructed from the manifest the
    ingress FROZE and persisted with the grant — NOT from the worker's own live
    registry — so the worker enforces exactly the tool set authorized at
    admission, and a tool registered between ingress and worker (or mid-run) is
    never swept in.
    """
    envelope = decode_grant_header(grant_header)
    public_key = _resolve_key(public_keys, envelope.key_id)
    try:
        return boundary.re_admit(
            envelope, public_key, now=now, capability_binding=capability_binding
        )
    except ValueError as exc:
        raise ManagedAdmissionError(
            "grant_rejected", "persisted execution grant rejected at worker", 401
        ) from exc
