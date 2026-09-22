"""Approval binding for grant-bound effects.

An approval authorizes exactly one effect: it is bound to that effect's
*digest* (operation type + canonical path + workspace + normalized content
digest), to the principal and workspace, has an expiry, and is single-use. The
operation boundary consumes it atomically immediately before claiming the effect,
so an expired, already-consumed, forged, or digest-mismatched approval fails
closed and no side effect is claimed.

Durable (sqlite, coexists with the effects/run_journal tables) so single-use
survives a process restart — a replayed approval after a crash is still rejected.
Pure-stdlib.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from youtab_runtime.effect_ledger import _immediate_txn  # single-writer txn
from youtab_runtime.run_journal import Principal, default_db_path

__all__ = [
    "ApprovalError",
    "ApprovalNotFoundError",
    "ApprovalExpiredError",
    "ApprovalConsumedError",
    "ApprovalDigestMismatchError",
    "ApprovalBindingError",
    "compute_effect_digest",
    "content_digest",
    "issue_approval",
    "consume_approval",
]


class ApprovalError(Exception):
    """Fail-closed base. Messages never disclose why beyond the coarse class."""


class ApprovalNotFoundError(ApprovalError):
    pass


class ApprovalExpiredError(ApprovalError):
    pass


class ApprovalConsumedError(ApprovalError):
    pass


class ApprovalDigestMismatchError(ApprovalError):
    pass


class ApprovalBindingError(ApprovalError):
    pass


def content_digest(data: Optional[bytes]) -> str:
    """Digest the request/content bytes. ``None`` (e.g. a read) → the empty
    digest, a stable constant, so a read approval is still content-bound."""
    return hashlib.sha256(data if data is not None else b"").hexdigest()


def compute_effect_digest(
    operation: str, safe_path: str, workspace_id: str, content_sha256: str
) -> str:
    """The identity an approval is bound to: operation + canonical path +
    workspace + normalized content digest. Same path + different content →
    different digest → an approval for one cannot authorize the other."""
    canonical = "\x1f".join(
        [str(operation), str(safe_path), str(workspace_id), str(content_sha256)]
    )
    return hashlib.sha256(("approval:" + canonical).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    effect_digest: str
    tenant: str
    user: str
    workspace_id: str
    expires_at: float
    consumed: bool


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS effect_approvals (
             approval_id   TEXT PRIMARY KEY,
             effect_digest TEXT NOT NULL,
             tenant        TEXT NOT NULL,
             user          TEXT NOT NULL,
             workspace_id  TEXT NOT NULL,
             expires_at    REAL NOT NULL,
             consumed      INTEGER NOT NULL DEFAULT 0
           )"""
    )


def issue_approval(
    approval_id: str,
    effect_digest: str,
    principal: Principal,
    workspace_id: str,
    *,
    expires_at: float,
    db_path: Optional[Path] = None,
) -> ApprovalRecord:
    """Record a single-use approval bound to ``effect_digest`` + principal +
    workspace, expiring at ``expires_at``. Re-issuing the same id is rejected."""
    if not isinstance(principal, Principal):
        raise ApprovalError("principal must be a Principal instance")
    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_table(conn)
        exists = conn.execute(
            "SELECT 1 FROM effect_approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if exists is not None:
            raise ApprovalError("approval id already issued")
        conn.execute(
            "INSERT INTO effect_approvals "
            "(approval_id, effect_digest, tenant, user, workspace_id, expires_at, consumed) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (approval_id, effect_digest, principal.tenant, principal.user,
             workspace_id, float(expires_at)),
        )
    return ApprovalRecord(
        approval_id, effect_digest, principal.tenant, principal.user,
        workspace_id, float(expires_at), False,
    )


def consume_approval(
    approval_id: str,
    expected_effect_digest: str,
    principal: Principal,
    workspace_id: str,
    *,
    now: float,
    db_path: Optional[Path] = None,
) -> ApprovalRecord:
    """Atomically validate and consume an approval (single-use).

    Fail-closed on: unknown id, principal/workspace mismatch, digest mismatch,
    expiry, or already-consumed. On success flips ``consumed=1`` inside the
    single-writer transaction so a concurrent or replayed consume loses.
    """
    if not isinstance(principal, Principal):
        raise ApprovalError("principal must be a Principal instance")
    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT * FROM effect_approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ApprovalNotFoundError("approval not found")
        # Binding first (leak-safe: same coarse failure regardless of which).
        if row["tenant"] != principal.tenant or row["user"] != principal.user \
                or row["workspace_id"] != workspace_id:
            raise ApprovalBindingError("approval binding mismatch")
        if int(row["consumed"]) != 0:
            raise ApprovalConsumedError("approval already consumed")
        if now >= float(row["expires_at"]):
            raise ApprovalExpiredError("approval expired")
        if row["effect_digest"] != expected_effect_digest:
            raise ApprovalDigestMismatchError("approval does not authorize this effect")
        conn.execute(
            "UPDATE effect_approvals SET consumed=1 WHERE approval_id=? AND consumed=0",
            (approval_id,),
        )
    return ApprovalRecord(
        approval_id, row["effect_digest"], row["tenant"], row["user"],
        row["workspace_id"], float(row["expires_at"]), True,
    )
