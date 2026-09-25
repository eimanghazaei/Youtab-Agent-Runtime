"""Default-deny memory router.

The router is the single seam every Runtime surface uses to decide *where* a
memory write belongs and *whether* the runtime may perform it. It classifies a
:class:`MemoryWriteIntent` and returns a :class:`MemoryRoutingDecision` naming
the owner. It mirrors :class:`~youtab_runtime.policy.ManagedToolDecision`: the
runtime may write its own execution-local state, but anything that would place
sovereign knowledge, effect truth, secrets or raw source-of-truth data into the
wrong tier is refused or routed to its authority — never silently written.

The router performs no I/O and holds no state; it is pure policy, so it is
trivially unit-testable and safe to import anywhere.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .claim import Classification, MemoryType, TrustLevel
from .scope import MemoryScope


class RouteOwner(StrEnum):
    """Which subsystem owns a given write (see RUNTIME_BOUNDARY / ADR-0002)."""

    RUNTIME_WORKING = "runtime_working"  # execution-local, agent-autonomous
    RUNTIME_CAPSULE = "runtime_capsule"  # bounded always-injected rules/profile
    EPISODIC_STORE = "episodic_store"  # scoped episodic memory
    BRAIN_MEMORYBUS = "brain_memorybus"  # Simorgh — requires effect-gate promotion
    EFFECT_LEDGER = "effect_ledger"  # run journal / effect ledger / receipts
    SOURCE_SYSTEM = "source_system"  # CRM/ERP/SAP/CAD authoritative record
    REJECTED = "rejected"  # not storable as memory at all


# Bounded capsule may only ever hold small, trusted, non-sensitive content.
_CAPSULE_MAX_CHARS = 3_000
_CAPSULE_ALLOWED_TRUST = frozenset(
    {TrustLevel.USER_ASSERTED, TrustLevel.SYSTEM_ATTRIBUTED, TrustLevel.VALIDATED}
)
_CAPSULE_ALLOWED_CLASSIFICATION = frozenset(
    {Classification.PUBLIC, Classification.INTERNAL}
)


class MemoryWriteIntent(BaseModel):
    """A proposed memory write, before it is turned into a stored claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: MemoryScope
    memory_type: MemoryType
    trust_level: TrustLevel
    classification: Classification = Classification.INTERNAL
    content_length: int = Field(ge=0, le=10_000_000)
    contains_secret_material: bool = False
    is_promotion_to_sovereign: bool = False
    carries_source_of_truth_payload: bool = False


class MemoryRoutingDecision(BaseModel):
    """The router's verdict for one intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: RouteOwner
    allow_runtime_write: bool
    requires_effect_gate: bool
    reason: str = Field(min_length=1, max_length=1024)


class MemoryRouter:
    """Fail-closed classifier and router for memory writes."""

    def route(self, intent: MemoryWriteIntent) -> MemoryRoutingDecision:
        # 1. Secrets never enter any memory content, in any tier.
        if intent.contains_secret_material or intent.classification is Classification.SECRET:
            return MemoryRoutingDecision(
                owner=RouteOwner.REJECTED,
                allow_runtime_write=False,
                requires_effect_gate=False,
                reason="secret material is never stored as memory content",
            )

        # 2. Effect truth is owned by the run journal / effect ledger, not memory.
        if intent.memory_type is MemoryType.EFFECT_TRUTH:
            return MemoryRoutingDecision(
                owner=RouteOwner.EFFECT_LEDGER,
                allow_runtime_write=False,
                requires_effect_gate=False,
                reason="effect truth must be recorded in the effect ledger, not memory",
            )

        # 3. Authoritative records: only identifiers/refs, never the raw payload.
        if intent.memory_type is MemoryType.AUTHORITATIVE_REF:
            if intent.carries_source_of_truth_payload:
                return MemoryRoutingDecision(
                    owner=RouteOwner.SOURCE_SYSTEM,
                    allow_runtime_write=False,
                    requires_effect_gate=False,
                    reason="raw source-of-truth payload must stay in the source system",
                )
            return MemoryRoutingDecision(
                owner=RouteOwner.RUNTIME_WORKING,
                allow_runtime_write=True,
                requires_effect_gate=False,
                reason="scoped identifier/reference to a source record",
            )

        # 4. Any promotion to sovereign/organization memory is Brain-gated.
        if intent.is_promotion_to_sovereign or intent.memory_type is MemoryType.SEMANTIC:
            return MemoryRoutingDecision(
                owner=RouteOwner.BRAIN_MEMORYBUS,
                allow_runtime_write=False,
                requires_effect_gate=True,
                reason="semantic promotion to One Brain requires the Effect Gate",
            )

        # 5. Always-injected capsule: bounded, trusted, non-sensitive only.
        if intent.memory_type is MemoryType.CAPSULE:
            if intent.content_length > _CAPSULE_MAX_CHARS:
                reason = "capsule content exceeds the bounded always-injected budget"
            elif intent.trust_level not in _CAPSULE_ALLOWED_TRUST:
                reason = "capsule requires trusted (non-inferred, non-untrusted) content"
            elif intent.classification not in _CAPSULE_ALLOWED_CLASSIFICATION:
                reason = "capsule may not hold confidential/restricted content"
            else:
                return MemoryRoutingDecision(
                    owner=RouteOwner.RUNTIME_CAPSULE,
                    allow_runtime_write=True,
                    requires_effect_gate=False,
                    reason="bounded trusted rules/profile capsule",
                )
            return MemoryRoutingDecision(
                owner=RouteOwner.REJECTED,
                allow_runtime_write=False,
                requires_effect_gate=False,
                reason=reason,
            )

        # 6. Execution-local working state is agent-autonomous within its scope.
        if intent.memory_type is MemoryType.WORKING:
            return MemoryRoutingDecision(
                owner=RouteOwner.RUNTIME_WORKING,
                allow_runtime_write=True,
                requires_effect_gate=False,
                reason="execution-local working/checkpoint state",
            )

        # 7. Episodic observations are scoped and agent-autonomous to record.
        if intent.memory_type is MemoryType.EPISODIC:
            return MemoryRoutingDecision(
                owner=RouteOwner.EPISODIC_STORE,
                allow_runtime_write=True,
                requires_effect_gate=False,
                reason="scoped episodic observation",
            )

        # 8. Procedural knowledge belongs to the skills registry, not free memory.
        if intent.memory_type is MemoryType.PROCEDURAL:
            return MemoryRoutingDecision(
                owner=RouteOwner.BRAIN_MEMORYBUS,
                allow_runtime_write=False,
                requires_effect_gate=True,
                reason="procedural/skill knowledge is promoted via the registry gate",
            )

        # 9. Default deny: an unclassified intent is never written.
        return MemoryRoutingDecision(
            owner=RouteOwner.REJECTED,
            allow_runtime_write=False,
            requires_effect_gate=False,
            reason="default-deny: unclassified memory write",
        )
