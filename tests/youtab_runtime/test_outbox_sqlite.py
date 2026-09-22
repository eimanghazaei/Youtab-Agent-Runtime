from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from youtab_runtime.memory import (
    DigestMismatch,
    MemoryScope,
    OutboxConflict,
    OutboxEventType,
    OutboxStatus,
    ScopeRevoked,
    SqliteOutbox,
    new_event,
)

WORKER = str(Path(__file__).parent / "_outbox_worker.py")


def _scope(tenant: str = "tenant-alpha") -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant, organization_id="org-acme", workspace_id="ws-sales",
        principal_id="user-alpha", agent_id="agent-01", run_id="run-1", purpose="default",
    )


def _ev(scope, eid, key, cursor=1, **kw):
    return new_event(event_id=eid, scope=scope, event_type=OutboxEventType.STORE_CANDIDATE,
                     memory_id="mem-1", ordering_key=key, convergence_cursor=cursor, **kw)


def _run(*args: str) -> str:
    proc = subprocess.run([sys.executable, WORKER, *args], capture_output=True, text=True,
                          cwd=str(Path(__file__).resolve().parents[2]))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# ---- separate-process durability (the required proof) --------------------

def test_producer_process_writes_then_consumer_process_delivers(tmp_path) -> None:
    db = str(tmp_path / "obx.db")
    _run("enqueue", db, "evt-00000001", "tenant-alpha", "k1", "5")  # producer process exits
    out = _run("claim_ack", db, "consumer-A", "0")                   # later consumer process
    assert out == "1"
    box = SqliteOutbox(db)
    assert box.count(OutboxStatus.ACKED) == 1
    assert box.converged_through() == 5


def test_crash_after_send_before_ack_redelivers_after_timeout(tmp_path) -> None:
    db = str(tmp_path / "obx.db")
    _run("enqueue", db, "evt-00000001", "tenant-alpha", "k1", "1")
    # consumer process claims but exits WITHOUT ack (crash)
    assert _run("claim_only", db, "consumer-A", "0") == "1"
    # the worker claimed with its default 30s visibility timeout at now_ms=0.
    box = SqliteOutbox(db)
    assert box.count(OutboxStatus.INFLIGHT) == 1
    assert box.claim_batch(10, now_ms=500, consumer_id="c2") == []       # before timeout
    assert len(box.claim_batch(10, now_ms=35_000, consumer_id="c2")) == 1  # after timeout


# ---- in-process (file-backed) contract scenarios --------------------------

def test_duplicate_delivery_deduped(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    s = _scope()
    assert box.enqueue(_ev(s, "evt-00000001", "k1")) is True
    assert box.enqueue(_ev(s, "evt-00000001", "k1")) is False


def test_ordering_preserved(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    s = _scope()
    box.enqueue(_ev(s, "evt-00000003", "k3"))
    box.enqueue(_ev(s, "evt-00000001", "k1"))
    box.enqueue(_ev(s, "evt-00000002", "k2"))
    claimed = box.claim_batch(10, now_ms=0, consumer_id="c")
    assert [e.ordering_key for e in claimed] == ["k1", "k2", "k3"]


def test_poison_to_dead_letter(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"), max_retries=2)
    s = _scope()
    box.enqueue(_ev(s, "evt-00000001", "k1"))
    status = None
    for i in range(3):
        box.claim_batch(10, now_ms=i * 100000, consumer_id="c")
        status = box.nack("evt-00000001", now_ms=i * 100000)
    assert status is OutboxStatus.DEAD_LETTER
    assert box.count(OutboxStatus.DEAD_LETTER) == 1


def test_revoked_scope_fails_closed(tmp_path) -> None:
    s = _scope("tenant-revoked")
    box = SqliteOutbox(str(tmp_path / "o.db"), revoked_partitions=frozenset({s.partition_key()}))
    with pytest.raises(ScopeRevoked):
        box.enqueue(_ev(s, "evt-00000001", "k1"))


def test_expired_authority_dead_letters(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    s = _scope()
    box.enqueue(_ev(s, "evt-00000001", "k1"), authority_expires_ms=1000)
    # claim after authority expiry -> event dead-lettered, never delivered
    claimed = box.claim_batch(10, now_ms=5000, consumer_id="c")
    assert claimed == []
    assert box.count(OutboxStatus.DEAD_LETTER) == 1


def test_tombstone_erasure_event(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    s = _scope()
    ev = new_event(event_id="evt-00000009", scope=s, event_type=OutboxEventType.ERASURE,
                   memory_id="mem-1", ordering_key="z", convergence_cursor=1, tombstone=True)
    box.enqueue(ev)
    claimed = box.claim_batch(10, now_ms=0, consumer_id="c")
    assert claimed[0].tombstone is True


def test_key_rotation_updates_metadata(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    s = _scope()
    box.enqueue(_ev(s, "evt-00000001", "k1"))
    n = box.rotate_encryption(["evt-00000001"], key_ref="kms://key/2", alg="AES-256-GCM")
    assert n == 1
    ev = box.claim_batch(10, now_ms=0, consumer_id="c")[0]
    assert ev.encryption_key_ref == "kms://key/2"


def test_offline_restart_survives(tmp_path) -> None:
    db = str(tmp_path / "o.db")
    b1 = SqliteOutbox(db)
    b1.enqueue(_ev(_scope(), "evt-00000001", "k1"))
    b1.close()
    b2 = SqliteOutbox(db)  # "restart"
    assert b2.count(OutboxStatus.PENDING) == 1


def test_convergence_cursor_after_ack(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    box.enqueue(_ev(_scope(), "evt-00000001", "k1", cursor=9))
    box.claim_batch(10, now_ms=0, consumer_id="c")
    assert box.converged_through() == 0
    box.ack("evt-00000001", ack_digest="a" * 64, payload_digest=None)
    assert box.converged_through() == 9


def test_two_concurrent_consumers_one_claim(tmp_path) -> None:
    box_a = SqliteOutbox(str(tmp_path / "o.db"))
    box_b = SqliteOutbox(str(tmp_path / "o.db"))
    box_a.enqueue(_ev(_scope(), "evt-00000001", "k1"))
    a = box_a.claim_batch(10, now_ms=0, consumer_id="A")
    b = box_b.claim_batch(10, now_ms=0, consumer_id="B")
    assert len(a) + len(b) == 1  # exactly one consumer claims it


def test_payload_digest_mismatch_rejected(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    box.enqueue(_ev(_scope(), "evt-00000001", "k1"), payload_digest="d" * 64)
    box.claim_batch(10, now_ms=0, consumer_id="c")
    with pytest.raises(DigestMismatch):
        box.ack("evt-00000001", ack_digest="a" * 64, payload_digest="e" * 64)


# ---- item 4: conflicting re-enqueue must FAIL CLOSED, not be silently ignored

def test_identical_reenqueue_is_idempotent(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    s = _scope()
    assert box.enqueue(_ev(s, "evt-00000001", "k1"), payload_digest="d" * 64) is True
    assert box.enqueue(_ev(s, "evt-00000001", "k1"), payload_digest="d" * 64) is False
    assert box.count(OutboxStatus.PENDING) == 1


def test_conflicting_digest_same_id_fails_closed(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    s = _scope()
    box.enqueue(_ev(s, "evt-00000001", "k1"), payload_digest="d" * 64)
    with pytest.raises(OutboxConflict):
        box.enqueue(_ev(s, "evt-00000001", "k1"), payload_digest="e" * 64)
    assert box.count(OutboxStatus.PENDING) == 1  # original untouched


def test_same_id_different_scope_fails_closed(tmp_path) -> None:
    box = SqliteOutbox(str(tmp_path / "o.db"))
    box.enqueue(_ev(_scope("tenant-alpha"), "evt-00000001", "k1"), payload_digest="d" * 64)
    with pytest.raises(OutboxConflict):
        box.enqueue(_ev(_scope("tenant-beta"), "evt-00000001", "k1"), payload_digest="d" * 64)


def test_conflict_persists_across_process_restart(tmp_path) -> None:
    db = str(tmp_path / "obx.db")
    # producer PROCESS writes the original (worker uses digest 'd'*64) and exits
    _run("enqueue", db, "evt-00000001", "tenant-alpha", "k1", "1")
    # a later process re-enqueues the SAME id with a DIFFERENT payload -> fail closed
    box = SqliteOutbox(db)
    with pytest.raises(OutboxConflict):
        box.enqueue(_ev(_scope("tenant-alpha"), "evt-00000001", "k1"), payload_digest="e" * 64)
