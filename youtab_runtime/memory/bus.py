"""Versioned Runtime<->Brain MemoryBus contract and a NON-LIVE reference bus.

The MemoryBus is the governed seam through which the runtime *reads* scoped
memory and *proposes* promotions to One Brain (Simorgh). The runtime never
writes sovereign memory directly: a promotion is a candidate the Brain Effect
Gate must authorize (mirroring :class:`~youtab_runtime.contracts.EffectProposal`).

No live Simorgh endpoint exists in this repository yet. This module therefore
defines the typed interface plus :class:`ReferenceMemoryBus`, a deterministic,
in-process implementation for Runtime tests. The reference bus is *type-distinct*
from any future live client: every result it returns is stamped
``source="reference"`` and ``is_live=False`` so a reference answer can never be
mistaken for live sovereign authority or a live receipt. See the exact
cross-repository dependency contract in
``docs/architecture/RUNTIME_SIMORGH_MEMORYBUS_CONTRACT.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .claim import ClaimStatus, MemoryClaim, MemoryType, TrustLevel
from .scope import MemoryScope

SourceLabel = Literal["live", "reference"]


class MemoryQuery(BaseModel):
    """A scoped, bounded read request against the MemoryBus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.memory-query.v1"] = "youtab.memory-query.v1"
    scope: MemoryScope
    text: str = Field(min_length=1, max_length=8192)
    memory_types: tuple[MemoryType, ...] = ()
    limit: int = Field(default=8, ge=1, le=200)
    min_trust: TrustLevel = TrustLevel.VALIDATED
    deadline_at: datetime | None = None


class RetrievedMemory(BaseModel):
    """One retrieved claim plus its citation and relevance score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: MemoryClaim
    score: float = Field(ge=0.0, le=1.0)
    citation: str = Field(min_length=1, max_length=1024)


class MemoryQueryResult(BaseModel):
    """The bounded, explainable result of a MemoryBus query.

    ``degraded`` is True when authoritative retrieval was unavailable and the
    caller must surface that memory retrieval could not be completed. There is
    never a silent fallback to a different tenant/profile.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.memory-query-result.v1"] = (
        "youtab.memory-query-result.v1"
    )
    source: SourceLabel
    is_live: bool
    degraded: bool = False
    degraded_reason: str | None = Field(default=None, max_length=1024)
    results: tuple[RetrievedMemory, ...] = ()


class PromotionCandidate(BaseModel):
    """A claim the runtime proposes for Brain-governed promotion.

    This is a request, not a write: ``runtime_authorized`` is fixed False,
    exactly as :class:`~youtab_runtime.contracts.EffectProposal`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.memory-promotion.v1"] = (
        "youtab.memory-promotion.v1"
    )
    claim: MemoryClaim
    justification: str = Field(min_length=1, max_length=4096)
    runtime_authorized: Literal[False] = False


@runtime_checkable
class MemoryBusClient(Protocol):
    """The typed contract a live Simorgh client or the reference bus implements."""

    @property
    def source(self) -> SourceLabel: ...

    @property
    def is_live(self) -> bool: ...

    def query(self, request: MemoryQuery) -> MemoryQueryResult: ...

    def store_candidate(self, candidate: PromotionCandidate) -> str: ...

    def feedback(self, memory_id: str, *, scope: MemoryScope, helpful: bool) -> None: ...

    def supersede(self, memory_id: str, *, scope: MemoryScope, by: str) -> None: ...

    def erase(self, memory_id: str, *, scope: MemoryScope, reason: str) -> None: ...


class ReferenceMemoryBus:
    """Deterministic, in-process, NON-LIVE MemoryBus for Runtime tests.

    It enforces the same scope discipline a live bus must: a query only ever
    sees claims stored under an exactly-matching scope partition; there is no
    cross-tenant, cross-workspace or cross-purpose bleed. Promotion candidates
    are queued, never auto-validated — only an explicit governed step could
    validate them, which this reference bus deliberately does not do.
    """

    source: SourceLabel = "reference"
    is_live: bool = False

    def __init__(self) -> None:
        self._store: dict[str, dict[str, MemoryClaim]] = {}
        self._pending: dict[str, PromotionCandidate] = {}

    def seed_validated(self, claim: MemoryClaim) -> None:
        """Test helper: place an already-VALIDATED claim into a scope partition."""

        if claim.status is not ClaimStatus.VALIDATED:
            raise ValueError("reference bus only seeds VALIDATED claims for retrieval")
        self._store.setdefault(claim.scope.partition_key(), {})[claim.memory_id] = claim

    def query(self, request: MemoryQuery) -> MemoryQueryResult:
        partition = self._store.get(request.scope.partition_key(), {})
        text = request.text.casefold()
        hits: list[RetrievedMemory] = []
        for claim in partition.values():
            if not claim.is_retrievable_for_production():
                continue
            if request.memory_types and claim.memory_type not in request.memory_types:
                continue
            body = claim.content or claim.content_ref or ""
            if text and text not in body.casefold():
                continue
            hits.append(
                RetrievedMemory(
                    claim=claim,
                    score=1.0,
                    citation=f"reference:{claim.memory_id}",
                )
            )
        hits.sort(key=lambda r: r.claim.memory_id)
        return MemoryQueryResult(
            source=self.source,
            is_live=self.is_live,
            results=tuple(hits[: request.limit]),
        )

    def store_candidate(self, candidate: PromotionCandidate) -> str:
        self._pending[candidate.claim.memory_id] = candidate
        return candidate.claim.memory_id

    def pending_candidates(self) -> tuple[PromotionCandidate, ...]:
        return tuple(self._pending.values())

    def feedback(self, memory_id: str, *, scope: MemoryScope, helpful: bool) -> None:
        # Deterministic reference: feedback is accepted but does not mutate trust.
        _ = (memory_id, scope, helpful)

    def supersede(self, memory_id: str, *, scope: MemoryScope, by: str) -> None:
        partition = self._store.get(scope.partition_key(), {})
        existing = partition.get(memory_id)
        if existing is None:
            raise KeyError("cannot supersede a claim outside the caller's scope")
        partition[memory_id] = existing.with_status(
            ClaimStatus.SUPERSEDED, supersedes_id=by
        )

    def erase(self, memory_id: str, *, scope: MemoryScope, reason: str) -> None:
        _ = reason
        partition = self._store.get(scope.partition_key(), {})
        partition.pop(memory_id, None)


def degraded_result(reason: str) -> MemoryQueryResult:
    """Build an explicit degraded result (retrieval unavailable, no fallback)."""

    return MemoryQueryResult(
        source="live",
        is_live=True,
        degraded=True,
        degraded_reason=reason,
        results=(),
    )
