"""Lane-2 recovery ledger for consumed-authorization / effect-not-created.

A signed authorization is consumed single-use in Lane-1's durable ledger, then
the connector creates the effect. Consumption and effect creation are separate
transactions (Lane-1 owns the approval table; the effect ledger opens its own
``BEGIN IMMEDIATE``), so they are not jointly atomic. If effect creation or
reservation fails AFTER a successful consume, the authorization must never be
silently lost: the connector records a deterministic
``authorization_consumed_effect_not_created`` row here, keyed by the single-use
authorization id and bound to the effect id + effect digest, so the situation is
recoverable/terminal and reconcilable rather than an ambiguous leak.

This is a Lane-2-owned table living in the same sqlite file as the run journal
(it coexists via ``CREATE TABLE IF NOT EXISTS``); it does not modify any
Lane-1-owned module.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from youtab_runtime.run_journal import default_db_path

__all__ = [
    "ORPHAN_REASON",
    "record_orphaned_authorization",
    "get_orphaned_authorization",
]

ORPHAN_REASON = "authorization_consumed_effect_not_created"


def _connect(db_path: Optional[Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or default_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS enterprise_orphaned_authorizations (
             authorization_id TEXT PRIMARY KEY,
             effect_id        TEXT NOT NULL,
             effect_digest    TEXT NOT NULL,
             reason           TEXT NOT NULL,
             recorded_at      REAL NOT NULL
           )"""
    )


def record_orphaned_authorization(
    authorization_id: str,
    effect_id: str,
    effect_digest: str,
    *,
    reason: str = ORPHAN_REASON,
    now: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Durably record that ``authorization_id`` was consumed but its effect was
    not created. Idempotent (INSERT OR IGNORE), so a retry re-recording the same
    id is a no-op — deterministic recovery, never a duplicate."""
    ts = time.time() if now is None else float(now)
    with _connect(db_path) as conn:
        _ensure(conn)
        conn.execute(
            "INSERT OR IGNORE INTO enterprise_orphaned_authorizations "
            "(authorization_id, effect_id, effect_digest, reason, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (authorization_id, effect_id, effect_digest, reason, ts),
        )
        conn.commit()


def get_orphaned_authorization(
    authorization_id: str, *, db_path: Optional[Path] = None
) -> Optional[dict]:
    """Return the recovery record for a consumed-but-not-created authorization."""
    with _connect(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT * FROM enterprise_orphaned_authorizations WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
    return dict(row) if row is not None else None
