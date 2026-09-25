from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from youtab_runtime.memory import (
    Classification,
    ClaimStatus,
    MemoryClaim,
    MemoryScope,
    MemoryType,
    TrustLevel,
)
from youtab_runtime.memory.claim import content_digest

from .helpers import keypair, signed_envelope
from youtab_runtime.memory import ScopeAdmission


def _scope() -> MemoryScope:
    private, _ = keypair()
    envelope = signed_envelope(private)
    return MemoryScope.from_admission(
        envelope,
        ScopeAdmission(
            organization_id="org-acme",
            workspace_id="ws-sales",
            agent_id="agent-01",
            run_id="run-abc123",
        ),
    )


def test_new_claim_is_pending_with_computed_hash() -> None:
    claim = MemoryClaim.new(
        scope=_scope(),
        memory_type=MemoryType.EPISODIC,
        content="customer asked about pricing",
        source_type="conversation",
        trust_level=TrustLevel.SYSTEM_ATTRIBUTED,
    )
    assert claim.status is ClaimStatus.PENDING
    assert claim.content_hash == content_digest("customer asked about pricing")
    assert not claim.is_retrievable_for_production()


def test_content_hash_must_match_content() -> None:
    with pytest.raises(ValidationError):
        MemoryClaim(
            memory_id="mem-00000001",
            scope=_scope(),
            memory_type=MemoryType.EPISODIC,
            content="hello",
            content_hash="0" * 64,
            source_type="test",
            trust_level=TrustLevel.SYSTEM_ATTRIBUTED,
            valid_from=datetime.now(UTC),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )


def test_exactly_one_of_content_or_ref() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):  # both set
        MemoryClaim(
            memory_id="mem-00000001",
            scope=_scope(),
            memory_type=MemoryType.EPISODIC,
            content="x",
            content_ref="ref://y",
            content_hash=content_digest("x"),
            source_type="test",
            trust_level=TrustLevel.SYSTEM_ATTRIBUTED,
            valid_from=now,
            created_at=now,
            updated_at=now,
        )
    with pytest.raises(ValidationError):  # neither set
        MemoryClaim(
            memory_id="mem-00000001",
            scope=_scope(),
            memory_type=MemoryType.EPISODIC,
            content=None,
            content_ref=None,
            content_hash="0" * 64,
            source_type="test",
            trust_level=TrustLevel.SYSTEM_ATTRIBUTED,
            valid_from=now,
            created_at=now,
            updated_at=now,
        )


def test_ai_inferred_cannot_be_born_validated() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        MemoryClaim(
            memory_id="mem-00000001",
            scope=_scope(),
            memory_type=MemoryType.SEMANTIC,
            content="the sky is green",
            content_hash=content_digest("the sky is green"),
            source_type="model",
            trust_level=TrustLevel.AI_INFERRED,
            status=ClaimStatus.VALIDATED,
            valid_from=now,
            created_at=now,
            updated_at=now,
        )


def test_only_validated_claims_are_retrievable() -> None:
    claim = MemoryClaim.new(
        scope=_scope(),
        memory_type=MemoryType.SEMANTIC,
        content="acme HQ is in Utrecht",
        source_type="verified",
        trust_level=TrustLevel.VALIDATED,
    )
    assert not claim.is_retrievable_for_production()
    validated = claim.with_status(ClaimStatus.VALIDATED)
    assert validated.is_retrievable_for_production()
    assert validated.updated_at >= claim.updated_at


def test_status_transitions_are_enforced() -> None:
    claim = MemoryClaim.new(
        scope=_scope(),
        memory_type=MemoryType.SEMANTIC,
        content="fact",
        source_type="verified",
        trust_level=TrustLevel.VALIDATED,
    )
    validated = claim.with_status(ClaimStatus.VALIDATED)
    superseded = validated.with_status(ClaimStatus.SUPERSEDED, supersedes_id="mem-00000009")
    assert superseded.status is ClaimStatus.SUPERSEDED
    assert superseded.supersedes_id == "mem-00000009"
    # terminal states cannot move further
    with pytest.raises(ValueError):
        superseded.with_status(ClaimStatus.VALIDATED)
    rejected = claim.with_status(ClaimStatus.REJECTED)
    with pytest.raises(ValueError):
        rejected.with_status(ClaimStatus.VALIDATED)


def test_valid_until_must_follow_valid_from() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        MemoryClaim(
            memory_id="mem-00000001",
            scope=_scope(),
            memory_type=MemoryType.EPISODIC,
            content="x",
            content_hash=content_digest("x"),
            source_type="test",
            trust_level=TrustLevel.SYSTEM_ATTRIBUTED,
            valid_from=now,
            valid_until=now - timedelta(seconds=1),
            created_at=now,
            updated_at=now,
        )


def test_naive_datetime_is_rejected() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValidationError):
        MemoryClaim(
            memory_id="mem-00000001",
            scope=_scope(),
            memory_type=MemoryType.EPISODIC,
            content="x",
            content_hash=content_digest("x"),
            source_type="test",
            trust_level=TrustLevel.SYSTEM_ATTRIBUTED,
            valid_from=naive,
            created_at=naive,
            updated_at=naive,
        )
