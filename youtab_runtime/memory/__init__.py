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

from .budget import (
    BudgetAllocation,
    BudgetInputs,
    DeploymentClass,
    TaskClass,
    allocate,
    estimate_tokens,
    legacy_capsule_fits,
    selective_retrieval_tokens,
)
from .cache import (
    CacheDisabled,
    CacheFull,
    EncryptedScopedCache,
    open_cache,
)
from .keystore import (
    DpapiKeyStore,
    KeyStore,
    KeyStoreUnavailable,
    ProvisionedKeyStore,
    RevokedKeyError,
    default_keystore,
)
from .bus import (
    MemoryBusClient,
    MemoryBusResult,
    MemoryQuery,
    PromotionCandidate,
    ReferenceMemoryBus,
    ReferenceMemoryBusResult,
    ReferenceProvenanceError,
    RetrievedMemory,
    consume_for_live,
    degraded_result,
)
from .claim import (
    Classification,
    ClaimStatus,
    MemoryClaim,
    MemoryType,
    TrustLevel,
    TrustSource,
)
from .outbox import (
    MemoryOutbox,
    OutboxEvent,
    OutboxEventType,
    OutboxStatus,
    ScopeRevoked,
    new_event,
)
from .outbox_sqlite import DigestMismatch, SqliteOutbox
from .placement import (
    PlacementError,
    PlacementVerifier,
    SignedScopePlacement,
    VerifiedPlacement,
)
from .retrieval import (
    RetrievalConfig,
    RetrievalOutcome,
    RetrievalPipeline,
    RetrievalResult,
)
from .router import MemoryRouter, MemoryRoutingDecision, MemoryWriteIntent, RouteOwner
from .scope import MemoryScope, ScopeAdmission
from .simorgh_client import (
    AuthenticatedSimorghClient,
    MemoryUnavailable,
    ScopeBinding,
    ScopeNotSigned,
    SimorghClientConfig,
    SimorghTransport,
    request_digest,
    response_digest,
)
from .tokenizer import (
    CompactionSummary,
    ExactTokenCounter,
    HeuristicTokenCounter,
    ModelCapability,
    PromptAccounting,
    PromptComponents,
    TokenCounter,
    account,
    needs_compaction,
    select_counter,
)

__all__ = [
    "AuthenticatedSimorghClient",
    "BudgetAllocation",
    "BudgetInputs",
    "CacheDisabled",
    "CacheFull",
    "Classification",
    "ClaimStatus",
    "CompactionSummary",
    "DeploymentClass",
    "DigestMismatch",
    "DpapiKeyStore",
    "EncryptedScopedCache",
    "ExactTokenCounter",
    "HeuristicTokenCounter",
    "KeyStore",
    "KeyStoreUnavailable",
    "MemoryUnavailable",
    "ModelCapability",
    "ProvisionedKeyStore",
    "RevokedKeyError",
    "ScopeBinding",
    "ScopeNotSigned",
    "SimorghClientConfig",
    "SimorghTransport",
    "PromptAccounting",
    "PromptComponents",
    "TokenCounter",
    "MemoryBusClient",
    "MemoryBusResult",
    "MemoryClaim",
    "MemoryOutbox",
    "MemoryQuery",
    "MemoryRouter",
    "MemoryRoutingDecision",
    "MemoryScope",
    "MemoryType",
    "MemoryWriteIntent",
    "OutboxEvent",
    "OutboxEventType",
    "OutboxStatus",
    "PlacementError",
    "PlacementVerifier",
    "PromotionCandidate",
    "RetrievalConfig",
    "RetrievalOutcome",
    "RetrievalPipeline",
    "RetrievalResult",
    "SignedScopePlacement",
    "VerifiedPlacement",
    "ReferenceMemoryBus",
    "ReferenceMemoryBusResult",
    "ReferenceProvenanceError",
    "RetrievedMemory",
    "RouteOwner",
    "ScopeAdmission",
    "ScopeRevoked",
    "SqliteOutbox",
    "TaskClass",
    "TrustLevel",
    "TrustSource",
    "account",
    "allocate",
    "consume_for_live",
    "default_keystore",
    "degraded_result",
    "estimate_tokens",
    "legacy_capsule_fits",
    "needs_compaction",
    "new_event",
    "open_cache",
    "request_digest",
    "response_digest",
    "select_counter",
    "selective_retrieval_tokens",
]
