"""The sealed, unforgeable execution context minted only by admission.

ADR-0002 DP2. Finding #5 was "a tool decision made on an envelope that was never
``admit()``-ed." This module makes that state **unrepresentable**: the only value
that authorizes a tool decision is an :class:`AdmittedCommand`, and an
``AdmittedCommand`` can be constructed **only** by :func:`seal_admitted_command`,
which is called **only** from ``AuthorityBoundary.admit()`` after signature,
scope, freshness and durable-replay checks pass. Model/tool code cannot obtain
the module-private capability token, so it cannot fabricate one — the fix is
structural ("there is no execution context unless admit() produced it"), not a
convention ("remember to call admit() first").

Defence in depth: each handle also carries an ``admission_proof`` — a keyed
digest over the envelope's canonical payload under a per-process admission
secret — so even a handle smuggled across a trust boundary (pickle, a forged
subclass) fails ``verify_proof`` unless it was minted in this process by the
admission path.
"""
from __future__ import annotations

import hmac
import secrets
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime

from .contracts import BrainCommandEnvelope

# Module-private capability token. Never exported, never returned, never logged.
# Holding this object is the sole authority to mint an AdmittedCommand, and only
# code in this package (AuthorityBoundary.admit) references it.
_ADMISSION_CAPABILITY = object()

# Per-process admission secret backing the admission proof. Regenerated every
# process start; a handle minted in another process (or forged offline) cannot
# carry a proof that verifies here.
_ADMISSION_SECRET = secrets.token_bytes(32)


def _compute_proof(envelope: BrainCommandEnvelope, admitted_at: datetime) -> str:
    msg = envelope.canonical_payload() + b"|" + admitted_at.isoformat().encode("utf-8")
    return hmac.new(_ADMISSION_SECRET, msg, "sha256").hexdigest()


@dataclass(frozen=True)
class AdmittedCommand:
    """An immutable, admission-proven execution context.

    Constructing this directly raises: it may be minted only by
    :func:`seal_admitted_command` (the InitVar capability is module-private).
    Every managed operation (tool dispatch, memory scoping, completion) reads
    tenant/user/scopes/budget from ``envelope`` here — never from caller args.
    """

    envelope: BrainCommandEnvelope
    admitted_at: datetime
    admission_proof: str
    _capability: InitVar[object] = None

    def __post_init__(self, _capability: object) -> None:
        if _capability is not _ADMISSION_CAPABILITY:
            raise TypeError(
                "AdmittedCommand is minted only by AuthorityBoundary.admit(); "
                "it cannot be constructed by tool or agent code"
            )

    def verify_proof(self) -> None:
        """Fail closed if this handle was not minted by this process's admission
        path (defence in depth against a smuggled/forged handle)."""
        expected = _compute_proof(self.envelope, self.admitted_at)
        if not hmac.compare_digest(expected, self.admission_proof):
            raise ValueError("admission proof invalid; handle was not admitted here")

    # Convenience, read-only projections of the authority the context carries.
    @property
    def tenant_id(self) -> str:
        return self.envelope.tenant_id

    @property
    def user_id(self) -> str:
        return self.envelope.user_id


def seal_admitted_command(
    capability: object, envelope: BrainCommandEnvelope, *, now: datetime | None = None
) -> AdmittedCommand:
    """Mint an :class:`AdmittedCommand`. ``capability`` MUST be the module-private
    admission token; only ``AuthorityBoundary.admit`` holds it. Any other caller
    gets a ``TypeError`` from the handle's ``__post_init__``."""
    admitted_at = (now or datetime.now(UTC)).astimezone(UTC)
    proof = _compute_proof(envelope, admitted_at)
    return AdmittedCommand(
        envelope=envelope,
        admitted_at=admitted_at,
        admission_proof=proof,
        _capability=capability,
    )
