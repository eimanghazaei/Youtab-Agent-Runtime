"""Worker lease + dead-letter for grant-bound effects.

When a worker claims an effect it takes a time-bounded *lease* (owner token +
expiry) recorded in the effect's ledger detail. Only the lease holder may settle
the effect. A lease that expires without settlement is swept:

  * below the attempt ceiling -> ``reconciliation_required`` (reclaimable only
    through an explicit, audited reconcile — never a silent auto-retry);
  * at/above the ceiling -> ``failed`` with a ``dead_letter`` marker (terminal,
    a known state).

This is the time-based complement to :func:`effect_ledger.recover_interrupted`
(which is process-liveness based). Both converge a stranded effect to a known,
non-retried state. Pure-stdlib on top of the ledger.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.run_journal import Principal
from youtab_runtime.run_states import EffectState

__all__ = [
    "LeaseError",
    "lease_detail",
    "assert_lease_holder",
    "sweep_expired_leases",
    "reconcile_to_terminal",
]


class LeaseError(Exception):
    """Fail-closed: a non-holder attempted to settle, or the lease is absent."""


def lease_detail(owner_token: str, lease_expires_at: float) -> dict:
    """Detail payload to pass to ``try_claim(detail=...)`` when claiming."""
    return {"lease": {"owner": str(owner_token), "expires_at": float(lease_expires_at)}}


def _lease_of(record) -> dict:
    return (record.detail or {}).get("lease") or {}


def assert_lease_holder(
    effect_id: str,
    principal: Principal,
    owner_token: str,
    *,
    db_path: Optional[Path] = None,
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


def sweep_expired_leases(
    run_id: str,
    principal: Principal,
    *,
    now: float,
    max_attempts: int = 3,
    db_path: Optional[Path] = None,
) -> dict:
    """Sweep this run's ``in_progress`` effects whose lease has expired.

    Returns ``{"reconciled": n, "dead_lettered": m}``. Effects whose lease is
    still valid are untouched (no lease stealing). An effect at/above
    ``max_attempts`` is dead-lettered (``failed``); otherwise it is moved to
    ``reconciliation_required`` for controlled recovery.
    """
    reconciled = dead = 0
    for rec in _ledger.list_effects(
        run_id, principal, state=EffectState.IN_PROGRESS, db_path=db_path
    ):
        lease = _lease_of(rec)
        exp = lease.get("expires_at")
        if exp is None or now < float(exp):
            continue  # no lease or still valid -> do not steal
        if int(rec.attempts) >= int(max_attempts):
            _ledger.mark_failed(
                rec.effect_id, principal,
                detail={"dead_letter": True, "reason": "lease_expired_max_attempts"},
                db_path=db_path,
            )
            dead += 1
        else:
            _ledger.mark_reconciliation_required(
                rec.effect_id, principal,
                detail={"reason": "lease_expired"},
                db_path=db_path,
            )
            reconciled += 1
    return {"reconciled": reconciled, "dead_lettered": dead}


def reconcile_to_terminal(
    effect_id: str,
    principal: Principal,
    *,
    committed: bool,
    db_path: Optional[Path] = None,
) -> str:
    """Resolve an ambiguous effect (``unknown`` / ``reconciliation_required``) to
    a terminal known state based on out-of-band proof: ``committed`` if the side
    effect is proven to have taken hold, else ``failed``. Returns the state."""
    if committed:
        rec = _ledger.mark_committed(effect_id, principal, db_path=db_path)
    else:
        rec = _ledger.mark_failed(
            effect_id, principal,
            detail={"reconciled": True}, db_path=db_path,
        )
    return rec.state.value
