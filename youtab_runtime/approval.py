"""Verification and single-use consumption of signed effect authority.

The Runtime does NOT mint authority (see :mod:`effect_authorization`). This
module verifies a signed :class:`EffectAuthorization` against the trusted
keyring, checks it is bound to the exact effect + principal + workspace, and
consumes its single-use id atomically — so an untrusted issuer, a test key in
production, a bad signature, an expired, digest-mismatched, cross-principal, or
already-consumed authorization fails closed and no effect is claimed.

The consumed-authorization ledger is durable (sqlite, coexists with the
effects/run_journal tables) so single-use survives a process restart — a replayed
authorization after a crash is still rejected.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Optional

from youtab_runtime.effect_authorization import (
    EffectAuthorization,
    resolve_authority_public_key,
)
from youtab_runtime.effect_ledger import _immediate_txn  # single-writer txn
from youtab_runtime.run_journal import Principal, default_db_path

__all__ = [
    "ApprovalError",
    "ApprovalConsumedError",
    "ApprovalDigestMismatchError",
    "ApprovalBindingError",
    "compute_effect_digest",
    "content_digest",
    "reserve_and_consume_authorization",
]


class ApprovalError(Exception):
    """Fail-closed base. Messages never disclose why beyond the coarse class."""


class ApprovalConsumedError(ApprovalError):
    pass


class ApprovalDigestMismatchError(ApprovalError):
    pass


class ApprovalBindingError(ApprovalError):
    pass


def content_digest(data: Optional[bytes]) -> str:
    """Digest the request/content bytes. ``None`` (e.g. a read) → the empty
    digest, a stable constant, so a read authorization is still content-bound."""
    return hashlib.sha256(data if data is not None else b"").hexdigest()


def compute_effect_digest(
    operation: str, safe_path: str, workspace_id: str, content_sha256: str
) -> str:
    """The identity an authorization is bound to: operation + canonical path +
    workspace + normalized content digest. Same path + different content →
    different digest → an authorization for one cannot authorize the other."""
    canonical = "\x1f".join(
        [str(operation), str(safe_path), str(workspace_id), str(content_sha256)]
    )
    return hashlib.sha256(("approval:" + canonical).encode("utf-8")).hexdigest()


def _ensure_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS consumed_authorizations (
             authorization_id TEXT PRIMARY KEY,
             tenant           TEXT NOT NULL,
             user             TEXT NOT NULL,
             workspace_id     TEXT NOT NULL,
             effect_digest    TEXT NOT NULL,
             key_id           TEXT NOT NULL,
             consumed_at      REAL NOT NULL
           )"""
    )


def reserve_and_consume_authorization(
    auth: EffectAuthorization,
    expected_effect_digest: str,
    principal: Principal,
    workspace_id: str,
    *,
    production: bool,
    now: float,
    production_keys: Optional[Mapping[str, str]] = None,
    test_keys: Optional[Mapping[str, str]] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Verify a signed authorization and consume it single-use, or fail closed.

    Order: resolve+trust the key (rejecting a test key in production) → verify
    signature + expiry → check tenant/principal/workspace binding → check the
    effect digest → atomically consume the single-use authorization id.
    """
    if not isinstance(principal, Principal):
        raise ApprovalError("principal must be a Principal instance")

    # 1) trust + signature (raises UntrustedIssuer / TestAuthorityInProduction /
    #    InvalidAuthorizationSignature — all EffectAuthorizationError, fail closed)
    public_key = resolve_authority_public_key(
        auth.key_id, production=production,
        production_keys=production_keys, test_keys=test_keys,
    )
    auth.verify(public_key, now=datetime.fromtimestamp(now, UTC))

    # 2) binding
    if (
        auth.tenant_id != principal.tenant
        or auth.user_id != principal.user
        or auth.workspace_id != workspace_id
    ):
        raise ApprovalBindingError("authorization binding mismatch")
    if auth.effect_digest != expected_effect_digest:
        raise ApprovalDigestMismatchError("authorization does not authorize this effect")

    # 3) atomic single-use consume
    path = db_path or default_db_path()
    with _immediate_txn(path) as conn:
        _ensure_table(conn)
        existing = conn.execute(
            "SELECT 1 FROM consumed_authorizations WHERE authorization_id=?",
            (auth.authorization_id,),
        ).fetchone()
        if existing is not None:
            raise ApprovalConsumedError("authorization already consumed")
        conn.execute(
            "INSERT INTO consumed_authorizations "
            "(authorization_id, tenant, user, workspace_id, effect_digest, key_id, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (auth.authorization_id, principal.tenant, principal.user, workspace_id,
             expected_effect_digest, auth.key_id, float(now)),
        )
