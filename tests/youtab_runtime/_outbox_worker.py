"""Separate-process worker for SqliteOutbox durability tests (not a test module).

Usage:
  python _outbox_worker.py enqueue   <db> <event_id> <tenant> <ordering_key> <cursor>
  python _outbox_worker.py claim_only <db> <consumer_id> <now_ms>   # claim, exit WITHOUT ack
  python _outbox_worker.py claim_ack  <db> <consumer_id> <now_ms>   # claim + ack
Prints a single integer result line for claim_* commands.
"""
from __future__ import annotations

import sys

from youtab_runtime.memory import (
    MemoryScope,
    OutboxEventType,
    SqliteOutbox,
    new_event,
)


def _scope(tenant: str) -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant, organization_id="org-acme", workspace_id="ws-sales",
        principal_id="user-alpha", agent_id="agent-01", run_id="run-1", purpose="default",
    )


def main(argv: list[str]) -> int:
    cmd = argv[1]
    db = argv[2]
    box = SqliteOutbox(db)
    try:
        if cmd == "enqueue":
            event_id, tenant, ordering_key, cursor = argv[3], argv[4], argv[5], int(argv[6])
            ev = new_event(event_id=event_id, scope=_scope(tenant),
                           event_type=OutboxEventType.STORE_CANDIDATE, memory_id="mem-1",
                           ordering_key=ordering_key, convergence_cursor=cursor)
            box.enqueue(ev, payload_digest="d" * 64)
            return 0
        if cmd in ("claim_only", "claim_ack"):
            consumer_id, now_ms = argv[3], int(argv[4])
            claimed = box.claim_batch(10, now_ms=now_ms, consumer_id=consumer_id)
            if cmd == "claim_ack":
                for ev in claimed:
                    box.ack(ev.event_id, ack_digest="a" * 64, payload_digest="d" * 64)
            print(len(claimed))
            return 0
        raise SystemExit(f"unknown command {cmd}")
    finally:
        box.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
