from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    Classification,
    MemoryRouter,
    MemoryScope,
    MemoryType,
    MemoryWriteIntent,
    RouteOwner,
    ScopeAdmission,
    TrustLevel,
)

from .helpers import keypair, signed_envelope


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


def _intent(**overrides: object) -> MemoryWriteIntent:
    base: dict[str, object] = {
        "scope": _scope(),
        "memory_type": MemoryType.WORKING,
        "trust_level": TrustLevel.SYSTEM_ATTRIBUTED,
        "classification": Classification.INTERNAL,
        "content_length": 100,
    }
    base.update(overrides)
    return MemoryWriteIntent(**base)


ROUTER = MemoryRouter()


def test_working_state_is_runtime_autonomous() -> None:
    d = ROUTER.route(_intent(memory_type=MemoryType.WORKING))
    assert d.owner is RouteOwner.RUNTIME_WORKING
    assert d.allow_runtime_write and not d.requires_effect_gate


def test_secret_material_is_always_rejected() -> None:
    d = ROUTER.route(_intent(memory_type=MemoryType.WORKING, contains_secret_material=True))
    assert d.owner is RouteOwner.REJECTED
    assert not d.allow_runtime_write
    d2 = ROUTER.route(_intent(classification=Classification.SECRET))
    assert d2.owner is RouteOwner.REJECTED


def test_effect_truth_routes_to_ledger_not_memory() -> None:
    d = ROUTER.route(_intent(memory_type=MemoryType.EFFECT_TRUTH))
    assert d.owner is RouteOwner.EFFECT_LEDGER
    assert not d.allow_runtime_write


def test_semantic_promotion_requires_effect_gate() -> None:
    d = ROUTER.route(_intent(memory_type=MemoryType.SEMANTIC))
    assert d.owner is RouteOwner.BRAIN_MEMORYBUS
    assert d.requires_effect_gate and not d.allow_runtime_write
    d2 = ROUTER.route(_intent(memory_type=MemoryType.WORKING, is_promotion_to_sovereign=True))
    assert d2.owner is RouteOwner.BRAIN_MEMORYBUS
    assert d2.requires_effect_gate


def test_authoritative_ref_allows_reference_but_not_raw_payload() -> None:
    ok = ROUTER.route(_intent(memory_type=MemoryType.AUTHORITATIVE_REF))
    assert ok.owner is RouteOwner.RUNTIME_WORKING and ok.allow_runtime_write
    raw = ROUTER.route(
        _intent(
            memory_type=MemoryType.AUTHORITATIVE_REF,
            carries_source_of_truth_payload=True,
        )
    )
    assert raw.owner is RouteOwner.SOURCE_SYSTEM
    assert not raw.allow_runtime_write


def test_capsule_accepts_small_trusted_public_content() -> None:
    d = ROUTER.route(
        _intent(
            memory_type=MemoryType.CAPSULE,
            trust_level=TrustLevel.USER_ASSERTED,
            classification=Classification.INTERNAL,
            content_length=500,
        )
    )
    assert d.owner is RouteOwner.RUNTIME_CAPSULE and d.allow_runtime_write


@pytest.mark.parametrize(
    "overrides",
    [
        {"content_length": 5_000},  # too big
        {"trust_level": TrustLevel.AI_INFERRED},  # not trusted
        {"trust_level": TrustLevel.UNTRUSTED},
        {"classification": Classification.CONFIDENTIAL},  # too sensitive
        {"classification": Classification.RESTRICTED},
    ],
)
def test_capsule_rejects_unbounded_untrusted_or_sensitive(overrides: dict) -> None:
    base = {
        "memory_type": MemoryType.CAPSULE,
        "trust_level": TrustLevel.USER_ASSERTED,
        "classification": Classification.INTERNAL,
        "content_length": 500,
    }
    base.update(overrides)
    d = ROUTER.route(_intent(**base))
    assert d.owner is RouteOwner.REJECTED
    assert not d.allow_runtime_write


def test_episodic_is_scoped_autonomous() -> None:
    d = ROUTER.route(_intent(memory_type=MemoryType.EPISODIC))
    assert d.owner is RouteOwner.EPISODIC_STORE and d.allow_runtime_write


def test_procedural_requires_gate() -> None:
    d = ROUTER.route(_intent(memory_type=MemoryType.PROCEDURAL))
    assert d.owner is RouteOwner.BRAIN_MEMORYBUS and d.requires_effect_gate


def test_router_is_pure_and_stateless() -> None:
    intent = _intent(memory_type=MemoryType.WORKING)
    assert ROUTER.route(intent) == ROUTER.route(intent)
