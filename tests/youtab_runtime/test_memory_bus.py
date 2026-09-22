from __future__ import annotations

from youtab_runtime.memory import (
    ClaimStatus,
    MemoryBusClient,
    MemoryClaim,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    PromotionCandidate,
    ReferenceMemoryBus,
    ScopeAdmission,
    TrustLevel,
    degraded_result,
)

from .helpers import keypair, signed_envelope


def _scope(tenant: str = "tenant-alpha", workspace: str = "ws-sales") -> MemoryScope:
    private, _ = keypair()
    nonce = "nonce-" + tenant.encode("utf-8").hex()[:16].ljust(16, "0")
    envelope = signed_envelope(private, tenant_id=tenant, nonce=nonce)
    return MemoryScope.from_admission(
        envelope,
        ScopeAdmission(
            organization_id="org-acme",
            workspace_id=workspace,
            agent_id="agent-01",
            run_id="run-abc123",
        ),
    )


def _validated(scope: MemoryScope, content: str, mtype: MemoryType) -> MemoryClaim:
    return MemoryClaim.new(
        scope=scope,
        memory_type=mtype,
        content=content,
        source_type="verified",
        trust_level=TrustLevel.VALIDATED,
    ).with_status(ClaimStatus.VALIDATED)


def test_reference_bus_satisfies_protocol_and_is_not_live() -> None:
    bus = ReferenceMemoryBus()
    assert isinstance(bus, MemoryBusClient)
    assert bus.source == "reference"
    assert bus.is_live is False


def test_query_result_is_stamped_non_live() -> None:
    bus = ReferenceMemoryBus()
    scope = _scope()
    bus.seed_validated(_validated(scope, "acme prefers annual billing", MemoryType.SEMANTIC))
    result = bus.query(MemoryQuery(scope=scope, text="billing"))
    assert result.source == "reference"
    assert result.is_live is False
    assert not result.degraded
    assert len(result.results) == 1
    assert result.results[0].citation.startswith("reference:")


def test_query_is_scope_isolated_across_tenant_and_workspace() -> None:
    bus = ReferenceMemoryBus()
    alpha = _scope(tenant="tenant-alpha")
    beta = _scope(tenant="tenant-beta")
    other_ws = _scope(tenant="tenant-alpha", workspace="ws-eng")
    bus.seed_validated(_validated(alpha, "secret alpha fact", MemoryType.SEMANTIC))
    # cross-tenant: no bleed
    assert bus.query(MemoryQuery(scope=beta, text="alpha")).results == ()
    # cross-workspace, same tenant: no bleed
    assert bus.query(MemoryQuery(scope=other_ws, text="alpha")).results == ()
    # exact scope: visible
    assert len(bus.query(MemoryQuery(scope=alpha, text="alpha")).results) == 1


def test_only_validated_claims_are_returned() -> None:
    bus = ReferenceMemoryBus()
    scope = _scope()
    pending = MemoryClaim.new(
        scope=scope,
        memory_type=MemoryType.SEMANTIC,
        content="unvetted claim",
        source_type="model",
        trust_level=TrustLevel.AI_INFERRED,
    )
    # seed_validated refuses non-validated claims outright
    try:
        bus.seed_validated(pending)
        raise AssertionError("expected refusal")
    except ValueError:
        pass


def test_store_candidate_queues_but_never_validates() -> None:
    bus = ReferenceMemoryBus()
    scope = _scope()
    claim = MemoryClaim.new(
        scope=scope,
        memory_type=MemoryType.SEMANTIC,
        content="candidate fact",
        source_type="model",
        trust_level=TrustLevel.AI_INFERRED,
    )
    candidate = PromotionCandidate(claim=claim, justification="observed twice")
    assert candidate.runtime_authorized is False
    bus.store_candidate(candidate)
    assert bus.pending_candidates() == (candidate,)
    # a queued candidate is NOT retrievable — promotion is not a write
    assert bus.query(MemoryQuery(scope=scope, text="candidate")).results == ()


def test_supersede_is_scope_bound() -> None:
    bus = ReferenceMemoryBus()
    scope = _scope()
    other = _scope(tenant="tenant-beta")
    claim = _validated(scope, "fact to supersede", MemoryType.SEMANTIC)
    bus.seed_validated(claim)
    # cannot supersede from a foreign scope
    try:
        bus.supersede(claim.memory_id, scope=other, by="mem-00000009")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    bus.supersede(claim.memory_id, scope=scope, by="mem-00000009")
    assert bus.query(MemoryQuery(scope=scope, text="fact")).results == ()


def test_erase_removes_from_scope() -> None:
    bus = ReferenceMemoryBus()
    scope = _scope()
    claim = _validated(scope, "erase me", MemoryType.SEMANTIC)
    bus.seed_validated(claim)
    bus.erase(claim.memory_id, scope=scope, reason="gdpr")
    assert bus.query(MemoryQuery(scope=scope, text="erase")).results == ()


def test_degraded_result_helper_is_explicit_and_empty() -> None:
    d = degraded_result("brain unreachable")
    assert d.degraded is True
    assert d.degraded_reason == "brain unreachable"
    assert d.results == ()
    assert d.is_live is True
