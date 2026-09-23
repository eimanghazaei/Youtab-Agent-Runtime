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
    TERMINAL_STATES,
    ApprovalNotOpen,
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
    """Base for fail-closed approval-decision refusals raised by the adapter."""


# ``ApprovalNotOpen`` is the durable store's fail-closed signal (unknown run, run
# not in WAITING_APPROVAL, approval_id mismatch, or CAS lost). It is re-exported
# here so callers catch one type; the atomic decide lives in the store.
class ApprovalScopeMismatch(ApprovalError):
    """The run is outside the caller's tenant/workspace/principal scope."""


class ApprovalAlreadyDecided(ApprovalError):
    """Retained for compatibility. The store collapses an already-decided /
    replayed / concurrent-lost decision into :class:`ApprovalNotOpen` (the run is
    no longer in WAITING_APPROVAL), so this is no longer raised by the decide
    path; kept exported so existing callers/tests importing it keep working."""


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
        return self._authority is not None and self._lost_reason is None and self._authority.held

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

    # -- approval: require-open + atomic id-bound single-use; approve->once, deny->deny - #
    def open_approval(self, run_id: str, *, approval_id: str) -> Dict[str, Any]:
        """Atomically open one approval on a RUNNING run.

        The store binds the ID to the request event in the same fenced
        transaction. An exact-ID retry returns the existing open request;
        another ID cannot replace it. Emitting this from the worker/tool path
        remains the held D1 dispatch seam.
        """
        self._require_authority()
        return self._store.open_approval(run_id, approval_id=approval_id)

    def ensure_running(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Project a pre-decision run to RUNNING under the authority (idempotent).

        A worker requesting approval implies the run is executing, but the durable
        run is admitted QUEUED. Advance it along the validated path
        ``QUEUED -> CLAIMED -> RUNNING`` so an ``open_approval`` (whose from-state CAS
        requires RUNNING) can proceed. Also reconciles ``UNKNOWN -> RUNNING`` for a
        run whose prior owner died while an approval was pending: the gated effect has
        NOT executed (it is exactly what is awaiting approval) and the decision stays
        a single-use CAS, so re-awaiting after a takeover is safe and never
        re-executes an effect — this is what makes the durable pending approval
        visible after a restart WITHOUT a user click. Idempotent and race-tolerant:
        an already-RUNNING/awaiting/terminal run is left as-is and a lost CAS is
        swallowed. Never a second authority: these are the ONE store's fenced writes.
        """
        self._require_authority()
        row = self._store.get_run(run_id)
        if row is None:
            return None
        st = RunState(row["state"])
        if st in TERMINAL_STATES or st in (RunState.RUNNING, RunState.WAITING_APPROVAL):
            return row  # already running/awaiting or terminal — nothing to project
        if st == RunState.QUEUED:
            try:
                self._store.set_state(run_id, RunState.CLAIMED, kind="claimed", strict=True)
            except InvalidTransition:
                pass
            row = self._store.get_run(run_id)
            st = RunState(row["state"]) if row else st
        if st in (RunState.CLAIMED, RunState.UNKNOWN):
            try:
                self._store.set_state(run_id, RunState.RUNNING, kind="running", strict=True)
            except InvalidTransition:
                pass
        return self._store.get_run(run_id)

    def prior_decision(self, run_id: str, approval_id: str) -> Optional[str]:
        """Return the DURABLE prior decision for ``approval_id`` (``approve``/``deny``)
        or ``None``. The durable store is the authoritative replay source: a decided
        approval leaves an ``approval_approved``/``approval_denied`` event bound to
        the id, so a duplicate product request returns the ORIGINAL decision instead
        of re-consuming (or being refused by) the single-use CAS."""
        self._require_authority()
        decision_kinds = {"approval_approved": "approve", "approval_denied": "deny"}
        for ev in self._store.get_events(run_id):
            if ev.get("kind") in decision_kinds:
                if (ev.get("payload") or {}).get("approval_id") == approval_id:
                    return decision_kinds[ev["kind"]]
        return None

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
        """Decide an OPEN approval — atomic, approval-id-BOUND, single-use, fail-closed.

        1. ``session``/``always`` rejected (per-effect single use only): ValueError.
        2. Foreign scope -> :class:`ApprovalScopeMismatch` (scope fields are
           immutable on the row, so this pre-check is race-free).
        3. The decision itself is the durable store's atomic primitive
           :meth:`DurableRunStore.decide_open_approval` — ONE fenced
           ``BEGIN IMMEDIATE`` transaction that requires ``WAITING_APPROVAL``,
           requires the supplied ``approval_id`` to equal the OPEN approval's id
           (the latest ``approval_request`` event's ``approval_id``), and applies a
           from-state CAS (``UPDATE ... WHERE state='WAITING_APPROVAL'``,
           ``rowcount==1``). A WRONG id, a closed/absent approval, or a
           concurrent/replayed second decision is refused with
           :class:`ApprovalNotOpen` — never a double consumption, across
           concurrent requests and restart. ``deny`` leaves ``result_ref`` null
           (durable denied decision, no fabricated receipt). No second ledger.
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
            # Foreign tenant/principal/workspace scope (immutable -> race-free).
            raise ApprovalScopeMismatch("run is outside the caller's scope")

        target = RunState.RUNNING if d == "approve" else RunState.CANCELLED
        # The decision IS the store's atomic, fenced, approval-id-BOUND,
        # single-use primitive (one BEGIN IMMEDIATE txn: require WAITING_APPROVAL,
        # require approval_id == the OPEN approval id, from-state CAS). A wrong id,
        # closed/absent approval, or concurrent/replayed second decision raises
        # ApprovalNotOpen. No second ledger; no non-atomic get-then-transition.
        newrow = self._store.decide_open_approval(
            run_id,
            approval_id=approval_id,
            to_state=target,
            kind=("approval_approved" if d == "approve" else "approval_denied"),
            payload={"decision": d, "choice": choice},
        )
        return {
            "run_id": run_id,
            "approval_id": approval_id,
            "decision": d,
            "choice": choice,
            "state": newrow.get("state"),
        }
