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

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

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
    EffectAlreadyClaimed,
    EffectClaimRefused,
    InvalidTransition,
    RunState,
)

logger = logging.getLogger(__name__)

_ENABLE_ENV = "YOUTAB_AGENT_DURABLE_INGRESS"
_BACKEND_ENV = "YOUTAB_AGENT_DURABLE_RUNSTORE_BACKEND"
_PG_DSN_ENV = "YOUTAB_AGENT_DURABLE_PG_DSN"
_DB_PATH_ENV = "YOUTAB_AGENT_DURABLE_DB_PATH"

# The broad runtime service secret. The per-run worker capability is DERIVED from it
# (a distinct HMAC key), so a client presenting the service secret itself does NOT
# authenticate the worker-only endpoints, and vice versa.
_SERVICE_SECRET_ENV = "YOUTAB_AGENT_RUNTIME_SERVICE_SECRET"
# Optional explicit self-URL the launcher hands the worker for the callback endpoint.
_SELF_URL_ENV = "YOUTAB_AGENT_RUNTIME_SELF_URL"
_WORKER_CAP_LABEL = b"youtab.worker-approval-cap.v2"
# Default capability TTL (seconds) when the admitted run carries no execution deadline.
_CAP_DEFAULT_TTL = 24 * 3600

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

# Run states in which a per-run worker capability is REVOKED (the endpoint refuses):
# terminal, cancelling, and the terminal-uncertain UNKNOWN/RECONCILIATION_REQUIRED.
_CAP_INACTIVE_STATES = frozenset({
    RunState.SUCCEEDED.value, RunState.FAILED.value, RunState.CANCELLED.value,
    RunState.CANCELLING.value, RunState.UNKNOWN.value,
    RunState.RECONCILIATION_REQUIRED.value,
})

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
    "durable_run_state",
    "durable_approval_events",
    "mint_worker_capability",
    "verify_worker_capability",
    "authorize_worker_capability",
    "worker_ingress_base_url",
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
    "EffectClaimRefused",
    "EffectAlreadyClaimed",
    "record_admission_binding",
    "admission_command_id",
    "claim_managed_effect",
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


def open_managed_approval(run_id: str, *, approval_id: str,
                          payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Open a worker's approval request on the durable RunStore, durable-FIRST (D2).

    The SERVER — the sole holder of the run authority — opens it durably here: ensure
    the run is RUNNING, then ``open_approval`` (RUNNING -> WAITING_APPROVAL, id-bound,
    idempotent for an exact-id retry). ``payload`` carries the request metadata
    (effect_digest, action, mode) recorded on the durable ``approval_request`` event so
    the request is bound to a specific effect. The worker never writes the durable
    store; kanban stays a rebuildable transport projection. Raises
    :class:`AuthorityLost` when the authority is absent/lost (fail closed).
    """
    if _lost_reason is not None:
        raise AuthorityLost(f"durable ingress authority lost: {_lost_reason}")
    auth = get_ingress_authority()
    if auth is None:
        raise AuthorityLost("durable ingress authority not available")
    auth.ensure_running(run_id)
    return auth.open_approval(run_id, approval_id=approval_id, payload=payload)


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


def durable_run_state(run_id: str) -> Dict[str, Any]:
    """Pure READ of the durable run state for a truthful status publish — NO ingest,
    NO write (the dispatcher-tick boundary owns ingest). Returns
    ``{"available": False, "state": None}`` when the authority is lost/absent/not yet
    acquired or the run is unknown to it (so the caller publishes ``unknown``, never a
    kanban-derived pending); else ``{"available": True, "state": <RunState value>}``.
    Deliberately does NOT lazily acquire the authority: a status read must not take the
    exclusive run authority as a side effect."""
    if _lost_reason is not None:
        return {"available": False, "state": None}
    auth = _authority  # never lazily acquire on a read path
    if auth is None or not auth.ready():
        return {"available": False, "state": None}
    try:
        row = auth.get_run(run_id)
    except Exception:  # noqa: BLE001 — a read must never raise into the caller
        return {"available": False, "state": None}
    if row is None:
        return {"available": False, "state": None}
    return {"available": True, "state": row.get("state")}


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
            p = ev.get("payload") or {}
            out.append({"kind": "request", "approval_id": aid,
                        "effect_digest": p.get("effect_digest"),
                        "action": p.get("action"), "mode": p.get("mode"),
                        "authorization_id": p.get("authorization_id"),
                        "checkpoint_digest": p.get("checkpoint_digest")})
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


def record_admission_binding(run_id: str, *, command_id: Optional[str],
                             authorization_epoch: Optional[int] = None,
                             expires_at: Optional[float] = None) -> None:
    """Record the admitted-grant binding (command_id + authority epoch/expiry) on the
    durable run at create so the worker capability and effect claim bind to the exact
    grant. No-op when durable ingress is off; raises :class:`AuthorityLost` when lost."""
    if _lost_reason is not None:
        raise AuthorityLost(f"durable ingress authority lost: {_lost_reason}")
    auth = get_ingress_authority()
    if auth is None:
        return
    auth.record_admission_binding(run_id, {
        "command_id": command_id, "authorization_epoch": authorization_epoch,
        "expires_at": expires_at,
    })


def admission_command_id(run_id: str) -> Optional[str]:
    """The run's admitted-grant command_id (from the durable admission binding), or
    None. A pure read used to server-fill the request binding and the effect claim."""
    if _lost_reason is not None:
        return None
    auth = _authority
    if auth is None or not auth.ready():
        return None
    try:
        return (auth.admission_binding(run_id) or {}).get("command_id")
    except Exception:  # noqa: BLE001
        return None


def claim_managed_effect(run_id: str, *, approval_id: str, attempt_id: str,
                         binding: Dict[str, Any], ttl_seconds: float = 60.0) -> Dict[str, Any]:
    """Single-use effect fence: durable, fenced CAS that yields at most one effect apply
    (approve-decided + active + binding-matched + unclaimed). Raises
    ``EffectClaimRefused`` / ``EffectAlreadyClaimed`` / ``AuthorityLost`` fail-closed."""
    if _lost_reason is not None:
        raise AuthorityLost(f"durable ingress authority lost: {_lost_reason}")
    auth = get_ingress_authority()
    if auth is None:
        raise AuthorityLost("durable ingress authority not available")
    return auth.claim_effect(run_id, approval_id=approval_id, attempt_id=attempt_id,
                             binding=binding, ttl_seconds=ttl_seconds)


def _worker_cap_secret() -> Optional[bytes]:
    """The per-run worker-capability signing key: a DISTINCT key derived from the
    runtime service secret via HMAC, so the capability is not the service secret and
    the service secret is not a capability. Returns None when no service secret is
    configured (the worker-only endpoints then cannot authenticate — fail closed)."""
    secret = os.environ.get(_SERVICE_SECRET_ENV)
    if not secret:
        return None
    return hmac.new(secret.encode("utf-8"), _WORKER_CAP_LABEL, hashlib.sha256).digest()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_dec(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _cap_sign(payload: Dict[str, Any], key: bytes) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return _b64u(body) + "." + _b64u(sig)


def mint_worker_capability(run_id: str) -> Optional[str]:
    """Mint the per-run worker capability the launcher carries to the worker
    (``build_worker_invocation`` env).

    v2: a SIGNED token whose payload BINDS the admitted run's scope
    (tenant/principal/workspace), the current authority ``epoch`` and an expiry, read
    from the LIVE durable run. Unforgeable without the derived key; it authorizes ONLY
    this run's worker-only endpoints, only while the same authority epoch holds (a
    takeover bumps the epoch and revokes it), and only until expiry. The endpoint
    additionally re-checks scope/epoch/state against the live run at each call
    (revocation on cancel/terminal/UNKNOWN). Returns None when durable ingress is off,
    no service secret is configured, or the run is not admitted/authority absent.

    NOT YET bound: the admitted signed-grant command_id (grant-identity binding). That
    is staged (R1 NO-GO) — it needs the create path to persist the grant command_id on
    the durable run and a cross-owner canonical-id agreement.
    """
    if not ingress_enabled():
        return None
    key = _worker_cap_secret()
    if key is None or not run_id or _lost_reason is not None:
        return None
    auth = _authority
    if auth is None or not auth.ready():
        return None
    try:
        row = auth.get_run(run_id)
        epoch = auth.epoch
    except Exception:  # noqa: BLE001 — never raise from minting
        return None
    if row is None:
        return None
    deadline = row.get("execution_deadline")
    exp = float(deadline) if deadline else time.time() + _CAP_DEFAULT_TTL
    binding = auth.admission_binding(run_id) or {}
    payload = {
        "v": 2, "run_id": run_id, "tenant": row.get("tenant_id"),
        "principal": row.get("principal_id"), "workspace": row.get("workspace_id"),
        "epoch": int(epoch), "exp": exp,
        "command_id": binding.get("command_id"),  # the admitted-grant command id (or None)
    }
    return _cap_sign(payload, key)


def _decode_capability(run_id: str, cap: Optional[str]) -> Optional[Dict[str, Any]]:
    """Verify the token signature, run_id binding and expiry (stateless). Returns the
    claims dict, or None. Does NOT check live scope/epoch/state — see
    :func:`authorize_worker_capability`."""
    if not cap or not run_id:
        return None
    key = _worker_cap_secret()
    if key is None:
        return None
    try:
        body_b64, sig_b64 = str(cap).split(".", 1)
        body = _b64u_dec(body_b64)
        sig = _b64u_dec(sig_b64)
    except Exception:  # noqa: BLE001 — malformed token
        return None
    expected = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        claims = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    if claims.get("v") != 2 or claims.get("run_id") != run_id:
        return None
    try:
        if float(claims.get("exp") or 0) <= time.time():
            return None  # expired
    except (TypeError, ValueError):
        return None
    return claims


def verify_worker_capability(run_id: str, cap: Optional[str]) -> bool:
    """Stateless integrity check: valid signature, matching run_id, not expired.
    Rejects arbitrary clients (no/wrong cap) and a cap minted for another run. Callers
    that mutate/read run state MUST use :func:`authorize_worker_capability`, which also
    enforces live scope/epoch/state revocation."""
    return _decode_capability(run_id, cap) is not None


def authorize_worker_capability(run_id: str, cap: Optional[str]) -> Tuple[bool, str]:
    """Full per-call authorization for a worker-only endpoint. Verifies the token
    (signature/run_id/expiry) AND re-checks it against the LIVE durable run:
      * scope (tenant/principal/workspace) in the token must equal the run's;
      * the token epoch must equal the CURRENT authority epoch (a takeover revokes it);
      * the run must not be terminal/CANCELLED/UNKNOWN (cancel/reconcile revokes it).
    Returns (ok, reason). reason is one of: ok | unauthorized_worker |
    run_authority_unavailable | run_not_found | run_not_active. Fail-closed.
    """
    if _lost_reason is not None:
        return (False, "run_authority_unavailable")
    claims = _decode_capability(run_id, cap)
    if claims is None:
        return (False, "unauthorized_worker")
    auth = _authority
    if auth is None or not auth.ready():
        return (False, "run_authority_unavailable")
    try:
        row = auth.get_run(run_id)
        epoch = auth.epoch
    except Exception:  # noqa: BLE001
        return (False, "run_authority_unavailable")
    if row is None:
        return (False, "run_not_found")
    if (claims.get("tenant"), claims.get("principal"), claims.get("workspace")) != (
        row.get("tenant_id"), row.get("principal_id"), row.get("workspace_id")
    ):
        return (False, "unauthorized_worker")  # scope drift
    if int(claims.get("epoch") or -1) != int(epoch):
        return (False, "unauthorized_worker")  # superseded by a takeover (revoked)
    # command_id must equal the run's admitted-grant command id (grant-identity binding)
    binding = auth.admission_binding(run_id) or {}
    if claims.get("command_id") != binding.get("command_id"):
        return (False, "unauthorized_worker")
    state = row.get("state")
    if state in _CAP_INACTIVE_STATES:
        return (False, "run_not_active")  # cancelled/terminal/UNKNOWN -> revoked
    return (True, "ok")


def worker_ingress_base_url() -> Optional[str]:
    """The base URL the launcher hands the worker for the callback endpoint, or None
    (the worker then has no transport — that wiring is the worker-owner's integration)."""
    url = os.environ.get(_SELF_URL_ENV)
    return url.rstrip("/") if url else None


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
