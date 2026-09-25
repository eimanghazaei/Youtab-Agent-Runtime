"""Versioned Runtime<->Brain MemoryBus contract and a NON-LIVE reference bus.

The MemoryBus is the governed seam through which the runtime *reads* scoped
memory and *proposes* promotions to One Brain (Simorgh). The runtime never
writes sovereign memory directly: a promotion is a candidate the Brain Effect
Gate must authorize (mirroring :class:`~youtab_runtime.contracts.EffectProposal`).

**Simorgh is the memory authority.** This module is transport only. It defines
the typed request/result envelopes plus two DELIBERATELY DISTINCT result types:

* :class:`MemoryBusResult` — production authority evidence. It may originate ONLY
  from an authenticated Simorgh client. ``is_live`` is the literal ``True``.
* :class:`ReferenceMemoryBusResult` — deterministic, local, NON-LIVE reference
  evidence produced by :class:`ReferenceMemoryBus` for Runtime tests. ``is_live``
  is the literal ``False`` and its ``schema_version`` differs, so it is NOT
  type-interchangeable with a production result and cannot validate as one.

A live consumer MUST route results through :func:`consume_for_live`, which is
fail-closed: it accepts only a genuine :class:`MemoryBusResult` and rejects a
reference result, a copied/forged-provenance object, or anything else.

No live Simorgh endpoint exists in this repository yet; nothing here is
LIVE-integrated. See ``docs/architecture/RUNTIME_SIMORGH_MEMORYBUS_CONTRACT.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .claim import ClaimStatus, MemoryClaim, MemoryType, TrustLevel
from .scope import MemoryScope


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
    """One retrieved claim (a transport DTO) plus its citation and score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: MemoryClaim
    score: float = Field(ge=0.0, le=1.0)
    citation: str = Field(min_length=1, max_length=1024)


class MemoryBusResult(BaseModel):
    """Production authority result — ONLY from an authenticated Simorgh client.

    ``degraded`` is True when authoritative retrieval was unavailable and the
    caller must surface that memory retrieval could not be completed. There is
    never a silent fallback to a different tenant/profile.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.memory-bus-result.v1"] = (
        "youtab.memory-bus-result.v1"
    )
    source: Literal["live"] = "live"
    is_live: Literal[True] = True
    degraded: bool = False
    degraded_reason: str | None = Field(default=None, max_length=1024)
    results: tuple[RetrievedMemory, ...] = ()


class ReferenceMemoryBusResult(BaseModel):
    """NON-LIVE reference result — deterministic, local, for tests only.

    A distinct type with a distinct ``schema_version`` so it can never be passed
    where a production :class:`MemoryBusResult` is required.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.memory-bus-reference-result.v1"] = (
        "youtab.memory-bus-reference-result.v1"
    )
    source: Literal["reference"] = "reference"
    is_live: Literal[False] = False
    degraded: bool = False
    degraded_reason: str | None = Field(default=None, max_length=1024)
    results: tuple[RetrievedMemory, ...] = ()


class ReferenceProvenanceError(ValueError):
    """Raised when reference (or non-authoritative) evidence reaches a live path."""


def consume_for_live(result: object) -> MemoryBusResult:
    """Fail-closed guard for the live memory path.

    Accepts only a genuine production :class:`MemoryBusResult`. A reference
    result, any object carrying reference provenance, or any other type is
    rejected. This is the single chokepoint a live consumer must call before
    trusting bus evidence.
    """

    if isinstance(result, ReferenceMemoryBusResult):
        raise ReferenceProvenanceError(
            "reference (NON-LIVE) evidence must not enter the live memory path"
        )
    if not isinstance(result, MemoryBusResult):
        raise ReferenceProvenanceError(
            f"expected an authenticated MemoryBusResult, got {type(result).__name__}"
        )
    # Defensive: reject a forged/copied object whose provenance was mutated.
    if result.source != "live" or result.is_live is not True:
        raise ReferenceProvenanceError("result provenance is not live")
    return result


class PromotionCandidate(BaseModel):
    """A claim the runtime proposes for Brain-governed promotion.

    This is a request, not a write: ``runtime_authorized`` is fixed False,
    exactly as :class:`~youtab_runtime.contracts.EffectProposal`. The runtime
    never validates, promotes, supersedes canonical claims, or shares across
    agents without Simorgh/Gateway authorization.
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
    """The typed contract an AUTHENTICATED live Simorgh client implements.

    A live client returns :class:`MemoryBusResult`. The reference bus deliberately
    does NOT satisfy this protocol's ``query`` return type — it returns
    :class:`ReferenceMemoryBusResult` — which keeps reference evidence out of any
    slot typed for a live client.
    """

    @property
    def is_live(self) -> Literal[True]: ...

    def query(self, request: MemoryQuery) -> MemoryBusResult: ...

    def store_candidate(self, candidate: PromotionCandidate) -> str: ...

    def feedback(self, memory_id: str, *, scope: MemoryScope, helpful: bool) -> None: ...

    def supersede(self, memory_id: str, *, scope: MemoryScope, by: str) -> None: ...

    def erase(self, memory_id: str, *, scope: MemoryScope, reason: str) -> None: ...


class ReferenceMemoryBus:
    """Deterministic, in-process, NON-LIVE MemoryBus for Runtime tests.

    It enforces the same scope discipline a live bus must: a query only ever
    sees claims stored under an exactly-matching scope partition; there is no
    cross-tenant, cross-workspace or cross-purpose bleed. It NEVER validates,
    promotes, or shares a canonical claim: promotion candidates are queued and
    left PENDING. Every result it returns is a :class:`ReferenceMemoryBusResult`.
    """

    is_live: Literal[False] = False

    def __init__(self) -> None:
        self._store: dict[str, dict[str, MemoryClaim]] = {}
        self._pending: dict[str, PromotionCandidate] = {}

    def seed_validated(self, claim: MemoryClaim) -> None:
        """Test helper: place an already-VALIDATED claim into a scope partition."""

        if claim.status is not ClaimStatus.VALIDATED:
            raise ValueError("reference bus only seeds VALIDATED claims for retrieval")
        self._store.setdefault(claim.scope.partition_key(), {})[claim.memory_id] = claim

    def query(self, request: MemoryQuery) -> ReferenceMemoryBusResult:
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
        return ReferenceMemoryBusResult(results=tuple(hits[: request.limit]))

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


def degraded_result(reason: str) -> MemoryBusResult:
    """Build an explicit degraded PRODUCTION result (retrieval unavailable).

    Degraded mode is a live-path concept: it tells the caller authoritative
    retrieval failed, with no silent tenant fallback. It is intentionally a
    :class:`MemoryBusResult` (not reference) because only a live consumer acts on it.
    """

    return MemoryBusResult(degraded=True, degraded_reason=reason, results=())
