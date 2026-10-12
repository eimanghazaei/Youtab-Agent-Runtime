"""Scoped retrieval pipeline — implemented as a DEFAULT-DISABLED adapter.

Steps: authenticate scope -> verify signed placement -> query provider -> reject
reference/live confusion -> filter by lifecycle + authorization -> rank/select ->
token-account -> inject only the bounded slice -> reference excluded large
artifacts -> record retrieval digest + convergence cursor.

There is NO silent fallback from LIVE to reference or built-in memory. When the
pipeline is disabled or a signed placement is unavailable/invalid, it returns an
explicit ``LIVE_MEMORY_UNAVAILABLE`` and retrieves nothing.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from youtab_runtime.contracts import BrainCommandEnvelope

from .bus import MemoryQuery, RetrievedMemory, consume_for_live
from .placement import PlacementError, PlacementVerifier, SignedScopePlacement
from .scope import MemoryScope
from .simorgh_client import AuthenticatedSimorghClient, MemoryUnavailable, ScopeBinding
from .tokenizer import TokenCounter

_ALL_DIMS = frozenset(
    {"tenant_id", "organization_id", "workspace_id", "principal_id", "agent_id", "run_id", "purpose"}
)


class RetrievalOutcome(StrEnum):
    OK = "ok"
    DISABLED = "disabled"
    LIVE_MEMORY_UNAVAILABLE = "live_memory_unavailable"


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    retrieval_token_budget: int = Field(default=4000, ge=1)
    artifact_ref_token_threshold: int = Field(default=1000, ge=1)


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: RetrievalOutcome
    injected: tuple[RetrievedMemory, ...] = ()
    references: tuple[str, ...] = ()  # excluded large artifacts, by citation
    retrieval_digest: str | None = None
    convergence_cursor: int = 0
    reason: str | None = None


class RetrievalPipeline:
    def __init__(
        self,
        config: RetrievalConfig,
        client: AuthenticatedSimorghClient,
        verifier: PlacementVerifier,
        counter: TokenCounter,
    ) -> None:
        self._config = config
        self._client = client
        self._verifier = verifier
        self._counter = counter

    def retrieve(
        self,
        request: MemoryQuery,
        placement: SignedScopePlacement,
        envelope: BrainCommandEnvelope,
    ) -> RetrievalResult:
        # 1. disabled -> explicit unavailable, no fallback.
        if not self._config.enabled:
            return RetrievalResult(
                outcome=RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE,
                reason="retrieval pipeline disabled",
            )
        # 2. verify signed placement; fail closed to LIVE_MEMORY_UNAVAILABLE.
        try:
            verified = self._verifier.verify(placement, envelope=envelope)
        except PlacementError as exc:
            return RetrievalResult(
                outcome=RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE,
                reason=f"placement not verified: {exc}",
            )
        if request.scope != verified.scope:
            return RetrievalResult(
                outcome=RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE,
                reason="request scope does not match verified placement",
            )
        # 3. query provider (fail closed on unavailable).
        binding = ScopeBinding(scope=verified.scope, signed_dimensions=_ALL_DIMS)
        try:
            result = self._client.query(request, binding)
        except MemoryUnavailable as exc:
            return RetrievalResult(
                outcome=RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE, reason=str(exc)
            )
        # 4. reject reference/live confusion; 5. lifecycle + authorization filter.
        live = consume_for_live(result)
        if live.degraded:
            return RetrievalResult(
                outcome=RetrievalOutcome.LIVE_MEMORY_UNAVAILABLE,
                reason=live.degraded_reason or "degraded",
            )
        eligible = [
            r
            for r in live.results
            if r.claim.enters_validated_projection() and r.claim.scope == verified.scope
        ]
        # 6. rank by score desc, stable by memory_id.
        eligible.sort(key=lambda r: (-r.score, r.claim.memory_id))
        # 7-8. token-account: inject bounded slice; reference oversized/overflow items.
        injected: list[RetrievedMemory] = []
        references: list[str] = []
        used = 0
        for r in eligible:
            body = r.claim.content or r.claim.content_ref or ""
            tokens = self._counter.count(body)
            if tokens > self._config.artifact_ref_token_threshold:
                references.append(r.citation)  # large artifact: reference, don't inline
                continue
            if used + tokens > self._config.retrieval_token_budget:
                references.append(r.citation)  # over budget: reference, don't inline
                continue
            injected.append(r)
            used += tokens
        # 9. retrieval digest + convergence cursor.
        digest = hashlib.sha256(
            "|".join(r.claim.memory_id for r in injected).encode("utf-8")
        ).hexdigest()
        return RetrievalResult(
            outcome=RetrievalOutcome.OK,
            injected=tuple(injected),
            references=tuple(references),
            retrieval_digest=digest,
            convergence_cursor=len(injected),
        )
