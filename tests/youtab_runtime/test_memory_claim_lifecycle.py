from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    ClaimStatus,
    MemoryClaim,
    MemoryScope,
    MemoryType,
    ScopeAdmission,
    TrustLevel,
    TrustSource,
)

from .helpers import keypair, signed_envelope


def _scope(tenant: str = "tenant-alpha") -> MemoryScope:
    private, _ = keypair()
    env = signed_envelope(private, tenant_id=tenant)
    return MemoryScope.from_admission(
        env, ScopeAdmission(organization_id="org-acme", workspace_id="ws-sales",
                            agent_id="agent-01", run_id="run-1"))


def _claim(scope, source=None, trust=TrustLevel.SYSTEM_ATTRIBUTED, content="a fact"):
    return MemoryClaim.new(scope=scope, memory_type=MemoryType.SEMANTIC, content=content,
                           source_type="test", trust_level=trust, trust_source=source)


def test_runtime_cannot_self_validate_ai_inferred() -> None:
    c = _claim(_scope(), source=TrustSource.AI_INFERRED, trust=TrustLevel.SYSTEM_ATTRIBUTED)
    with pytest.raises(ValueError):
        c.with_status(ClaimStatus.VALIDATED)


def test_admin_validated_source_may_validate() -> None:
    c = _claim(_scope(), source=TrustSource.ADMIN_VALIDATED, trust=TrustLevel.VALIDATED)
    v = c.with_status(ClaimStatus.VALIDATED)
    assert v.enters_validated_projection()


def test_tombstone_from_validated_and_terminal() -> None:
    c = _claim(_scope(), trust=TrustLevel.VALIDATED).with_status(ClaimStatus.VALIDATED)
    t = c.with_status(ClaimStatus.TOMBSTONED)
    assert t.status is ClaimStatus.TOMBSTONED
    assert not t.enters_validated_projection()
    with pytest.raises(ValueError):
        t.with_status(ClaimStatus.VALIDATED)  # terminal


def test_invalid_transition_rejected() -> None:
    c = _claim(_scope(), trust=TrustLevel.VALIDATED)
    v = c.with_status(ClaimStatus.VALIDATED)
    with pytest.raises(ValueError):
        v.with_status(ClaimStatus.PENDING)  # cannot go back


def test_replayed_validation_rejected() -> None:
    c = _claim(_scope(), trust=TrustLevel.VALIDATED)
    v = c.with_status(ClaimStatus.VALIDATED)
    with pytest.raises(ValueError):
        v.with_status(ClaimStatus.VALIDATED)  # VALIDATED -> VALIDATED illegal


def test_stale_superseded_claim_is_terminal_for_retrieval() -> None:
    c = _claim(_scope(), trust=TrustLevel.VALIDATED).with_status(ClaimStatus.VALIDATED)
    s = c.with_status(ClaimStatus.SUPERSEDED, supersedes_id="mem-00000009")
    assert not s.enters_validated_projection()
    with pytest.raises(ValueError):
        s.with_status(ClaimStatus.VALIDATED)


def test_supersession_preserves_provenance() -> None:
    scope = _scope()
    c = MemoryClaim.new(scope=scope, memory_type=MemoryType.SEMANTIC, content="x",
                        source_type="verified", trust_level=TrustLevel.VALIDATED,
                        provenance=("origin:conversation", "corroborated:2x"))
    v = c.with_status(ClaimStatus.VALIDATED)
    s = v.with_status(ClaimStatus.SUPERSEDED, supersedes_id="mem-00000009")
    assert s.provenance == ("origin:conversation", "corroborated:2x")


def test_foreign_scope_claim_is_a_distinct_record() -> None:
    a = _claim(_scope("tenant-alpha"), trust=TrustLevel.VALIDATED)
    b = _claim(_scope("tenant-beta"), trust=TrustLevel.VALIDATED)
    assert a.scope.partition_key() != b.scope.partition_key()


def test_copied_provenance_does_not_confer_validation() -> None:
    # Copying a validated claim's provenance onto an AI-inferred claim must NOT let it validate.
    validated = _claim(_scope(), trust=TrustLevel.VALIDATED)
    forged = MemoryClaim.new(
        scope=_scope(), memory_type=MemoryType.SEMANTIC, content="forged",
        source_type="model", trust_level=TrustLevel.SYSTEM_ATTRIBUTED,
        trust_source=TrustSource.AI_INFERRED, provenance=validated.provenance,
    )
    with pytest.raises(ValueError):
        forged.with_status(ClaimStatus.VALIDATED)
