"""Worker lease, workspace binding, and evidence-bound reconciliation for
grant-bound effects.

Lease: when a worker claims an effect it takes a time-bounded lease (owner token
+ expiry) recorded in the effect's ledger detail. Only the lease holder may
settle. Lease acquisition/renewal/reacquire are atomic compare-and-set on the
effect row inside the ledger's single-writer transaction. A lease that expires
is either swept to a known state (reconciliation / dead-letter) or reacquired by
a new worker with a real attempt increment up to a ceiling, then dead-lettered.

Workspace: receipt/settle/reconcile bind the effect's canonical workspace, not
only ``Principal(tenant, user)`` — same tenant + user in workspace B cannot touch
a workspace-A effect.

Reconciliation: resolving an ambiguous effect to a terminal state requires
verifiable evidence from the connector/provider/reference boundary; an arbitrary
caller assertion is refused and the effect stays non-terminal.

Time-based complement to :func:`effect_ledger.recover_interrupted`. Pure-stdlib.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.run_journal import Principal, default_db_path
from youtab_runtime.run_states import EffectState

__all__ = [
    "LeaseError",
    "WorkspaceMismatchError",
    "ReconciliationError",
    "EffectEvidence",
    "lease_detail",
    "assert_lease_holder",
    "assert_workspace",
    "renew_lease",
    "reacquire_expired_lease",
    "sweep_expired_leases",
    "reconcile_to_terminal",
]


class LeaseError(Exception):
    """Fail-closed: a non-holder attempted to settle, or the lease is absent."""


class WorkspaceMismatchError(Exception):
    """Fail-closed: the effect belongs to a different canonical workspace."""


class ReconciliationError(Exception):
    """Fail-closed: reconciliation evidence is missing, mismatched, or stale."""


def lease_detail(owner_token: str, lease_expires_at: float) -> dict:
    """Detail payload to pass to ``try_claim(detail=...)`` when claiming."""
    return {"lease": {"owner": str(owner_token), "expires_at": float(lease_expires_at),
                      "attempt": 1}}


def _lease_of(record) -> dict:
    return (record.detail or {}).get("lease") or {}


def assert_lease_holder(
    effect_id: str, principal: Principal, owner_token: str,
    *, db_path: Optional[Path] = None,
) -> None:
    """Raise unless ``owner_token`` matches the effect's recorded lease holder.

    Uses the per-principal ``get_effect`` so a foreign principal cannot even
    address the effect (returns None -> LeaseError).
    """
    rec = _ledger.get_effect(effect_id, principal, db_path=db_path)
    if rec is None:
        raise LeaseError("effect not found or not owned")
    holder = _lease_of(rec).get("owner")
    if holder is None or holder != str(owner_token):
        raise LeaseError("caller does not hold the effect lease")


def assert_workspace(
    effect_id: str, principal: Principal, workspace_id: str,
    *, db_path: Optional[Path] = None,
) -> None:
    """Raise unless the effect is bound to ``workspace_id``. Same tenant+user in a
    different workspace is refused (fail closed)."""
    rec = _ledger.get_effect(effect_id, principal, db_path=db_path)
    if rec is None:
        raise LeaseError("effect not found or not owned")
    bound = (rec.detail or {}).get("workspace")
    if bound is None or bound != workspace_id:
        raise WorkspaceMismatchError("effect belongs to a different workspace")


# --------------------------------------------------------------------------- #
# Atomic lease mutation (compare-and-set on the effect row's detail)           #
# --------------------------------------------------------------------------- #
def _update_lease_cas(
    effect_id: str, principal: Principal, *, expect_owner: Optional[str],
    mutate, db_path: Optional[Path],
):
    """Atomically read the effect row, verify ownership + state, apply ``mutate``
    to the lease dict, and write it back — all inside the ledger's single-writer
    ``BEGIN IMMEDIATE`` transaction. ``mutate(state, lease)`` returns the new
    lease dict or raises. Cross-principal rows are invisible (fail closed)."""
    path = db_path or default_db_path()
    with _ledger._immediate_txn(path) as conn:
        _ledger._ensure_effects_table(conn)
        row = conn.execute(
            "SELECT * FROM effects WHERE effect_id=? AND tenant=? AND user=?",
            (effect_id, principal.tenant, principal.user),
        ).fetchone()
        if row is None:
            raise LeaseError("effect not found or not owned")
        detail = json.loads(row["detail_json"])
        lease = dict(detail.get("lease") or {})
        if expect_owner is not None and lease.get("owner") != expect_owner:
            raise LeaseError("caller does not hold the effect lease")
        new_lease = mutate(EffectState(row["state"]), lease)
        detail["lease"] = new_lease
        conn.execute(
            "UPDATE effects SET detail_json=? WHERE effect_id=? AND tenant=? AND user=?",
            (json.dumps(detail, ensure_ascii=False, sort_keys=True),
             effect_id, principal.tenant, principal.user),
        )
        return new_lease


def renew_lease(
    effect_id: str, principal: Principal, current_token: str, new_token: str,
    *, new_expires_at: float, db_path: Optional[Path] = None,
) -> None:
    """Renew the lease: the current holder rotates to ``new_token`` + a new
    expiry. Atomically invalidates ``current_token`` — a stale holder can no
    longer settle. Only the current holder may renew (else LeaseError)."""
    def mutate(state, lease):
        if state != EffectState.IN_PROGRESS:
            raise LeaseError("lease can only be renewed while in progress")
        return {"owner": str(new_token), "expires_at": float(new_expires_at),
                "attempt": int(lease.get("attempt", 1))}
    _update_lease_cas(effect_id, principal, expect_owner=str(current_token),
                      mutate=mutate, db_path=db_path)


def reacquire_expired_lease(
    effect_id: str, principal: Principal, new_token: str,
    *, now: float, new_expires_at: float, max_attempts: int = 3,
    db_path: Optional[Path] = None,
) -> dict:
    """A new worker reacquires an in_progress effect whose lease has EXPIRED,
    incrementing the real attempt counter. At/above ``max_attempts`` the effect
    is dead-lettered (``failed``) instead. Returns
    ``{"reacquired": bool, "attempt": int, "dead_lettered": bool}``. A lease that
    is still valid cannot be reacquired (fail closed)."""
    outcome = {"reacquired": False, "attempt": 0, "dead_lettered": False}

    def mutate(state, lease):
        if state != EffectState.IN_PROGRESS:
            raise LeaseError("only an in-progress effect can be reacquired")
        exp = lease.get("expires_at")
        if exp is None or now < float(exp):
            raise LeaseError("lease is still valid; cannot reacquire")
        attempt = int(lease.get("attempt", 1))
        if attempt >= int(max_attempts):
            outcome["dead_lettered"] = True
            outcome["attempt"] = attempt
            raise _DeadLetter()  # handled below, after the txn closes cleanly
        new_attempt = attempt + 1
        outcome["reacquired"] = True
        outcome["attempt"] = new_attempt
        return {"owner": str(new_token), "expires_at": float(new_expires_at),
                "attempt": new_attempt}

    try:
        _update_lease_cas(effect_id, principal, expect_owner=None,
                          mutate=mutate, db_path=db_path)
    except _DeadLetter:
        _ledger.mark_failed(
            effect_id, principal,
            detail={"dead_letter": True, "reason": "lease_reacquire_max_attempts"},
            db_path=db_path,
        )
    return outcome


class _DeadLetter(Exception):
    pass


def sweep_expired_leases(
    run_id: str, principal: Principal, *, now: float, max_attempts: int = 3,
    db_path: Optional[Path] = None,
) -> dict:
    """Sweep this run's ``in_progress`` effects whose lease has expired without a
    reacquire. Returns ``{"reconciled": n, "dead_lettered": m}``. A still-valid
    lease is untouched (no stealing). At/above the attempt ceiling the effect is
    dead-lettered; otherwise it moves to ``reconciliation_required``."""
    reconciled = dead = 0
    for rec in _ledger.list_effects(
        run_id, principal, state=EffectState.IN_PROGRESS, db_path=db_path
    ):
        lease = _lease_of(rec)
        exp = lease.get("expires_at")
        if exp is None or now < float(exp):
            continue
        if int(lease.get("attempt", 1)) >= int(max_attempts):
            _ledger.mark_failed(
                rec.effect_id, principal,
                detail={"dead_letter": True, "reason": "lease_expired_max_attempts"},
                db_path=db_path,
            )
            dead += 1
        else:
            _ledger.mark_reconciliation_required(
                rec.effect_id, principal,
                detail={"reason": "lease_expired"}, db_path=db_path,
            )
            reconciled += 1
    return {"reconciled": reconciled, "dead_lettered": dead}


# --------------------------------------------------------------------------- #
# Evidence-bound reconciliation                                               #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EffectEvidence:
    """Reconciliation evidence from the connector/provider/reference boundary."""

    effect_id: str            # provider/effect idempotency identity
    operation_digest: str     # must equal the effect's stored target scope digest
    workspace_id: str
    outcome: str              # "succeeded" | "failed"
    result_digest: str        # digest of the provider/reference result
    provenance: str           # who produced this evidence (connector/reference id)
    verified_at: float        # epoch seconds the outcome was verified


def reconcile_to_terminal(
    effect_id: str, principal: Principal, evidence: EffectEvidence,
    *, workspace_id: str, now: float, max_age_seconds: float = 3600.0,
    db_path: Optional[Path] = None,
) -> str:
    """Resolve an ambiguous effect (``unknown`` / ``reconciliation_required``) to a
    terminal state using verifiable ``evidence``. Refuses (leaving the effect
    non-terminal) if the evidence is forged (wrong effect id/workspace), mismatched
    (wrong operation digest), or stale (verified too long ago / in the future).
    ``succeeded`` -> committed; ``failed`` -> failed."""
    assert_workspace(effect_id, principal, workspace_id, db_path=db_path)
    rec = _ledger.get_effect(effect_id, principal, db_path=db_path)
    if rec is None:
        raise LeaseError("effect not found or not owned")
    if rec.state not in (EffectState.UNKNOWN, EffectState.RECONCILIATION_REQUIRED):
        raise ReconciliationError("effect is not in an ambiguous state")
    if evidence.effect_id != effect_id or evidence.workspace_id != workspace_id:
        raise ReconciliationError("evidence identity does not match the effect")
    if evidence.operation_digest != rec.target_scope_digest:
        raise ReconciliationError("evidence operation digest does not match the effect")
    if evidence.verified_at > now or (now - evidence.verified_at) > float(max_age_seconds):
        raise ReconciliationError("evidence is stale or from the future")
    if evidence.outcome not in ("succeeded", "failed"):
        raise ReconciliationError("evidence outcome is not conclusive")
    ev = {"reconciled": True, "result_digest": evidence.result_digest,
          "provenance": evidence.provenance}
    if evidence.outcome == "succeeded":
        return _ledger.mark_committed(effect_id, principal, detail=ev, db_path=db_path).state.value
    return _ledger.mark_failed(effect_id, principal, detail=ev, db_path=db_path).state.value
