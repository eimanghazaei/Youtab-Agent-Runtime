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
    InvalidTransition,
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
    "ApprovalNotOpen",
    "ApprovalScopeMismatch",
    "ApprovalAlreadyDecided",
]

# Product approval decision -> durable single-use choice. ``session`` / ``always``
# are intentionally absent: the product surface is per-effect single use only.
APPROVAL_DECISION_TO_CHOICE = {"approve": "once", "deny": "deny"}


class ApprovalError(Exception):
    """Base for fail-closed approval-decision refusals."""


class ApprovalNotOpen(ApprovalError):
    """No open approval to decide (unknown run, or run not in WAITING_APPROVAL)."""


class ApprovalScopeMismatch(ApprovalError):
    """The run is outside the caller's tenant/workspace/principal scope."""


class ApprovalAlreadyDecided(ApprovalError):
    """The open approval was already consumed (concurrent/replayed decision)."""


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

    # -- approval: require-open + atomic single-use; approve->once, deny->deny - #
    def decide_approval(
        self,
        run_id: str,
        *,
        approval_id: str,
        decision: str,
        tenant_id: str,
        workspace_id: str,
        principal_id: str,
    ) -> Dict[str, Any]:
        """Decide an OPEN approval, atomically and single-use, fail-closed.

        Enforced with the durable store's existing atomic primitive (no second
        ledger, no non-atomic get-events→append):

        1. ``session``/``always`` are rejected (per-effect single use only).
        2. Foreign scope: the run must belong to the caller's
           tenant/workspace/principal, else :class:`ApprovalScopeMismatch`.
        3. Require-open: the run must be in ``WAITING_APPROVAL`` (an open
           approval), else :class:`ApprovalNotOpen` (covers unknown/closed).
        4. Single-use: the decision is the atomic state transition
           ``WAITING_APPROVAL -> RUNNING`` (approve) / ``-> CANCELLED`` (deny).
           ``transition`` runs under ``BEGIN IMMEDIATE`` + the validated
           transition table + terminal immutability, so exactly ONE decision
           consumes the ``WAITING_APPROVAL`` edge; a concurrent/replayed second
           decision finds the run past ``WAITING_APPROVAL`` and is refused
           (:class:`ApprovalAlreadyDecided`) — never a double consumption, across
           concurrent requests and restart (the state is durable). ``deny``
           leaves ``result_ref`` null (durable denied decision, no fabricated
           receipt).

        HELD interface request (see R1_INTEGRATION_BOUNDARY_SPEC §9): an
        approval_id-BOUND, from-state-guarded CAS decide primitive is not present
        in the durable store (the schema has no pending-approval-id column). Only
        ONE approval is open per run at a time (``WAITING_APPROVAL`` is a single
        state), so this decides THE open approval and records ``approval_id`` for
        audit; binding the decision to a specific ``approval_id`` atomically
        (multi-approval) requires a store primitive owned by the durable owner and
        the D1 dispatch/worker path that opens the approval — both HELD.
        """
        self._require_authority()
        d = (decision or "").strip().lower()
        if d not in APPROVAL_DECISION_TO_CHOICE:
            raise ValueError("decision must be approve or deny")
        choice = APPROVAL_DECISION_TO_CHOICE[d]

        row = self._store.get_run(run_id)
        if row is None:
            raise ApprovalNotOpen(f"run {run_id} not found")
        if (
            row.get("tenant_id"),
            row.get("workspace_id"),
            row.get("principal_id"),
        ) != (tenant_id, workspace_id, principal_id):
            # Foreign tenant/principal/workspace: not-found to the caller.
            raise ApprovalScopeMismatch("run is outside the caller's scope")
        if row.get("state") != RunState.WAITING_APPROVAL.value:
            raise ApprovalNotOpen(
                f"no open approval (run state={row.get('state')})"
            )

        target = RunState.RUNNING if d == "approve" else RunState.CANCELLED
        try:
            newrow = self._store.transition(
                run_id,
                target,
                kind=("approval_approved" if d == "approve" else "approval_denied"),
                payload={"approval_id": approval_id, "decision": d, "choice": choice},
            )
        except InvalidTransition as exc:
            # The WAITING_APPROVAL edge was already consumed (concurrent/replayed).
            raise ApprovalAlreadyDecided(
                f"approval for run {run_id} already decided"
            ) from exc
        return {
            "run_id": run_id,
            "approval_id": approval_id,
            "decision": d,
            "choice": choice,
            "state": newrow.get("state"),
        }
