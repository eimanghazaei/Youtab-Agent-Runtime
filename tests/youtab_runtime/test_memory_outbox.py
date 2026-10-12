from __future__ import annotations

import pytest

from youtab_runtime.memory import (
    MemoryOutbox,
    MemoryScope,
    OutboxEventType,
    OutboxStatus,
    ScopeAdmission,
    ScopeRevoked,
    new_event,
)

from .helpers import keypair, signed_envelope


def _scope(tenant: str = "tenant-alpha", ws: str = "ws-sales") -> MemoryScope:
    private, _ = keypair()
    nonce = "nonce-" + (tenant + ws).encode().hex()[:16].ljust(16, "0")
    env = signed_envelope(private, tenant_id=tenant, nonce=nonce)
    return MemoryScope.from_admission(
        env, ScopeAdmission(organization_id="org-acme", workspace_id=ws,
                            agent_id="agent-01", run_id="run-1"))


def _ev(scope, eid, key, cursor=1, **kw):
    return new_event(event_id=eid, scope=scope, event_type=OutboxEventType.STORE_CANDIDATE,
                     memory_id="mem-1", ordering_key=key, convergence_cursor=cursor, **kw)


def test_duplicate_delivery_is_deduped_by_event_id() -> None:
    box = MemoryOutbox()
    s = _scope()
    assert box.enqueue(_ev(s, "evt-00000001", "k1")) is True
    assert box.enqueue(_ev(s, "evt-00000001", "k1")) is False  # dupe
    assert box.count(OutboxStatus.PENDING) == 1


def test_crash_before_send_survives_restart() -> None:
    store: dict = {}
    box = MemoryOutbox(store)
    box.enqueue(_ev(_scope(), "evt-00000001", "k1"))
    # "crash": drop the object, rebuild over the SAME store
    box2 = MemoryOutbox(store)
    assert box2.count(OutboxStatus.PENDING) == 1


def test_crash_after_send_before_ack_reclaims_inflight() -> None:
    box = MemoryOutbox(visibility_timeout_ms=1000)
    box.enqueue(_ev(_scope(), "evt-00000001", "k1"))
    claimed = box.claim_batch(10, now_ms=0)
    assert len(claimed) == 1 and box.count(OutboxStatus.INFLIGHT) == 1
    # no ack arrives; before timeout it is NOT re-claimable, after timeout it is
    assert box.claim_batch(10, now_ms=500) == []
    reclaimed = box.claim_batch(10, now_ms=2000)
    assert len(reclaimed) == 1  # at-least-once redelivery after visibility timeout


def test_ordering_key_preserved_on_claim() -> None:
    box = MemoryOutbox()
    s = _scope()
    box.enqueue(_ev(s, "evt-00000003", "k3"))
    box.enqueue(_ev(s, "evt-00000001", "k1"))
    box.enqueue(_ev(s, "evt-00000002", "k2"))
    claimed = box.claim_batch(10, now_ms=0)
    assert [e.ordering_key for e in claimed] == ["k1", "k2", "k3"]


def test_poison_record_goes_to_dead_letter() -> None:
    box = MemoryOutbox(max_retries=2)
    s = _scope()
    box.enqueue(_ev(s, "evt-00000001", "k1"))
    status = None
    for i in range(3):
        box.claim_batch(10, now_ms=i * 100000)
        status = box.nack("evt-00000001", now_ms=i * 100000)
    assert status is OutboxStatus.DEAD_LETTER
    assert len(box.dead_letters()) == 1


def test_ack_advances_convergence_cursor() -> None:
    box = MemoryOutbox()
    s = _scope()
    box.enqueue(_ev(s, "evt-00000001", "k1", cursor=7))
    box.claim_batch(10, now_ms=0)
    assert box.converged_through() == 0
    box.ack("evt-00000001", ack_digest="a" * 64)
    assert box.converged_through() == 7
    assert box.count(OutboxStatus.ACKED) == 1


def test_revoked_scope_fails_closed_on_enqueue_and_never_delivers() -> None:
    s = _scope(tenant="tenant-revoked")
    box = MemoryOutbox(revoked_partitions=frozenset({s.partition_key()}))
    with pytest.raises(ScopeRevoked):
        box.enqueue(_ev(s, "evt-00000001", "k1"))


def test_revoked_after_enqueue_is_not_delivered() -> None:
    s = _scope(tenant="tenant-alpha")
    store: dict = {}
    MemoryOutbox(store).enqueue(_ev(s, "evt-00000001", "k1"))
    # scope later revoked: a new consumer over the same store must not deliver it
    revoked_box = MemoryOutbox(store, revoked_partitions=frozenset({s.partition_key()}))
    assert revoked_box.claim_batch(10, now_ms=0) == []


def test_erasure_tombstone_carries_no_payload_flag() -> None:
    s = _scope()
    ev = new_event(event_id="evt-00000009", scope=s, event_type=OutboxEventType.ERASURE,
                   memory_id="mem-1", ordering_key="z", convergence_cursor=1, tombstone=True)
    assert ev.tombstone is True and ev.event_type is OutboxEventType.ERASURE


def test_encryption_metadata_is_carried() -> None:
    s = _scope()
    ev = _ev(s, "evt-00000010", "k1", encryption_key_ref="kms://key/1", encryption_alg="AES-256-GCM")
    assert ev.encryption_key_ref == "kms://key/1" and ev.encryption_alg == "AES-256-GCM"
