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

import hashlib
import hmac
import secrets
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime
from typing import Sequence

from .contracts import BrainCommandEnvelope

# Module-private capability token. Never exported, never returned, never logged.
# Holding this object is the sole authority to mint an AdmittedCommand, and only
# code in this package (AuthorityBoundary.admit) references it.
_ADMISSION_CAPABILITY = object()

# Per-process admission secret backing the admission proof. Regenerated every
# process start; a handle minted in another process (or forged offline) cannot
# carry a proof that verifies here.
_ADMISSION_SECRET = secrets.token_bytes(32)


@dataclass(frozen=True)
class CapabilityBinding:
    """The per-run capability manifest bound to an admitted execution context.

    WAVE-30H correction 2. A grant's ``allowed_toolsets`` may contain the ``"*"``
    full-envelope sentinel, meaning "all tools this principal is entitled to".
    ``"*"`` must NOT silently authorize tools that appear LATER (a tool registered
    after admission, or one added by a future deploy). This binding freezes, at
    admission time, the exact set of tool names + schema hashes that were
    registered, authorized (agent ACL / tenant / Simorgh policy) and operational,
    together with the effective policy/ACL versions. ``decide_tool`` authorizes a
    tool only if it is in this frozen manifest (unless ``dynamic_inclusion`` is
    set by the bound policy). Because the manifest hash is folded into the
    admission proof, a handle whose binding was swapped fails ``verify_proof``.
    """

    tool_hashes: tuple[tuple[str, str], ...]  # sorted, de-duped (name, schema_hash)
    policy_version: str
    acl_version: str
    dynamic_inclusion: bool = False

    @classmethod
    def build(
        cls,
        pairs: Sequence[tuple[str, str]],
        *,
        policy_version: str,
        acl_version: str,
        dynamic_inclusion: bool = False,
    ) -> "CapabilityBinding":
        norm = tuple(sorted({(str(n), str(h)) for n, h in pairs}))
        return cls(
            tool_hashes=norm,
            policy_version=str(policy_version),
            acl_version=str(acl_version),
            dynamic_inclusion=bool(dynamic_inclusion),
        )

    @property
    def manifest_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.policy_version.encode("utf-8"))
        h.update(b"\x1f")
        h.update(self.acl_version.encode("utf-8"))
        h.update(b"\x1f")
        h.update(b"1" if self.dynamic_inclusion else b"0")
        h.update(b"\x1e")
        for name, schema_hash in self.tool_hashes:
            h.update(name.encode("utf-8"))
            h.update(b"=")
            h.update(schema_hash.encode("utf-8"))
            h.update(b";")
        return h.hexdigest()

    def authorizes(self, tool_name: str) -> bool:
        """True if ``tool_name`` was in the manifest frozen at admission.

        ``dynamic_inclusion`` (an explicit bound-policy opt-in) is the only way a
        tool NOT in the frozen manifest becomes authorized; it is off by default.
        """
        if self.dynamic_inclusion:
            return True
        return any(name == tool_name for name, _ in self.tool_hashes)


def _binding_hash(binding: "CapabilityBinding | None") -> str:
    return binding.manifest_hash if binding is not None else ""


def _compute_proof(
    envelope: BrainCommandEnvelope,
    admitted_at: datetime,
    binding: "CapabilityBinding | None" = None,
) -> str:
    msg = (
        envelope.canonical_payload()
        + b"|"
        + admitted_at.isoformat().encode("utf-8")
        + b"|"
        + _binding_hash(binding).encode("utf-8")
    )
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
    capability_binding: "CapabilityBinding | None" = None
    _capability: InitVar[object] = None

    def __post_init__(self, _capability: object) -> None:
        if _capability is not _ADMISSION_CAPABILITY:
            raise TypeError(
                "AdmittedCommand is minted only by AuthorityBoundary.admit(); "
                "it cannot be constructed by tool or agent code"
            )

    def verify_proof(self) -> None:
        """Fail closed if this handle was not minted by this process's admission
        path (defence in depth against a smuggled/forged handle). The bound
        capability manifest is part of the proof, so swapping the binding on a
        handle also fails here."""
        expected = _compute_proof(
            self.envelope, self.admitted_at, self.capability_binding
        )
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
    capability: object,
    envelope: BrainCommandEnvelope,
    *,
    now: datetime | None = None,
    capability_binding: "CapabilityBinding | None" = None,
) -> AdmittedCommand:
    """Mint an :class:`AdmittedCommand`. ``capability`` MUST be the module-private
    admission token; only ``AuthorityBoundary.admit`` holds it. Any other caller
    gets a ``TypeError`` from the handle's ``__post_init__``.

    ``capability_binding`` freezes the per-run authorized tool manifest into the
    sealed context (correction 2); it is folded into the admission proof."""
    admitted_at = (now or datetime.now(UTC)).astimezone(UTC)
    proof = _compute_proof(envelope, admitted_at, capability_binding)
    return AdmittedCommand(
        envelope=envelope,
        admitted_at=admitted_at,
        admission_proof=proof,
        capability_binding=capability_binding,
        _capability=capability,
    )
