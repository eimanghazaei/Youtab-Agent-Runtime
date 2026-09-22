"""File-backed SQLite outbox — local durability with transactional fencing.

Implements the same event contract as :mod:`youtab_runtime.memory.outbox` but on a
WAL SQLite file, so durability survives a process exit (not just object reuse).
Claiming uses a transactional, fenced UPDATE so two concurrent consumers never
deliver the same event twice. This proves LOCAL durability only — NOT distributed
or multi-host delivery.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .outbox import OutboxEvent, OutboxStatus, ScopeRevoked

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    partition_key TEXT NOT NULL,
    event_json TEXT NOT NULL,
    ordering_key TEXT NOT NULL,
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL,
    backoff_until_ms INTEGER NOT NULL,
    authority_expires_ms INTEGER,
    convergence_cursor INTEGER NOT NULL,
    payload_digest TEXT,
    claimed_by TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON outbox_events(status, ordering_key);
"""


class DigestMismatch(RuntimeError):
    """Raised when an ack's payload digest does not match the stored one."""


class SqliteOutbox:
    def __init__(
        self,
        db_path: str,
        *,
        max_retries: int = 5,
        visibility_timeout_ms: int = 30_000,
        revoked_partitions: frozenset[str] = frozenset(),
    ) -> None:
        self._path = db_path
        self._max_retries = max_retries
        self._vt = visibility_timeout_ms
        self._revoked = revoked_partitions
        self._conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- producer ----------------------------------------------------------

    def enqueue(
        self,
        event: OutboxEvent,
        *,
        payload_digest: str | None = None,
        authority_expires_ms: int | None = None,
    ) -> bool:
        pk = event.scope.partition_key()
        if pk in self._revoked:
            raise ScopeRevoked("cannot enqueue for a revoked scope")
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO outbox_events
               (event_id, partition_key, event_json, ordering_key, status, retry_count,
                backoff_until_ms, authority_expires_ms, convergence_cursor, payload_digest,
                claimed_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id, pk, event.model_dump_json(), event.ordering_key,
                OutboxStatus.PENDING.value, 0, 0, authority_expires_ms,
                event.convergence_cursor, payload_digest, None,
                event.created_at.isoformat(),
            ),
        )
        return cur.rowcount == 1

    # ---- consumer ----------------------------------------------------------

    def claim_batch(self, limit: int, *, now_ms: int, consumer_id: str) -> list[OutboxEvent]:
        claimed: list[OutboxEvent] = []
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                """SELECT event_id, event_json, partition_key, authority_expires_ms
                   FROM outbox_events
                   WHERE (status=? AND backoff_until_ms<=?)
                      OR (status=? AND backoff_until_ms<=?)
                   ORDER BY ordering_key, created_at""",
                (OutboxStatus.PENDING.value, now_ms, OutboxStatus.INFLIGHT.value, now_ms),
            ).fetchall()
            for event_id, event_json, pk, auth_exp in rows:
                if len(claimed) >= limit:
                    break
                if pk in self._revoked:
                    continue
                if auth_exp is not None and now_ms >= auth_exp:
                    # expired authority: dead-letter, never deliver
                    self._conn.execute(
                        "UPDATE outbox_events SET status=? WHERE event_id=?",
                        (OutboxStatus.DEAD_LETTER.value, event_id),
                    )
                    continue
                # fenced claim: only succeeds if still deliverable
                cur = self._conn.execute(
                    """UPDATE outbox_events SET status=?, backoff_until_ms=?, claimed_by=?
                       WHERE event_id=? AND status IN (?,?) AND backoff_until_ms<=?""",
                    (OutboxStatus.INFLIGHT.value, now_ms + self._vt, consumer_id, event_id,
                     OutboxStatus.PENDING.value, OutboxStatus.INFLIGHT.value, now_ms),
                )
                if cur.rowcount == 1:
                    claimed.append(OutboxEvent.model_validate_json(event_json))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return claimed

    def ack(self, event_id: str, *, ack_digest: str, payload_digest: str | None = None) -> None:
        row = self._conn.execute(
            "SELECT payload_digest, convergence_cursor FROM outbox_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        stored_digest, _cursor = row
        if stored_digest is not None and payload_digest is not None and stored_digest != payload_digest:
            raise DigestMismatch("payload digest mismatch on ack")
        self._conn.execute(
            "UPDATE outbox_events SET status=?, payload_digest=COALESCE(payload_digest, ?) WHERE event_id=?",
            (OutboxStatus.ACKED.value, ack_digest, event_id),
        )

    def nack(self, event_id: str, *, now_ms: int, backoff_ms: int = 1_000) -> OutboxStatus:
        row = self._conn.execute(
            "SELECT retry_count FROM outbox_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        new_retry = int(row[0]) + 1
        if new_retry > self._max_retries:
            self._conn.execute(
                "UPDATE outbox_events SET status=?, retry_count=? WHERE event_id=?",
                (OutboxStatus.DEAD_LETTER.value, new_retry, event_id),
            )
            return OutboxStatus.DEAD_LETTER
        self._conn.execute(
            "UPDATE outbox_events SET status=?, retry_count=?, backoff_until_ms=? WHERE event_id=?",
            (OutboxStatus.PENDING.value, new_retry, now_ms + backoff_ms * new_retry, event_id),
        )
        return OutboxStatus.PENDING

    # ---- observability -----------------------------------------------------

    def converged_through(self) -> int:
        row = self._conn.execute(
            "SELECT MAX(convergence_cursor) FROM outbox_events WHERE status=?",
            (OutboxStatus.ACKED.value,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def count(self, status: OutboxStatus) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE status=?", (status.value,)
        ).fetchone()
        return int(row[0])

    def rotate_encryption(self, event_ids: Iterable[str], *, key_ref: str, alg: str) -> int:
        """Rotate carried encryption metadata for pending events (re-serialize)."""

        n = 0
        for eid in event_ids:
            row = self._conn.execute(
                "SELECT event_json FROM outbox_events WHERE event_id=?", (eid,)
            ).fetchone()
            if row is None:
                continue
            ev = OutboxEvent.model_validate_json(row[0])
            ev2 = ev.model_copy(update={"encryption_key_ref": key_ref, "encryption_alg": alg})
            self._conn.execute(
                "UPDATE outbox_events SET event_json=? WHERE event_id=?",
                (ev2.model_dump_json(), eid),
            )
            n += 1
        return n
