"""Enterprise memory routing and claim contracts for the managed runtime.

This subpackage is the additive, fail-closed seam that binds canonical scoped
identity (from an admitted :class:`~youtab_runtime.contracts.BrainCommandEnvelope`)
to typed memory claims and routes memory writes to their correct owner.

It does not itself persist anything, does not open network connections, and is
intentionally NOT imported by the live agent loop. It reifies the boundary
described in ``docs/architecture/YOUTAB_AGENT_RUNTIME_BOUNDARY.md`` and the
proposed cognitive-growth memory model (ADR-0002) without granting the runtime
any sovereign-memory authority: the runtime may classify and route, never
self-promote knowledge to One Brain.
"""

from __future__ import annotations

from .claim import (
    Classification,
    ClaimStatus,
    MemoryClaim,
    MemoryType,
    TrustLevel,
)
from .router import MemoryRouter, MemoryRoutingDecision, MemoryWriteIntent, RouteOwner
from .scope import MemoryScope, ScopeAdmission

__all__ = [
    "Classification",
    "ClaimStatus",
    "MemoryClaim",
    "MemoryRouter",
    "MemoryRoutingDecision",
    "MemoryScope",
    "MemoryType",
    "MemoryWriteIntent",
    "RouteOwner",
    "ScopeAdmission",
    "TrustLevel",
]
