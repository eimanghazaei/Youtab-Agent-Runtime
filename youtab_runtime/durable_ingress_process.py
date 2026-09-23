"""Process-singleton durable run authority for the product ``/api/runtime/v1`` ingress.

D1/D2 (owner-approved local design 2026-09-23): ONE effectful Runtime process
holds ONE fenced, owner-stamped durable RunStore authority — the SOLE authority
for run-state lifecycle, idempotency and approval. In server mode that authority
is a PostgreSQL session advisory lock; in local/SQLite mode it is an exclusive OS
file lock. Either way exactly one live instance may admit and commit run work
against a given store; a competing instance fails readiness and admission.

This module owns that single :class:`DurableRunStateAuthority` for the whole
process and exposes the seam the product create handler calls:

  * :func:`acquire_ingress_authority` — eager acquire at server startup (from the
    web-server lifespan) with a fail-stop loss hook; reconciles prior instances'
    nonterminal runs to UNKNOWN.
  * :func:`get_ingress_authority` — the acquired authority (lazily acquiring on
    first use), or ``None`` when durable ingress is disabled.
  * :func:`ingress_enabled` / :func:`ingress_ready` — feature + readiness gates
    (readiness mirrors the durable server: a lost/absent authority is NOT ready).
  * :func:`admit_managed_run` — the single-authority admission gate: owner-stamped
    ``admit`` with principal-scoped ``idempotency_key`` + ``request_digest`` dedup
    (``created`` distinguishes a fresh admit from the ORIGINAL run returned on a
    key hit); a same key with a different digest fails closed with
    :class:`IdempotencyConflict` (the caller maps it to product ``409``). Kanban
    then mirrors the SAME run id as execution transport only — never a second
    idempotency/lifecycle writer.
  * :func:`request_digest_for` — a stable canonical digest of the salient create
    request, so a replayed ``Idempotency-Key`` carrying a DIFFERENT request body
    is refused rather than silently returning the first run.

Enabling (opt-in, additive — the mature kanban path is unchanged when off):
    ``YOUTAB_AGENT_DURABLE_INGRESS`` in {"1","true","yes","on"}.
Backend/location reuse the durable env already used by the ``/v1/runs`` server:
    ``YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND`` (sqlite|postgres),
    ``YOUTAB_AGENT_DURABLE_PG_DSN`` (libpq DSN; direct or session-mode PgBouncer),
    ``YOUTAB_AGENT_DURABLE_DB_PATH`` (sqlite file path).

Fail-stop: on authority loss a module flag latches so :func:`ingress_ready`
returns ``False`` and every subsequent :func:`admit_managed_run` raises
``AuthorityLost`` (the store also fences each write in-transaction). The
web-server lifespan wires the hard process fail-stop via the ``on_lost`` hook.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any, Callable, Dict, Optional

from youtab_runtime.durable_ingress import (
    ApprovalNotOpen,
    ApprovalScopeMismatch,
    AuthorityLost,
    CreatedRun,
    DurableRunStateAuthority,
    IdempotencyConflict,
)
from youtab_runtime.durable_run_store import (
    ApprovalAlreadyOpen,
    InvalidTransition,
    RunState,
)

logger = logging.getLogger(__name__)

_ENABLE_ENV = "YOUTAB_AGENT_DURABLE_INGRESS"
_BACKEND_ENV = "YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND"
_PG_DSN_ENV = "YOUTAB_AGENT_DURABLE_PG_DSN"
_DB_PATH_ENV = "YOUTAB_AGENT_DURABLE_DB_PATH"

_TRUTHY = {"1", "true", "yes", "on"}

# Result of reconcile_pending_approval — the caller (status read) MUST derive the
# published pending-approval status from this, never from kanban alone (P1):
#   ACCEPTED     the sole authority holds the run in WAITING_APPROVAL for this id
#   NONE         nothing pending, or already decided durably (not pending)
#   UNAVAILABLE  the authority is absent/lost/holds no such run -> cannot accept
#   NOT_ACCEPTED authority healthy but could not open (run terminal / other id open)
PROJECTION_ACCEPTED = "accepted"
PROJECTION_NONE = "none"
PROJECTION_UNAVAILABLE = "unavailable"
PROJECTION_NOT_ACCEPTED = "not_accepted"

# One authority per process. Guarded by ``_lock`` for the acquire race only; the
# store itself is internally fenced and concurrency-safe for writes.
_lock = threading.Lock()
_authority: Optional[DurableRunStateAuthority] = None
_lost_reason: Optional[str] = None
_external_on_lost: Optional[Callable[[str], None]] = None

__all__ = [
    "ingress_enabled",
    "ingress_ready",
    "acquire_ingress_authority",
    "get_ingress_authority",
    "release_ingress_authority",
    "admit_managed_run",
    "request_digest_for",
    "open_managed_approval",
    "decide_managed_approval",
    "prior_approval_decision",
    "reconcile_pending_approval",
    "durable_approval_events",
    "PROJECTION_ACCEPTED",
    "PROJECTION_NONE",
    "PROJECTION_UNAVAILABLE",
    "PROJECTION_NOT_ACCEPTED",
    "reset_for_tests",
    "AuthorityLost",
    "IdempotencyConflict",
    "ApprovalNotOpen",
    "ApprovalScopeMismatch",
    "ApprovalAlreadyOpen",
    "InvalidTransition",
    "CreatedRun",
]


def ingress_enabled() -> bool:
    """True when the durable single-authority ingress is opt-in enabled."""
    return str(os.environ.get(_ENABLE_ENV, "")).strip().lower() in _TRUTHY


def _backend() -> str:
    return str(os.environ.get(_BACKEND_ENV, "") or "sqlite").strip().lower()


def _store_kwargs() -> Dict[str, Any]:
    backend = _backend()
    if backend == "postgres":
        dsn = os.environ.get(_PG_DSN_ENV)
        if not dsn:
            raise RuntimeError(
                f"{_BACKEND_ENV}=postgres requires {_PG_DSN_ENV} (a libpq DSN)"
            )
        return {"dsn": dsn}
    db_path = os.environ.get(_DB_PATH_ENV)
    return {"db_path": db_path} if db_path else {}


def _on_lost(reason: str) -> None:
    """Latch loss and fan out to the process fail-stop hook (set by the lifespan).

    Idempotent: only the first loss latches and notifies. A graceful release is
    not a loss and never reaches here.
    """
    global _lost_reason
    with _lock:
        if _lost_reason is not None:
            return
        _lost_reason = reason
        hook = _external_on_lost
    logger.critical("durable ingress authority LOST: %s", reason)
    if hook is not None:
        try:
            hook(reason)
        except Exception:  # noqa: BLE001 — a hook must never mask the loss
            logger.exception("durable ingress on_lost hook failed")


def acquire_ingress_authority(
    *,
    on_lost: Optional[Callable[[str], None]] = None,
    scope: Optional[str] = None,
) -> Optional[DurableRunStateAuthority]:
    """Acquire the one process authority (idempotent). Returns None when disabled.

    ``on_lost`` is the process fail-stop hook, invoked once if the authority is
    later lost/superseded. Reconciles prior instances' nonterminal owner-stamped
    runs to UNKNOWN (durable takeover semantics) as a side effect of acquire.
    Raises ``AuthorityHeld`` if another live instance already holds the store.
    """
    global _authority, _external_on_lost, _lost_reason
    if not ingress_enabled():
        return None
    with _lock:
        if on_lost is not None:
            _external_on_lost = on_lost
        if _authority is not None:
            return _authority
        auth = DurableRunStateAuthority(backend=_backend(), **_store_kwargs())
        reconciled = auth.acquire(scope=scope, on_lost=_on_lost)
        _authority = auth
        _lost_reason = None
    if reconciled:
        logger.warning(
            "durable ingress acquire reconciled %d prior nonterminal run(s) -> UNKNOWN",
            len(reconciled),
        )
    return _authority


def get_ingress_authority() -> Optional[DurableRunStateAuthority]:
    """The acquired authority (lazily acquiring on first use), or None if disabled."""
    if not ingress_enabled():
        return None
    if _authority is not None:
        return _authority
    return acquire_ingress_authority()


def ingress_ready() -> bool:
    """Readiness gate: enabled, acquired, not lost, and still holding the authority."""
    if not ingress_enabled():
        return False
    if _lost_reason is not None:
        return False
    auth = _authority
    return auth is not None and auth.ready()


def release_ingress_authority() -> None:
    """Graceful release (server shutdown). Not a loss; never triggers fail-stop."""
    global _authority
    with _lock:
        auth, _authority = _authority, None
    if auth is not None:
        auth.release()


def request_digest_for(fields: Dict[str, Any]) -> str:
    """Stable canonical digest of the salient create-request fields.

    Two requests with the same ``Idempotency-Key`` must carry the same digest to
    be treated as a replay of the same run; a different digest is a conflict. The
    canonical form is sorted-key, compact JSON so key order / whitespace never
    changes the digest. ``None`` values are dropped so an omitted optional field
    equals its explicit-null form.
    """
    canon = {k: v for k, v in fields.items() if v is not None}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def admit_managed_run(
    *,
    run_id: str,
    tenant_id: str,
    workspace_id: str,
    principal_id: str,
    agent_id: str,
    organization_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    request_digest: Optional[str] = None,
    execution_deadline: Optional[float] = None,
) -> CreatedRun:
    """Single-authority admission gate for the product ingress.

    Owner-stamped ``admit`` on the one fenced durable store, idempotent by the
    scoped key ``(tenant, workspace, principal, operation='run', idempotency_key)``:
      * fresh key      -> ``created=True`` with ``run_id`` (the caller's canonical id);
      * same key+digest -> ``created=False`` with the ORIGINAL run row;
      * same key, diff digest -> raises :class:`IdempotencyConflict` (caller -> 409).

    ``organization_id`` defaults to ``tenant_id`` when a distinct org is not
    modelled by the ingress identity. Raises :class:`AuthorityLost` when the
    authority is absent/lost (fail closed — never a silent non-durable admit).
    """
    if _lost_reason is not None:
        raise AuthorityLost(f"durable ingress authority lost: {_lost_reason}")
    auth = get_ingress_authority()
    if auth is None:
        raise AuthorityLost("durable ingress authority not available")
    return auth.create_run(
        run_id=run_id,
        task_id=run_id,
        tenant_id=tenant_id,
        organization_id=organization_id or tenant_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
        agent_id=agent_id,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        execution_deadline=execution_deadline,
        initial_state=RunState.QUEUED,
    )


def open_managed_approval(run_id: str, *, approval_id: str) -> Dict[str, Any]:
    """Project a worker's APPROVAL_REQUEST into a durable open approval (D2).

    The worker signals an approval need over the kanban transport; the SERVER —
    the sole holder of the run authority — opens it durably here: ensure the run is
    RUNNING, then ``open_approval`` (RUNNING -> WAITING_APPROVAL, id-bound, and
    idempotent for an exact-id retry). The worker never writes the durable store;
    kanban stays a rebuildable transport projection. Raises :class:`AuthorityLost`
    when the authority is absent/lost (fail closed).
    """
    if _lost_reason is not None:
        raise AuthorityLost(f"durable ingress authority lost: {_lost_reason}")
    auth = get_ingress_authority()
    if auth is None:
        raise AuthorityLost("durable ingress authority not available")
    auth.ensure_running(run_id)
    return auth.open_approval(run_id, approval_id=approval_id)


def reconcile_pending_approval(run_id: str, *, pending_approval_id: Optional[str]) -> str:
    """Ingest a worker's PENDING approval request into the durable RunStore under the
    fenced server authority (D2 P1), invoked when the server OBSERVES the request over
    the kanban transport, and RETURN what the authority actually did so the caller can
    publish a TRUTHFUL status.

    The worker never writes the durable store; the SERVER — the sole authority holder
    — opens the SAME durable run's approval so the canonical store (not kanban alone)
    holds the pending request, id-bound and scoped, and can be rebuilt if kanban is
    lost. Idempotent: a run already WAITING_APPROVAL for this id, or one just opened,
    both return ``PROJECTION_ACCEPTED``.

    Crucially this does NOT silently succeed on a lost/absent authority: it returns
    ``PROJECTION_UNAVAILABLE`` so the caller publishes an explicit unknown rather than
    a kanban-derived ``awaiting_approval`` the authority never accepted (the P1 lie).
    A healthy authority that cannot open (run terminal / a different id already open)
    returns ``PROJECTION_NOT_ACCEPTED``; an already-decided or absent request returns
    ``PROJECTION_NONE``.
    """
    if pending_approval_id is None:
        return PROJECTION_NONE
    if _lost_reason is not None:
        return PROJECTION_UNAVAILABLE
    auth = get_ingress_authority()
    if auth is None:
        return PROJECTION_UNAVAILABLE
    try:
        if auth.prior_decision(run_id, pending_approval_id) is not None:
            return PROJECTION_NONE  # already decided durably — not pending
        row = auth.get_run(run_id)
        if row is None:
            return PROJECTION_UNAVAILABLE  # authority holds no such run — cannot confirm
        if row.get("state") == RunState.WAITING_APPROVAL.value:
            return PROJECTION_ACCEPTED  # already open under the authority
        auth.ensure_running(run_id)
        auth.open_approval(run_id, approval_id=pending_approval_id)
        return PROJECTION_ACCEPTED
    except AuthorityLost:
        return PROJECTION_UNAVAILABLE
    except (ApprovalNotOpen, ApprovalAlreadyOpen, InvalidTransition):
        # Healthy authority but could not open (run terminal, or a different id is the
        # open one): re-read durable truth and report it, never a false pending.
        try:
            row = auth.get_run(run_id)
        except AuthorityLost:
            return PROJECTION_UNAVAILABLE
        if row is not None and row.get("state") == RunState.WAITING_APPROVAL.value:
            return PROJECTION_ACCEPTED
        return PROJECTION_NOT_ACCEPTED


def durable_approval_events(run_id: str) -> List[Dict[str, Any]]:
    """The run's approval history AS RECORDED IN THE CANONICAL DURABLE STORE, as
    ``{"kind": "request"|"decision", "approval_id": str, "decision"?: "approve"|"deny"}``
    in durable order. This is the source for rebuilding the kanban approval transport
    projection from the RunStore (proving kanban is a rebuildable projection, not the
    authority). Raises :class:`AuthorityLost` when the authority is absent/lost."""
    if _lost_reason is not None:
        raise AuthorityLost(f"durable ingress authority lost: {_lost_reason}")
    auth = get_ingress_authority()
    if auth is None:
        raise AuthorityLost("durable ingress authority not available")
    kinds = {"approval_approved": "approve", "approval_denied": "deny"}
    out: List[Dict[str, Any]] = []
    for ev in auth.get_events(run_id):
        k = ev.get("kind")
        aid = (ev.get("payload") or {}).get("approval_id")
        if not aid:
            continue
        if k == "approval_request":
            out.append({"kind": "request", "approval_id": aid})
        elif k in kinds:
            out.append({"kind": "decision", "approval_id": aid, "decision": kinds[k]})
    return out


def decide_managed_approval(
    run_id: str,
    *,
    approval_id: str,
    decision: str,
    tenant_id: str,
    workspace_id: str,
    principal_id: str,
) -> Dict[str, Any]:
    """Decide an OPEN approval via the canonical fenced durable CAS (D2).

    Thin pass-through to the adapter's atomic, approval-id-bound, single-use
    ``decide_approval`` (approve -> once, deny -> deny; scope-checked; fail closed).
    Raises :class:`AuthorityLost` when the authority is absent/lost.
    """
    if _lost_reason is not None:
        raise AuthorityLost(f"durable ingress authority lost: {_lost_reason}")
    auth = get_ingress_authority()
    if auth is None:
        raise AuthorityLost("durable ingress authority not available")
    return auth.decide_approval(
        run_id,
        approval_id=approval_id,
        decision=decision,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
    )


def prior_approval_decision(run_id: str, approval_id: str) -> Optional[str]:
    """Authoritative durable replay lookup: the prior decision for ``approval_id``
    (``approve``/``deny``) or ``None``. Lets the product route return the ORIGINAL
    decision on a duplicate instead of re-consuming the single-use CAS."""
    if _lost_reason is not None:
        raise AuthorityLost(f"durable ingress authority lost: {_lost_reason}")
    auth = get_ingress_authority()
    if auth is None:
        raise AuthorityLost("durable ingress authority not available")
    return auth.prior_decision(run_id, approval_id)


def reset_for_tests() -> None:
    """Release and clear the process singleton (test isolation only)."""
    global _authority, _lost_reason, _external_on_lost
    with _lock:
        auth, _authority = _authority, None
        _lost_reason = None
        _external_on_lost = None
    if auth is not None:
        try:
            auth.release()
        except Exception:  # noqa: BLE001
            pass
