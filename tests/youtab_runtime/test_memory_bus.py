from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    ClaimStatus,
    MemoryBusResult,
    MemoryClaim,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    PromotionCandidate,
    ReferenceMemoryBus,
    ReferenceMemoryBusResult,
    ReferenceProvenanceError,
    ScopeAdmission,
    TrustLevel,
    consume_for_live,
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


def test_reference_bus_returns_reference_result_type() -> None:
    bus = ReferenceMemoryBus()
    assert bus.is_live is False
    scope = _scope()
    bus.seed_validated(_validated(scope, "acme prefers annual billing", MemoryType.SEMANTIC))
    result = bus.query(MemoryQuery(scope=scope, text="billing"))
    assert isinstance(result, ReferenceMemoryBusResult)
    assert not isinstance(result, MemoryBusResult)
    assert result.source == "reference"
    assert result.is_live is False
    assert len(result.results) == 1
    assert result.results[0].citation.startswith("reference:")


# ---- item 8: reference evidence can NEVER enter the live path -------------

def test_consume_for_live_rejects_reference_result() -> None:
    bus = ReferenceMemoryBus()
    scope = _scope()
    bus.seed_validated(_validated(scope, "fact", MemoryType.SEMANTIC))
    ref = bus.query(MemoryQuery(scope=scope, text="fact"))
    with pytest.raises(ReferenceProvenanceError):
        consume_for_live(ref)


def test_consume_for_live_rejects_foreign_and_none() -> None:
    with pytest.raises(ReferenceProvenanceError):
        consume_for_live(object())
    with pytest.raises(ReferenceProvenanceError):
        consume_for_live(None)
    with pytest.raises(ReferenceProvenanceError):
        consume_for_live({"source": "live", "is_live": True, "results": []})


def test_reference_result_cannot_validate_as_production_result() -> None:
    ref = ReferenceMemoryBusResult()
    # A reference payload must not validate as a production MemoryBusResult:
    # distinct schema_version literal + frozen/extra-forbid make it non-coercible.
    with pytest.raises(Exception):
        MemoryBusResult.model_validate(ref.model_dump())


def test_consume_for_live_accepts_genuine_production_result() -> None:
    live = MemoryBusResult(results=())
    assert consume_for_live(live) is live
    assert live.source == "live" and live.is_live is True


def test_tampered_live_provenance_fails_closed() -> None:
    # source/is_live are Literal-typed, so a mutated copy cannot even be built;
    # attempting to forge a live stamp on reference data is rejected at validation.
    with pytest.raises(Exception):
        MemoryBusResult(source="reference")  # type: ignore[arg-type]
    with pytest.raises(Exception):
        MemoryBusResult(is_live=False)  # type: ignore[arg-type]


# ---- scope isolation (item 9 negative coverage) --------------------------

def test_query_is_scope_isolated_across_tenant_and_workspace() -> None:
    bus = ReferenceMemoryBus()
    alpha = _scope(tenant="tenant-alpha")
    beta = _scope(tenant="tenant-beta")
    other_ws = _scope(tenant="tenant-alpha", workspace="ws-eng")
    bus.seed_validated(_validated(alpha, "secret alpha fact", MemoryType.SEMANTIC))
    assert bus.query(MemoryQuery(scope=beta, text="alpha")).results == ()
    assert bus.query(MemoryQuery(scope=other_ws, text="alpha")).results == ()
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
    with pytest.raises(ValueError):
        bus.seed_validated(pending)


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
    assert bus.query(MemoryQuery(scope=scope, text="candidate")).results == ()


def test_supersede_is_scope_bound() -> None:
    bus = ReferenceMemoryBus()
    scope = _scope()
    other = _scope(tenant="tenant-beta")
    claim = _validated(scope, "fact to supersede", MemoryType.SEMANTIC)
    bus.seed_validated(claim)
    with pytest.raises(KeyError):
        bus.supersede(claim.memory_id, scope=other, by="mem-00000009")
    bus.supersede(claim.memory_id, scope=scope, by="mem-00000009")
    assert bus.query(MemoryQuery(scope=scope, text="fact")).results == ()


def test_erase_removes_from_scope() -> None:
    bus = ReferenceMemoryBus()
    scope = _scope()
    claim = _validated(scope, "erase me", MemoryType.SEMANTIC)
    bus.seed_validated(claim)
    bus.erase(claim.memory_id, scope=scope, reason="gdpr")
    assert bus.query(MemoryQuery(scope=scope, text="erase")).results == ()


def test_degraded_result_is_live_typed_and_empty() -> None:
    d = degraded_result("brain unreachable")
    assert isinstance(d, MemoryBusResult)
    assert d.degraded is True
    assert d.degraded_reason == "brain unreachable"
    assert d.results == ()
    assert d.is_live is True
    assert consume_for_live(d) is d  # a degraded live result is still live evidence
