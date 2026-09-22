"""The versioned memory-claim TRANSPORT ENVELOPE (``youtab.memory-claim.v1``).

A :class:`MemoryClaim` is a transport/DTO record shape for exchanging a memory
item between the Runtime and the memory authority — it is NOT a competing
canonical authority and NOT a second organizational memory database.

**Simorgh / One Brain is the memory authority.** The Runtime uses this envelope
to *carry* a candidate or a retrieved item; it does not, by holding a claim,
gain the right to: validate enterprise truth, promote AI inference to canonical
memory, supersede a canonical Simorgh claim, or share across agents/workspaces
without Simorgh/Gateway authorization. Those transitions are performed only by
Simorgh's governed pipeline; the ``status``/``trust_level`` fields here are
transport annotations, and the local ``with_status`` transition helper is for
Runtime-local candidate bookkeeping and reference tests, never an authoritative
promotion.

The model enforces shape and self-consistency: exactly one of content/ref, a
matching content hash, timezone-aware times, and the invariants that only a
``VALIDATED`` envelope is retrievable for production and an ``AI_INFERRED``
envelope cannot be born ``VALIDATED``. Trust, provenance and timestamps are
intended to be system-attributed at admission. This aligns with the proposed
cognitive-growth memory model (ADR-0002, still PROPOSED — not ratified).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .scope import MemoryScope


class MemoryType(StrEnum):
    """The seven state categories the classification keeps separate."""

    CAPSULE = "capsule"  # always-injected rules/profile (bounded, trusted)
    WORKING = "working"  # active task/checkpoint state
    EPISODIC = "episodic"  # time-ordered observations/decisions
    SEMANTIC = "semantic"  # validated cross-session facts/relations
    PROCEDURAL = "procedural"  # skills/scripts/templates/methods
    AUTHORITATIVE_REF = "authoritative_ref"  # id/ref into a source system only
    EFFECT_TRUTH = "effect_truth"  # run journal / effect ledger / receipts


class TrustLevel(StrEnum):
    UNTRUSTED = "untrusted"  # e.g. retrieved web / tool output, unvalidated
    AI_INFERRED = "ai_inferred"  # produced by a model; cannot self-promote
    USER_ASSERTED = "user_asserted"  # stated by an authenticated principal
    SYSTEM_ATTRIBUTED = "system_attributed"  # attributed by admission/platform
    VALIDATED = "validated"  # passed governed verification


class ClaimStatus(StrEnum):
    PENDING = "pending"
    VALIDATED = "validated"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class Classification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"  # never permitted in memory content (routed out)
    RESTRICTED = "restricted"


_VALID_TRANSITIONS: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.PENDING: frozenset(
        {ClaimStatus.VALIDATED, ClaimStatus.REJECTED, ClaimStatus.SUPERSEDED}
    ),
    ClaimStatus.VALIDATED: frozenset({ClaimStatus.SUPERSEDED}),
    ClaimStatus.SUPERSEDED: frozenset(),
    ClaimStatus.REJECTED: frozenset(),
}


def content_digest(content: str) -> str:
    """Deterministic sha256 of UTF-8 content, matching the effect-ledger idiom."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class MemoryClaim(BaseModel):
    """One scoped, provenance-bearing memory record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["youtab.memory-claim.v1"] = "youtab.memory-claim.v1"
    memory_id: str = Field(min_length=8, max_length=128)
    scope: MemoryScope
    memory_type: MemoryType
    content: str | None = Field(default=None, max_length=262_144)
    content_ref: str | None = Field(default=None, max_length=1024)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_type: str = Field(min_length=1, max_length=64)
    source_ref: str | None = Field(default=None, max_length=1024)
    trust_level: TrustLevel
    status: ClaimStatus = ClaimStatus.PENDING
    valid_from: datetime
    valid_until: datetime | None = None
    supersedes_id: str | None = Field(default=None, min_length=8, max_length=128)
    provenance: tuple[str, ...] = ()
    classification: Classification = Classification.INTERNAL
    retention_policy: str = Field(default="default", max_length=64)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _coherent(self) -> "MemoryClaim":
        if (self.content is None) == (self.content_ref is None):
            raise ValueError("exactly one of content or content_ref must be set")
        if self.content is not None and content_digest(self.content) != self.content_hash:
            raise ValueError("content_hash does not match content")
        for name, value in (
            ("valid_from", self.valid_from),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.valid_until is not None:
            if self.valid_until.tzinfo is None:
                raise ValueError("valid_until must be timezone-aware")
            if self.valid_until <= self.valid_from:
                raise ValueError("valid_until must follow valid_from")
        # A validated fact may not carry an unvetted trust level.
        if self.status is ClaimStatus.VALIDATED and self.trust_level in {
            TrustLevel.UNTRUSTED,
            TrustLevel.AI_INFERRED,
        }:
            raise ValueError(
                "a VALIDATED claim cannot have untrusted/ai_inferred trust_level"
            )
        return self

    @classmethod
    def new(
        cls,
        *,
        scope: MemoryScope,
        memory_type: MemoryType,
        content: str,
        source_type: str,
        trust_level: TrustLevel,
        classification: Classification = Classification.INTERNAL,
        source_ref: str | None = None,
        provenance: tuple[str, ...] = (),
        retention_policy: str = "default",
        now: datetime | None = None,
    ) -> "MemoryClaim":
        """Mint a PENDING inline claim with a computed hash and timestamps."""

        current = (now or datetime.now(UTC)).astimezone(UTC)
        return cls(
            memory_id=f"mem-{uuid4().hex}",
            scope=scope,
            memory_type=memory_type,
            content=content,
            content_hash=content_digest(content),
            source_type=source_type,
            source_ref=source_ref,
            trust_level=trust_level,
            status=ClaimStatus.PENDING,
            valid_from=current,
            provenance=provenance,
            classification=classification,
            retention_policy=retention_policy,
            created_at=current,
            updated_at=current,
        )

    def is_retrievable_for_production(self) -> bool:
        """Only validated, live, non-superseded claims feed production retrieval."""

        return self.status is ClaimStatus.VALIDATED

    def with_status(
        self,
        status: ClaimStatus,
        *,
        supersedes_id: str | None = None,
        now: datetime | None = None,
    ) -> "MemoryClaim":
        """Return a new claim advanced to ``status`` if the transition is legal."""

        allowed = _VALID_TRANSITIONS[self.status]
        if status not in allowed:
            raise ValueError(f"illegal status transition {self.status} -> {status}")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        return self.model_copy(
            update={
                "status": status,
                "updated_at": current,
                "supersedes_id": supersedes_id
                if supersedes_id is not None
                else self.supersedes_id,
            }
        )
