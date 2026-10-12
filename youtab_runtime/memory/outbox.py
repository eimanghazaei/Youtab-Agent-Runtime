"""Memory-owned transactional outbox contract (Simorgh NOT activated).

The outbox is how the Runtime durably queues memory transport events (promotion
candidates, feedback, supersession, erasure/tombstone) for later idempotent
delivery to Simorgh — without blocking a task and without writing sovereignly.

This module is the CONTRACT + an in-process reference over an injectable store
(a plain dict), so crash/restart/duplicate/reorder/poison/revocation scenarios
are testable deterministically. It does not open a network connection and does
not activate any Simorgh client. Encryption metadata fields are carried by the
contract; actual at-rest crypto is a later slice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .scope import MemoryScope


class OutboxEventType(StrEnum):
    STORE_CANDIDATE = "store_candidate"
    FEEDBACK = "feedback"
    SUPERSEDE = "supersede"
    ERASURE = "erasure"  # tombstone / right-to-erasure


class OutboxStatus(StrEnum):
    PENDING = "pending"
    INFLIGHT = "inflight"
    ACKED = "acked"
    DEAD_LETTER = "dead_letter"


class OutboxEvent(BaseModel):
    """One durable transport event with delivery + encryption + scope metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=8, max_length=128)  # stable, dedupe key
    scope: MemoryScope
    event_type: OutboxEventType
    memory_id: str = Field(min_length=1, max_length=128)
    ordering_key: str = Field(min_length=1, max_length=256)
    status: OutboxStatus = OutboxStatus.PENDING
    retry_count: int = Field(default=0, ge=0)
    backoff_until_ms: int = Field(default=0, ge=0)
    supersedes_id: str | None = Field(default=None, max_length=128)
    tombstone: bool = False  # true for ERASURE; carries no payload
    encryption_key_ref: str | None = Field(default=None, max_length=256)
    encryption_alg: str | None = Field(default=None, max_length=64)
    convergence_cursor: int = Field(default=0, ge=0)
    ack_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class ScopeRevoked(RuntimeError):
    """Raised when an event targets a revoked scope (fail closed)."""


class MemoryOutbox:
    """Producer/consumer over an injectable store; survives restart via that store.

    ``store`` maps event_id -> serialized event dict. Passing the same store to a
    new MemoryOutbox simulates a process restart with no data loss.
    """

    def __init__(
        self,
        store: dict[str, dict] | None = None,
        *,
        max_retries: int = 5,
        visibility_timeout_ms: int = 30_000,
        revoked_partitions: frozenset[str] = frozenset(),
    ) -> None:
        self._store: dict[str, dict] = store if store is not None else {}
        self._max_retries = max_retries
        self._visibility_timeout_ms = visibility_timeout_ms
        self._revoked = revoked_partitions
        self._cursor = self._compute_cursor()

    # ---- producer ----------------------------------------------------------

    def enqueue(self, event: OutboxEvent) -> bool:
        """Idempotent enqueue. Returns False if the event_id already exists."""

        if event.scope.partition_key() in self._revoked:
            raise ScopeRevoked("cannot enqueue for a revoked scope")
        if event.event_id in self._store:
            return False  # duplicate delivery deduped by stable event_id
        self._store[event.event_id] = event.model_dump(mode="json")
        return True

    # ---- consumer ----------------------------------------------------------

    def claim_batch(self, limit: int, *, now_ms: int) -> list[OutboxEvent]:
        """Claim deliverable PENDING/expired-INFLIGHT events in ordering-key order."""

        claimable: list[OutboxEvent] = []
        for raw in self._store.values():
            ev = OutboxEvent.model_validate(raw)
            if ev.scope.partition_key() in self._revoked:
                continue  # revoked scope never delivers
            if ev.status is OutboxStatus.PENDING and ev.backoff_until_ms <= now_ms:
                claimable.append(ev)
            elif ev.status is OutboxStatus.INFLIGHT and self._inflight_expired(ev, now_ms):
                claimable.append(ev)
        claimable.sort(key=lambda e: (e.ordering_key, e.created_at))
        chosen = claimable[:limit]
        for ev in chosen:
            self._store[ev.event_id] = ev.model_copy(
                update={"status": OutboxStatus.INFLIGHT, "backoff_until_ms": now_ms + self._visibility_timeout_ms}
            ).model_dump(mode="json")
        return chosen

    def ack(self, event_id: str, ack_digest: str) -> None:
        ev = self._require(event_id)
        self._store[event_id] = ev.model_copy(
            update={"status": OutboxStatus.ACKED, "ack_digest": ack_digest}
        ).model_dump(mode="json")
        self._cursor = self._compute_cursor()

    def nack(self, event_id: str, *, now_ms: int, backoff_ms: int = 1_000) -> OutboxStatus:
        ev = self._require(event_id)
        new_retry = ev.retry_count + 1
        if new_retry > self._max_retries:
            updated = ev.model_copy(update={"status": OutboxStatus.DEAD_LETTER, "retry_count": new_retry})
        else:
            updated = ev.model_copy(update={
                "status": OutboxStatus.PENDING,
                "retry_count": new_retry,
                "backoff_until_ms": now_ms + backoff_ms * new_retry,
            })
        self._store[event_id] = updated.model_dump(mode="json")
        return updated.status

    # ---- observability -----------------------------------------------------

    def converged_through(self) -> int:
        return self._cursor

    def count(self, status: OutboxStatus) -> int:
        return sum(1 for r in self._store.values() if r.get("status") == status.value)

    def dead_letters(self) -> list[OutboxEvent]:
        return [
            OutboxEvent.model_validate(r)
            for r in self._store.values()
            if r.get("status") == OutboxStatus.DEAD_LETTER.value
        ]

    # ---- internals ---------------------------------------------------------

    def _require(self, event_id: str) -> OutboxEvent:
        raw = self._store.get(event_id)
        if raw is None:
            raise KeyError(f"unknown outbox event {event_id}")
        return OutboxEvent.model_validate(raw)

    def _inflight_expired(self, ev: OutboxEvent, now_ms: int) -> bool:
        return ev.backoff_until_ms <= now_ms

    def _compute_cursor(self) -> int:
        acked = [
            OutboxEvent.model_validate(r).convergence_cursor
            for r in self._store.values()
            if r.get("status") == OutboxStatus.ACKED.value
        ]
        return max(acked, default=0)


def new_event(
    *,
    event_id: str,
    scope: MemoryScope,
    event_type: OutboxEventType,
    memory_id: str,
    ordering_key: str,
    convergence_cursor: int,
    tombstone: bool = False,
    supersedes_id: str | None = None,
    encryption_key_ref: str | None = None,
    encryption_alg: str | None = None,
    now: datetime | None = None,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        scope=scope,
        event_type=event_type,
        memory_id=memory_id,
        ordering_key=ordering_key,
        convergence_cursor=convergence_cursor,
        tombstone=tombstone,
        supersedes_id=supersedes_id,
        encryption_key_ref=encryption_key_ref,
        encryption_alg=encryption_alg,
        created_at=(now or datetime.now(UTC)).astimezone(UTC),
    )
