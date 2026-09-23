"""Transport-agnostic, authority-bearing run-state adapter for the product
``/api/runtime/v1`` ingress (R1).

This binds the product ingress to the SINGLE durable run authority
(:mod:`youtab_runtime.durable_run_store`) so run creation, status, event replay,
principal-scoped idempotency and approval-decision durability are owned by ONE
fenced store object — never a second ledger and never an unfenced write path.

Deliberately NOT here (held pending the D1 execution-transport decision): the
create-to-worker DISPATCH seam. This adapter carries only run STATE; how a worker
is spawned and bound to the durable ``run_id`` is left to the owner-decided
transport so that the durable RunStore remains the ONLY run-state/idempotency
authority.

Design constraints honored (from the R4/Timeout owner, integrated SHA
``a1f9e33428``):
  * runs are admitted **owner-stamped** via ``store.admit(..., owner=authority.owner)``
    — never ``create_run`` (which is ownerless and would never be recovered);
  * the fence lives on ONE ``_authority``-bearing store instance, so this adapter
    holds that single store and every run write goes through it;
  * on authority loss the caller must fail-stop (readiness off, admission
    refused); ``AuthorityLost`` propagates from any write;
  * approval decisions map ``approve -> once`` / ``deny -> deny`` only — the wider
    ``session``/``always`` durable choices are never exposed (per-effect single
    use).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from youtab_runtime.durable_run_authority import AuthorityLost
from youtab_runtime.durable_run_store import (
    IdempotencyConflict,
    RunIdentity,
    RunState,
    create_run_store,
)

__all__ = [
    "DurableRunStateAuthority",
    "CreatedRun",
    "IdempotencyConflict",
    "AuthorityLost",
    "APPROVAL_DECISION_TO_CHOICE",
]

# Product approval decision -> durable single-use choice. ``session`` / ``always``
# are intentionally absent: the product surface is per-effect single use only.
APPROVAL_DECISION_TO_CHOICE = {"approve": "once", "deny": "deny"}


@dataclass
class CreatedRun:
    """Result of an idempotent admit. ``created`` is False when a prior
    idempotency key returned the ORIGINAL run (the generated run_id was not used)."""

    row: Dict[str, Any]
    created: bool


class DurableRunStateAuthority:
    """Single-authority run-state facade for the product ingress.

    One instance per process holds the exclusive run authority (PostgreSQL session
    advisory lock in server mode, OS file lock in local/SQLite mode). Construct,
    then :meth:`acquire` at startup; every mutating call is fenced against that one
    authority and raises :class:`AuthorityLost` after a takeover/loss.
    """

    def __init__(self, *, store: Any = None, backend: Optional[str] = None, **store_kwargs: Any) -> None:
        # Exactly one authoritative store (never a second one). Injectable for tests.
        self._store = store if store is not None else create_run_store(backend, **store_kwargs)
        self._authority = None
        self._lost_reason: Optional[str] = None
        self._on_lost: Optional[Callable[[str], None]] = None

    # -- lifecycle --------------------------------------------------------- #
    def acquire(
        self,
        *,
        scope: Optional[str] = None,
        supervise_interval: Optional[float] = None,
        on_lost: Optional[Callable[[str], None]] = None,
    ) -> List[str]:
        """Acquire the exclusive run authority, reconcile prior instances' runs to
        UNKNOWN, and arm the loss listener. Returns the reconciled run_ids
        (prior nonterminal owner-stamped ``operation='run'`` rows). Raises
        ``AuthorityHeld`` if another live instance already holds it."""
        self._on_lost = on_lost
        self._lost_reason = None
        self._authority = self._store.acquire_instance_authority(
            scope=scope, supervise_interval=supervise_interval
        )
        self._authority.add_loss_listener(self._handle_loss)
        return self._store.reconcile_prior_instances(self._authority)

    def _handle_loss(self, reason: str) -> None:
        self._lost_reason = reason
        if self._on_lost is not None:
            try:
                self._on_lost(reason)
            except Exception:  # noqa: BLE001 — a listener must never mask the loss
                pass

    def ready(self) -> bool:
        """True only while this instance still holds the authority. Mirrors the
        durable server readiness gate: a lost/absent authority is not ready."""
        return self._authority is not None and self._lost_reason is None and self._authority.held()

    def release(self) -> None:
        if self._authority is not None:
            self._authority.release()

    def _require_authority(self) -> None:
        if self._authority is None or self._lost_reason is not None:
            raise AuthorityLost(self._lost_reason or "run authority not held")

    @property
    def owner(self) -> str:
        self._require_authority()
        return self._authority.owner

    @property
    def store(self) -> Any:
        return self._store

    # -- create (owner-stamped admit + principal-key dedup) ---------------- #
    def create_run(
        self,
        *,
        tenant_id: str,
        organization_id: str,
        workspace_id: str,
        principal_id: str,
        agent_id: str,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        request_digest: Optional[str] = None,
        execution_deadline: Optional[float] = None,
        initial_state: RunState = RunState.QUEUED,
    ) -> CreatedRun:
        """Admit a new run under the single authority (owner-stamped, atomic).

        Idempotent by the scoped key ``(tenant, workspace, principal, operation,
        idempotency_key)``: the same key + the same ``request_digest`` returns the
        ORIGINAL run (``created=False``); the same key + a DIFFERENT digest raises
        :class:`IdempotencyConflict` (the caller maps it to product ``409``).
        """
        self._require_authority()
        rid = run_id or uuid.uuid4().hex
        identity = RunIdentity(
            task_id=task_id or rid,
            run_id=rid,
            tenant_id=tenant_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            agent_id=agent_id,
            operation="run",
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            execution_deadline=execution_deadline,
        )
        row = self._store.admit(identity, owner=self.owner, initial_state=initial_state)
        # A dedup return carries the ORIGINAL run_id (!= the one we generated).
        return CreatedRun(row=row, created=(row.get("run_id") == rid))

    # -- reads (projection source) ----------------------------------------- #
    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get_run(run_id)

    def get_events(self, run_id: str, *, from_seq: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        """Durable event replay (``seq > from_seq``). An UNKNOWN run yields no
        further live events and must be treated as terminal-uncertain."""
        return self._store.get_events(run_id, from_seq=from_seq, limit=limit)

    # -- approval: approve->once, deny->deny; never session|always --------- #
    def record_approval_decision(self, run_id: str, *, approval_id: str, decision: str) -> Dict[str, Any]:
        """Durably record an approval decision on the run.

        Maps the product decision to the single-use durable choice
        (``approve->once``, ``deny->deny``); ``session``/``always`` are rejected.
        Idempotent by ``approval_id`` (the original decision stands). A ``deny``
        durably terminates the run cancelled with reason ``approval_denied`` (zero
        effect, no fabricated result). An ``approve`` records the once-decision;
        the worker continuation is the held dispatch seam.
        """
        self._require_authority()
        d = (decision or "").strip().lower()
        if d not in APPROVAL_DECISION_TO_CHOICE:
            raise ValueError("decision must be approve or deny")
        choice = APPROVAL_DECISION_TO_CHOICE[d]
        # Idempotent by approval_id — the ORIGINAL decision stands on replay.
        for ev in self._store.get_events(run_id, from_seq=0, limit=1_000_000):
            if ev.get("kind") == "approval_decision" and (ev.get("payload") or {}).get("approval_id") == approval_id:
                prior = ev.get("payload") or {}
                return {
                    "run_id": run_id,
                    "approval_id": approval_id,
                    "decision": prior.get("decision"),
                    "choice": prior.get("choice"),
                    "already_decided": True,
                }
        self._store.append_event(
            run_id, "approval_decision", {"approval_id": approval_id, "decision": d, "choice": choice}
        )
        if d == "deny":
            # Terminal, immutable; result stays null (no fabricated receipt).
            self._store.set_state(
                run_id,
                RunState.CANCELLED,
                strict=False,
                kind="approval_denied",
                payload={"approval_id": approval_id, "reason": "approval_denied"},
            )
        return {"run_id": run_id, "approval_id": approval_id, "decision": d, "choice": choice}
