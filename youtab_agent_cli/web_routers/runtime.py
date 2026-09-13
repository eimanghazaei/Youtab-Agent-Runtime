"""Agent Runtime product surface — ``/api/runtime/v1`` (AR-PROD-01, Milestone 1).

A thin, service-authenticated router that exposes the engine's **real** run
lifecycle to the youtab-ai-os product gateway. It wraps the existing kanban
engine (``kanban_db.create_task`` + ``dispatch_once`` + ``list_events`` +
``list_runs`` + ``list_attachments``) — the single authoritative run store — and
introduces **no new run state machine and no duplicate agent/run store**.

Roles (fixed by the AR-PROD-01 topology):

    Web OS / Windows / iOS  ->  youtab-ai-os /v1/agents  (control plane: product
      auth, tenant/user authz, entitlements, audit, public contracts)
        ->  AgentRuntimeConnector  ->  THIS surface  ->  kanban engine
      (agents, runs, dispatcher, retries, tools, skills, sandbox execution,
       events, artifacts).

Security boundary (two distinct trust facts, both required, fail-closed):

  1. **Service identity** — the request must have been authenticated by the
     bundled ``runtime-service`` dashboard-auth provider (shared bearer secret,
     constant-time compare). The ``require_service_identity`` dependency refuses
     any request whose ``token_principal`` does not carry the ``runtime`` scope.
     The browser never holds this secret and never reaches this surface directly.
  2. **End-user identity** — the gateway is the identity authority; it forwards
     the *verified* end-user context in headers (``X-Youtab-Tenant-Id`` /
     ``-User-Id`` / ``-Roles`` / ``-Correlation-Id``). The engine NEVER trusts a
     client-supplied tenant/user; these headers are honoured only because the
     request already cleared the service-secret gate. Missing tenant/user =>
     fail-closed 403.

Mutating commands (create-run, cancel, retry) additionally require a signed
command envelope (see :mod:`youtab_agent_cli.runtime_command_auth`):
signature + timestamp + expiry + nonce + replay protection, bound to the
tenant/user so a captured command cannot be lifted onto another identity.

Cross-tenant / cross-user isolation is enforced at EVERY read: a run is visible
only to the (tenant, user) that created it; a mismatch reads as 404 (never leaks
existence).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from youtab_agent_cli import agent_identity
from youtab_agent_cli import effective_binding as eb
from youtab_agent_cli import engine_connection
from youtab_agent_cli import kanban_db as kb
from youtab_agent_cli import runtime_command_auth as rca

_log = logging.getLogger("youtab_agent_cli.web_server")

router = APIRouter()

# Contract version this engine surface implements. The connector negotiates
# against this on connect; a mismatch surfaces as ``runtime_incompatible``
# rather than a silent empty success.
CONTRACT_VERSION = "1"

# Product runs live on a dedicated board so they are cleanly separated from any
# other kanban usage of the same home. Tenant filtering is the primary isolation
# invariant; the board is defence-in-depth. Overridable for tests / deploys.
RUNTIME_BOARD = os.environ.get("YOUTAB_AGENT_RUNTIME_BOARD", "runtime") or "runtime"

# Gateway-verified identity headers (lowercased for case-insensitive reads).
_H_TENANT = "x-youtab-tenant-id"
_H_USER = "x-youtab-user-id"
_H_ROLES = "x-youtab-roles"
_H_CORRELATION = "x-youtab-correlation-id"
_H_IDEMPOTENCY = "idempotency-key"

# kanban task.status -> product run status projection. A kanban "task" on the
# runtime board IS one product run (create-run makes one task).
_STATUS_MAP = {
    "triage": "queued",
    "todo": "queued",
    "scheduled": "queued",
    "ready": "queued",
    "running": "running",
    "review": "running",
    "blocked": "blocked",
    "done": "completed",
    "archived": "cancelled",
}
# Statuses from which a run cannot progress further within the product model.
_TERMINAL_PRODUCT_STATUSES = {"completed", "cancelled"}

# Event kind marking a product-initiated cancel (so a cancelled+archived task is
# projected as ``cancelled`` and distinguished from an ordinary archive).
_CANCEL_EVENT_KIND = "runtime_cancel_requested"

# WAVE-30H Phase-A (ADR-0004): the managed interactive-lifecycle control contract
# — clarification (question/answer), approval (request/decision) and REAL
# pause/checkpoint/resume — is defined canonically in youtab_runtime.run_control.
from youtab_runtime import run_control as _rc  # noqa: E402


# ---------------------------------------------------------------------------
# Dispatcher — drive real execution
# ---------------------------------------------------------------------------
#
# Creating a task only enqueues it; a dispatcher tick claims ``ready`` tasks and
# spawns the real worker subprocess (``kanban_db._default_spawn`` ->
# ``youtab -p <profile> chat -q ...``), then later ticks reclaim/finalise. The
# dashboard process does not run an embedded dispatcher, so the runtime surface
# owns a lightweight background ticker that advances runs while any are active.
#
# ``_spawn_override`` lets a test / local-proof harness inject a deterministic
# real worker (a genuine subprocess that writes real run rows, events, and
# artifacts) in place of the LLM-backed default — the runtime is never mocked,
# only the model brain is substituted. Production leaves it None (real spawn).
_spawn_override = None
_ticker_thread: "Optional[threading.Thread]" = None
_ticker_stop = threading.Event()
_ticker_lock = threading.Lock()

# WAVE-30H task 2a — optional pre-warmed single-use worker pool (default OFF). When
# YOUTAB_AGENT_RUNTIME_WORKER_POOL is set, the real-model spawn path hands each run
# to a warm, pre-imported, single-use worker instead of cold-spawning a fresh
# youtab process, eliminating the per-run import + engine.resolve startup cost.
# Per-run tenant/grant/capability/memory/env binding is IDENTICAL (the worker uses
# kanban_db.build_worker_invocation, same as a fresh spawn) and each worker serves
# exactly one run then exits.
_worker_pool = None
_worker_pool_disabled = False  # sticky: a failed/invalid config disables the pool
_worker_pool_lock = threading.Lock()

_WORKER_POOL_MAX_SIZE = 16  # hard safety ceiling on pooled processes


def _worker_pool_enabled() -> bool:
    return (os.environ.get("YOUTAB_AGENT_RUNTIME_WORKER_POOL", "") or "").strip() not in ("", "0", "false")


def _worker_pool_size() -> Optional[int]:
    """Validated pool size, or None when the configured value is INVALID/UNSAFE.

    Fail-closed: a non-integer, non-positive, or over-ceiling size returns None so
    the caller disables the pool and uses the classic cold spawn — never a crash
    and never an unbounded process fleet."""
    raw = (os.environ.get("YOUTAB_AGENT_RUNTIME_WORKER_POOL_SIZE", "2") or "").strip()
    try:
        size = int(raw)
    except (ValueError, TypeError):
        _log.warning("worker pool: invalid size %r; disabling pool (fail-closed cold spawn)", raw)
        return None
    if size < 1 or size > _WORKER_POOL_MAX_SIZE:
        _log.warning("worker pool: size %d out of [1,%d]; disabling pool (fail-closed)", size, _WORKER_POOL_MAX_SIZE)
        return None
    return size


def _get_worker_pool():
    """Return the live pool, or None to signal a fail-closed fallback to cold spawn.

    Any invalid/unsafe config or a pool-creation failure disables the pool
    stickily so the board keeps running on the classic spawn path."""
    global _worker_pool, _worker_pool_disabled
    with _worker_pool_lock:
        if _worker_pool_disabled:
            return None
        if _worker_pool is None:
            size = _worker_pool_size()
            if size is None:
                _worker_pool_disabled = True  # invalid config -> fail closed
                return None
            try:
                from youtab_agent_cli.worker_pool import WorkerPool
                _worker_pool = WorkerPool(size=size, runner="cli", warm_full=True)
                _log.info("runtime worker pool started (size=%s)", size)
            except Exception as exc:  # noqa: BLE001 — creation failure must not stall the board
                _worker_pool_disabled = True
                _log.warning("worker pool creation failed (%s); disabling pool (fail-closed cold spawn)", exc)
                return None
        return _worker_pool


def _shutdown_worker_pool() -> None:
    global _worker_pool, _worker_pool_disabled
    with _worker_pool_lock:
        if _worker_pool is not None:
            try:
                proof = _worker_pool.close()
                _log.info("runtime worker pool shut down: %s", proof)
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                _log.warning("runtime worker pool shutdown error: %s", exc)
            _worker_pool = None
        _worker_pool_disabled = False  # allow a re-enable after a clean shutdown


def _deterministic_worker_enabled() -> bool:
    """True when the NON-PRODUCTION deterministic integration worker is opted in.

    Gated by ``YOUTAB_AGENT_RUNTIME_DETERMINISTIC_WORKER`` and refused in
    production. Lets a local/staging preview execute real runs end-to-end when
    no model credential is configured — with every event/result honestly tagged
    ``[deterministic-integration-agent]`` (see runtime_integration_worker).
    """
    if (os.environ.get("APP_ENV", "") or "").strip().lower() in {"prod", "production"}:
        return False
    return (os.environ.get("YOUTAB_AGENT_RUNTIME_DETERMINISTIC_WORKER", "") or "").strip().lower() in {
        "1", "true", "yes",
    }


def _deterministic_spawn(task, workspace, *, board=None):
    """Spawn the real deterministic integration worker subprocess.

    The worker's stdout/stderr are redirected to the engine's per-task worker
    log so ``/runs/{id}/logs`` returns real content (same convention as the
    production ``_default_spawn``).
    """
    import subprocess
    import sys
    env = dict(os.environ)
    db_path = str(kb.kanban_db_path(board=RUNTIME_BOARD))
    log_path = kb.worker_log_path(task.id, board=RUNTIME_BOARD)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_f = open(log_path, "ab", buffering=0)
    except OSError:
        log_f = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "youtab_agent_cli.runtime_integration_worker", task.id, db_path],
            env=env,
            stdout=log_f if log_f is not None else None,
            stderr=subprocess.STDOUT if log_f is not None else None,
        )
        return proc.pid
    finally:
        if log_f is not None:
            log_f.close()  # the child holds its own dup'd fd


# Event recorded at create-run naming the execution mode of a run:
#   "model"         — the real, provider-backed agent (production default);
#   "deterministic" — the NON-PRODUCTION deterministic integration worker.
_MODE_EVENT = "runtime_execution_mode"

# Event recorded at create-run naming the branded product-engine the caller
# selected (a profile_id + its public label). Consumer-safe; never carries the
# resolved provider/model/override behind that selection.
_ENGINE_EVENT = "runtime_engine_selection"

# Event recorded at create-run carrying the authoritative, server-clamped per-run
# limit set (WAVE-30B §8). Numeric limits + max_cost_eur only — never a secret.
_LIMITS_EVENT = "runtime_limits"

# Event recorded on a retry run naming the original run it was retried from and
# the preserved correlation id — makes retry lineage explicit and queryable.
_RETRIED_FROM_EVENT = "runtime_retried_from"

# Event recorded at create-run (managed trust mode only) carrying the Simorgh
# execution grant that authorised the run, so the worker can re-admit it in its
# own process (WAVE-30H R3). The grant is a signed, tenant-scoped authorization
# token — never a secret (the engine holds only the Brain PUBLIC key).
_GRANT_EVENT = "runtime_execution_grant"
# Companion event carrying the frozen per-run capability manifest (WAVE-30H
# correction 2). Persisted with the grant so the worker rebuilds the SAME
# CapabilityBinding it was admitted under — "*" cannot authorize a tool
# registered after admission.
_GRANT_MANIFEST_EVENT = "runtime_capability_manifest"

# WAVE-30H Gate-2 Phase-B: toolsets whose availability is an EXECUTION-CONTEXT
# gate the dispatched worker satisfies but the ingress process cannot. Every run
# on this plane is dispatched as a kanban worker, so the kanban task-lifecycle
# toolset (kanban_complete/block/heartbeat/show/...) is part of the run's context;
# its _check_kanban_mode gate is keyed on the worker-only YOUTAB_AGENT_KANBAN_TASK
# env. Including it in the frozen manifest is still fully gated by the grant's
# allowed_toolsets + the agent ACL (never a global authorization), and the
# invocation-time strict gate (available_strict) re-checks it in the worker.
_WORKER_EXECUTION_CONTEXT_TOOLSETS = frozenset({"kanban"})


def _resolve_task_mode(task_id: str) -> str:
    """Return the recorded execution mode for a task ("model" by default).

    The mode is a create-time event so it survives an engine restart and is
    visible in the run's own event stream.
    """
    try:
        with kb.connect_closing(board=RUNTIME_BOARD) as conn:
            for e in kb.list_events(conn, task_id):
                if e.kind == _MODE_EVENT and isinstance(e.payload, dict):
                    m = str(e.payload.get("mode") or "").strip().lower()
                    if m in ("model", "deterministic"):
                        return m
    except Exception:  # noqa: BLE001 — resolution failure defaults to real model
        pass
    # No explicit mode: the global non-prod flag makes deterministic the default
    # (for CI / credential-less environments); otherwise real model execution.
    return "deterministic" if _deterministic_worker_enabled() else "model"


def _mode_aware_spawn(task, workspace, *, board=None):
    """Single dispatcher spawn function; picks the worker per-task by its mode.

    Precedence: explicit test/proof override > per-task deterministic mode
    (non-prod only) > the real model-backed ``_default_spawn``. Deterministic is
    NEVER chosen under a production environment.
    """
    if _spawn_override is not None:
        return _spawn_override(task, workspace, board=board)
    mode = _resolve_task_mode(task.id)
    if mode == "deterministic" and _deterministic_worker_enabled():
        return _deterministic_spawn(task, workspace, board=board)
    # Real model path: a warm single-use pool worker when enabled (default OFF)
    # AND the config is valid AND the pool started; else the classic cold spawn.
    # Both return a real pid + bind per-run context identically; the pool only
    # pre-pays interpreter import. A disabled/failed pool fails closed to cold spawn.
    if _worker_pool_enabled():
        pool = _get_worker_pool()
        if pool is not None:
            return pool.spawn(task, workspace, board=board)
    return kb._default_spawn(task, workspace, board=board)


def _dispatch_tick() -> None:
    """Run one dispatcher tick on the runtime board (best-effort, never raises)."""
    try:
        with kb.connect_closing(board=RUNTIME_BOARD) as conn:
            kb.dispatch_once(conn, spawn_fn=_mode_aware_spawn, board=RUNTIME_BOARD)
    except Exception as exc:  # noqa: BLE001 — a tick failure must not crash the loop
        _log.debug("runtime dispatcher tick failed: %s", exc)


def _ticker_loop() -> None:
    while not _ticker_stop.wait(1.5):
        _dispatch_tick()


def ensure_dispatcher_running() -> None:
    """Start the background dispatcher ticker once (idempotent)."""
    global _ticker_thread
    with _ticker_lock:
        if _ticker_thread is not None and _ticker_thread.is_alive():
            return
        _ticker_stop.clear()
        _ticker_thread = threading.Thread(
            target=_ticker_loop, name="youtab-runtime-dispatcher", daemon=True
        )
        _ticker_thread.start()
        _log.info("runtime dispatcher ticker started (board=%s)", RUNTIME_BOARD)


def stop_dispatcher() -> None:
    """Stop the background ticker (test teardown / shutdown) and tear down the
    worker pool if one was started (zero-survivor)."""
    _ticker_stop.set()
    _shutdown_worker_pool()


# ---------------------------------------------------------------------------
# Auth / identity dependency
# ---------------------------------------------------------------------------


class RuntimeIdentity:
    """Verified caller context: service identity + gateway-forwarded end user."""

    __slots__ = (
        "tenant", "user", "roles", "correlation_id", "idempotency_key", "workspace",
    )

    def __init__(
        self,
        *,
        tenant: str,
        user: str,
        roles: List[str],
        correlation_id: str,
        idempotency_key: Optional[str],
        workspace: str,
    ) -> None:
        self.tenant = tenant
        self.user = user
        self.roles = roles
        self.correlation_id = correlation_id
        self.idempotency_key = idempotency_key
        # Canonical v2 workspace binding (``-`` when unscoped). Authoritative for the
        # run's effective-binding scope; identical to the value the signed command is
        # verified against and the Simorgh grant is admitted under, so the worker's
        # workspace-scope gate (binding.workspace == grant env.workspace_id) holds.
        self.workspace = workspace


def _runtime_secret() -> str:
    # File-backed (YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE) preferred; the inline
    # env var is still honoured. The value never has to enter the environment.
    from youtab_agent_cli.secret_file import env_or_file

    return (env_or_file("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET") or "").strip()


def require_service_identity(request: Request) -> RuntimeIdentity:
    """Fail-closed dependency: require the runtime service identity + end user.

    * The request must have been authenticated by the ``runtime-service``
      provider (``token_principal`` carries the ``runtime`` scope). If the
      runtime secret is unset the provider isn't registered and the token seam
      already 401s upstream; this is the belt to that suspenders.
    * The gateway-verified end-user headers (tenant + user) must be present.
      A client-supplied tenant/user is never trusted — these are honoured only
      because the service-secret gate already passed.
    """
    principal = getattr(request.state, "token_principal", None)
    authed = bool(getattr(request.state, "token_authenticated", False))
    if not _runtime_secret():
        # Surface disabled — no signing/verification key configured.
        raise HTTPException(status_code=503, detail={"error": "runtime_disabled"})
    if not authed or principal is None or "runtime" not in getattr(principal, "scopes", ()):  # noqa: E501
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})

    tenant = _header(request, _H_TENANT)
    user = _header(request, _H_USER)
    if not tenant or not user:
        # Identity unverifiable — never fall back to a client-guessable default.
        raise HTTPException(status_code=403, detail={"error": "identity_unverified"})

    roles_raw = _header(request, _H_ROLES)
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()] if roles_raw else []
    correlation_id = _header(request, _H_CORRELATION) or f"cid-{uuid.uuid4().hex}"
    idem = _header(request, _H_IDEMPOTENCY) or None
    # Canonical v2 workspace (``-`` when unscoped) — the SAME value the signed
    # command is verified against (`_verify_signed_command`) and the grant is
    # admitted under (`_admit_execution_grant`), captured once so the run binding is
    # scoped to it and the worker's workspace-scope gate can match it.
    workspace = (
        request.headers.get(rca.WORKSPACE_HEADER) or rca.WORKSPACE_UNSCOPED
    ).strip() or rca.WORKSPACE_UNSCOPED
    return RuntimeIdentity(
        tenant=tenant,
        user=user,
        roles=roles,
        correlation_id=correlation_id,
        idempotency_key=idem,
        workspace=workspace,
    )


def _header(request: Request, name: str) -> str:
    return (request.headers.get(name) or "").strip()


# ---- signed-command nonce store (lazy singleton) --------------------------

_nonce_store: "Optional[rca.NonceStore]" = None
_nonce_store_lock = threading.Lock()


def _nonce_memory_opt_in() -> bool:
    """Whether the process-local in-memory nonce store is EXPLICITLY allowed.

    Off by default. The in-memory store only protects a single process for its
    lifetime; it cannot see nonces burned by sibling workers, and it forgets
    everything on restart — both reopen replay. It is acceptable only for a
    single-process/dev/test deployment that opts in deliberately.
    """
    return (os.environ.get("YOUTAB_RUNTIME_NONCE_ALLOW_MEMORY") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _get_nonce_store() -> "rca.NonceStore":
    global _nonce_store
    with _nonce_store_lock:
        if _nonce_store is None:
            try:
                db_path = str(kb.kanban_db_path(board=RUNTIME_BOARD).parent / "runtime_command_nonces.db")  # noqa: E501
                _nonce_store = rca.SqliteNonceStore(db_path)
            except Exception as exc:  # noqa: BLE001
                # A durable, cross-process nonce store is a security precondition
                # for accepting mutating commands. Silently degrading to a
                # process-local memory store (the previous behaviour) reopened
                # cross-process and post-restart replay. Fail closed by default;
                # only fall back when memory is EXPLICITLY opted in. Do not cache
                # the failure — a later request retries construction.
                if _nonce_memory_opt_in():
                    _log.warning(
                        "runtime nonce store using EXPLICITLY opted-in in-memory "
                        "store (process-local replay protection only): %s", exc,
                    )
                    _nonce_store = rca._MemoryNonceStore()
                else:
                    _log.error(
                        "runtime nonce store unavailable; refusing signed "
                        "commands (set YOUTAB_RUNTIME_NONCE_ALLOW_MEMORY=1 only "
                        "for a single-process deployment): %s", exc,
                    )
                    raise rca.CommandAuthError(
                        "nonce_store_unavailable",
                        "runtime nonce store is unavailable",
                        503,
                    ) from exc
        return _nonce_store


_grant_boundary: "Optional[Any]" = None
_grant_boundary_lock = threading.Lock()


def _get_grant_boundary():
    """Lazy AuthorityBoundary with a DURABLE, cross-process grant-nonce store.

    The grant's single-use nonce lives in its OWN table (separate from the HMAC
    transport nonces) so the two namespaces never interfere. Fail-closed exactly
    like ``_get_nonce_store``: a durable store is required for managed admission;
    only an explicit opt-in permits the process-local in-memory claimer.
    """
    global _grant_boundary
    with _grant_boundary_lock:
        if _grant_boundary is None:
            from youtab_runtime.policy import AuthorityBoundary
            try:
                db_path = str(
                    kb.kanban_db_path(board=RUNTIME_BOARD).parent
                    / "runtime_grant_nonces.db"
                )
                store = rca.SqliteNonceStore(db_path)
            except Exception as exc:  # noqa: BLE001
                if _nonce_memory_opt_in():
                    _log.warning(
                        "grant admission using EXPLICITLY opted-in in-memory nonce "
                        "store (process-local replay protection only): %s", exc,
                    )
                    store = rca._MemoryNonceStore()
                else:
                    _log.error(
                        "grant admission nonce store unavailable; refusing managed "
                        "runs (set YOUTAB_RUNTIME_NONCE_ALLOW_MEMORY=1 only for a "
                        "single-process deployment): %s", exc,
                    )
                    raise
            _grant_boundary = AuthorityBoundary(nonce_store=store)
        return _grant_boundary


async def _admit_execution_grant(request: Request, identity: RuntimeIdentity):
    """Enforce the trust-mode execution-authority gate for a mutating managed run.

    Managed mode REQUIRES a valid Simorgh grant (fail-closed on missing/invalid/
    expired/replayed/mismatched); local-standalone mode REFUSES a managed grant.
    Returns ``(grant_header, manifest)`` where ``grant_header`` is the raw grant
    header string when one was admitted (managed) else ``None``, and ``manifest``
    is the frozen per-run capability manifest (correction 2) to persist alongside
    the grant, else ``None``. The sealed :class:`AdmittedCommand` — including this
    capability binding — is re-created in the worker via
    ``managed_execution.re_admit_worker_grant`` from the persisted grant+manifest.
    """
    from youtab_runtime import managed_execution as mx
    from youtab_runtime import stage_trace as _st

    grant_header = request.headers.get(mx.GRANT_HEADER)
    try:
        mode = mx.current_trust_mode()
        if mode is mx.TrustMode.MANAGED:
            workspace = (
                request.headers.get(rca.WORKSPACE_HEADER) or rca.WORKSPACE_UNSCOPED
            ).strip() or rca.WORKSPACE_UNSCOPED
            # Freeze the per-run capability manifest from the LIVE registry now, so
            # "*" authorizes exactly what is registered/authorized/operational at
            # admission — never a tool registered later. Built before admit() so it
            # is sealed into (and proof-bound to) the AdmittedCommand.
            capability_binding = None
            with _st.trace_context_scope(
                correlation_id=getattr(identity, "correlation_id", None),
                tenant=identity.tenant, user=identity.user,
            ):
                if grant_header:
                    from tools.registry import registry as _registry

                    from youtab_agent_cli import capability_manifest as _cm

                    envelope = mx.decode_grant_header(grant_header)
                    # R8: freezing the per-run manifest = the tools discovery +
                    # schema-hash work at ingress; measure it as its own stage.
                    with _st.span(_st.Stage.TOOLS_DISCOVER) as _sp:
                        capability_binding = _cm.build_ingress_binding(
                            envelope,
                            registry=_registry,
                            # WAVE-30H Gate-2 Phase-B fix: every run on this runtime
                            # plane is dispatched as a kanban worker (create_task →
                            # dispatch_once → a ``youtab … chat -q "work kanban task
                            # <id>"`` worker), so the kanban task-lifecycle toolset
                            # (kanban_complete/block/heartbeat) is part of the run's
                            # execution context. Its availability gate
                            # (_check_kanban_mode) is keyed on the worker-only
                            # YOUTAB_AGENT_KANBAN_TASK env, which is absent in THIS
                            # ingress process — so without this the completion tool
                            # is wrongly dropped from the frozen manifest and the
                            # worker (which re-admits it) can never be authorized to
                            # complete its own task. Deferring that execution-context
                            # gate to the invocation-time strict gate (C9) is NOT an
                            # availability bypass and stays fully grant/ACL gated
                            # (a grant that does not authorize "kanban"/"*" still
                            # excludes these tools).
                            context_available_toolsets=_WORKER_EXECUTION_CONTEXT_TOOLSETS,
                        )
                        try:
                            _sp["tool_count"] = len(
                                getattr(capability_binding, "tool_hashes", None) or {}
                            )
                        except Exception:
                            pass
                with _st.span(_st.Stage.GRANT_VERIFY):
                    mx.admit_managed_run(
                        grant_header=grant_header,
                        identity=mx.AdmissionIdentity(
                            tenant=identity.tenant, user=identity.user,
                            workspace=workspace,
                        ),
                        boundary=_get_grant_boundary(),
                        public_keys=mx.load_brain_public_keys(),
                        capability_binding=capability_binding,
                    )
            manifest = (
                _cm.binding_to_persisted(capability_binding)
                if capability_binding is not None
                else None
            )
            return grant_header, manifest
        # local-standalone: must not accept a managed grant implicitly.
        mx.reject_grant_in_standalone(grant_header)
        return None, None
    except mx.ManagedAdmissionError as exc:
        raise HTTPException(
            status_code=exc.http_status, detail={"error": exc.code}
        ) from exc


async def _verify_signed_command(request: Request, identity: RuntimeIdentity) -> None:
    """Verify the signed-command envelope on a mutating request. Raise on failure."""
    body = await request.body()
    # Canonical v2 binds the workspace (field 5). The gateway sends it on
    # ``X-Youtab-Workspace-Id`` (``-`` when unscoped) and signs the SAME value;
    # the engine MUST verify against that value, not the default, or a
    # workspace-scoped request would fail (or, worse, be verified under the
    # wrong workspace). For v1 requests the param is ignored by the v1 builder.
    workspace = (request.headers.get(rca.WORKSPACE_HEADER) or rca.WORKSPACE_UNSCOPED).strip()
    from youtab_runtime import stage_trace as _st

    try:
        with _st.trace_context_scope(
            correlation_id=getattr(identity, "correlation_id", None),
            tenant=identity.tenant, user=identity.user,
        ), _st.span(_st.Stage.ADMISSION_VERIFY):
            rca.verify_command(
                method=request.method,
                path=request.url.path,
                tenant=identity.tenant,
                user=identity.user,
                body=body,
                headers=request.headers,
                secret=_runtime_secret(),
                store=_get_nonce_store(),
                workspace=workspace or rca.WORKSPACE_UNSCOPED,
            )
    except rca.CommandAuthError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"error": exc.code}) from exc  # noqa: E501


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def _agent_projection(p: Any) -> Dict[str, Any]:
    """Project a ProfileInfo (an agent) to the connector's AgentProjection."""
    return {
        "id": getattr(p, "name", None),
        "display_name": getattr(p, "name", None),
        "description": getattr(p, "description", "") or "",
        "status": "available",
        "version": None,
        "model": getattr(p, "model", None),
        "provider": getattr(p, "provider", None),
        "skill_count": getattr(p, "skill_count", 0),
        "is_default": bool(getattr(p, "is_default", False)),
        "scope": "tenant",
        "runtime_available": True,
    }


def _run_status(task: "kb.Task", *, cancelled: bool,
                interactive: "Optional[str]" = None) -> str:
    if cancelled and task.status in ("archived", "blocked", "done"):
        return "cancelled"
    base = _STATUS_MAP.get(task.status, task.status)
    # A non-terminal run that is waiting on the user (question/approval) or has
    # been paused projects that interactive status over the coarse kanban status
    # (ADR-0004). Terminal runs and cancels always win and never show these.
    if (
        interactive in _rc.INTERACTIVE_STATUSES
        and base not in _TERMINAL_PRODUCT_STATUSES
    ):
        return interactive
    return base


def _interactive_status(events: "List[kb.Event]") -> "Optional[str]":
    """Derive the interactive lifecycle status from the ordered event log."""
    return _rc.interactive_status(events)


def _mode_from_events(events: "List[kb.Event]") -> str:
    """Read the recorded execution mode from a run's events ("model" default)."""
    for e in events:
        if e.kind == _MODE_EVENT and isinstance(e.payload, dict):
            m = str(e.payload.get("mode") or "").strip().lower()
            if m in ("model", "deterministic"):
                return m
    return "model"


def _engine_selection_from_events(events: "List[kb.Event]") -> Optional[Dict[str, Any]]:
    """The branded engine selection recorded at create, or ``None``.

    Consumer-safe projection: ``{profile_id, public_label}`` only. The resolved
    provider/model/override that selection drove is deliberately NOT recorded on
    the event and never surfaces here.
    """
    for e in events:
        if e.kind == _ENGINE_EVENT and isinstance(e.payload, dict):
            pid = e.payload.get("profile_id")
            if pid:
                return {"profile_id": pid, "public_label": e.payload.get("public_label")}
    return None


def _limits_from_events(events: "List[kb.Event]") -> Optional[Dict[str, Any]]:
    """The authoritative clamped per-run limits (cost policy) recorded at create.

    Numeric-only ``{...max_cost_eur, worker_attempt_limit}`` projection; never a
    secret. ``None`` when the original run recorded no limits event.
    """
    for e in events:
        if e.kind == _LIMITS_EVENT and isinstance(e.payload, dict):
            return dict(e.payload)
    return None


def _retry_execution_binding(
    task: "kb.Task", events: "List[kb.Event]"
) -> Dict[str, Any]:
    """Assemble the authoritative execution binding a retry child MUST inherit.

    A retry re-executes the ORIGINAL run; it must run on the SAME substrate and
    must never silently downgrade to the worker-default provider/model. The
    binding has two durable layers, both reproduced on the child:

    * the row-level resolved ``provider_override`` / ``model_override`` — the pin
      the dispatcher hands the worker (the load-bearing guarantee against a
      default-model run);
    * the create-time provenance events — the branded engine/profile selection
      (``runtime_engine_selection``) and the clamped cost-policy limits
      (``runtime_limits``).

    The per-run Simorgh grant is deliberately NOT part of this binding: a fresh
    grant is re-minted per run by :func:`_admit_execution_grant` (a per-run
    authority is never copied/replayed).

    Fail closed (HTTP 422) when the recorded binding is invalid or internally
    conflicting, so a broken original is refused rather than retried onto a
    default model.
    """
    model_override = (task.model_override or "").strip() or None
    provider_override = (task.provider_override or "").strip() or None
    engine_selection = _engine_selection_from_events(events)
    limits = _limits_from_events(events)

    # invalid/conflicting: a provider pin with no model pin cannot resolve a
    # concrete model — it would fall through to a default. Refuse.
    if provider_override and not model_override:
        raise HTTPException(
            status_code=422,
            detail={"error": "retry_binding_invalid",
                    "reason": "provider_override without model_override"},
        )
    # invalid: a recorded engine-selection EVENT whose profile_id is empty is a
    # corrupt binding. Checked on the RAW event (``_engine_selection_from_events``
    # silently skips empty ids) so the retry is refused rather than quietly
    # dropping the pin and re-running unbound.
    for e in events:
        if e.kind == _ENGINE_EVENT and isinstance(e.payload, dict):
            if not str(e.payload.get("profile_id") or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail={"error": "retry_binding_invalid",
                            "reason": "engine selection event has empty profile_id"},
                )
            break
    # WAVE-30H: inherit the parent's immutable effective binding UNCHANGED and pin
    # the child row from it, so a config change AFTER the original run can never
    # silently re-resolve a different provider/model on retry. Fail closed when an
    # attested run lacks a binding, when a binding is corrupt, or when it carries no
    # resolved model to pin.
    binding = eb.effective_binding_from_events(events)
    attested = any(
        e.kind in (_ENGINE_EVENT, _GRANT_EVENT)
        for e in events
        if isinstance(getattr(e, "payload", None), dict)
    )
    if binding is not None and binding.get("__corrupt__"):
        raise HTTPException(
            status_code=422,
            detail={"error": "retry_binding_invalid",
                    "reason": "effective binding has no binding_version"},
        )
    if binding is None and attested:
        raise HTTPException(
            status_code=422,
            detail={"error": "retry_binding_missing",
                    "reason": "attested run has no persisted effective binding"},
        )
    # WAVE-30H #4: the parent's ORIGINAL stored hash MUST self-verify BEFORE we pin
    # the child row from it or rescope it. A tampered-at-rest parent (fields changed,
    # stale hash) would otherwise be (a) used to pin the child's provider/model row to
    # the tampered substrate and (b) laundered by rescope_binding into a fresh VALID
    # child hash over the tampered fields — bypassing the worker's tamper gate. Verify
    # here, while the binding still carries the PARENT's run scope.
    if binding is not None and not eb.verify_binding(binding):
        raise HTTPException(
            status_code=422,
            detail={"error": "retry_binding_invalid",
                    "reason": "effective binding hash mismatch (tampered/malformed)"},
        )
    if binding is not None and not model_override:
        # Pin the child row from the binding ONLY when it carries a RESOLVED
        # concrete model — that is the drift that must be frozen (a cloud/branded
        # identity re-resolving from changed config). An unresolved/config-default
        # binding has no concrete identity to pin, so the child re-resolves exactly
        # as the original did (legacy-safe); the binding is still re-recorded.
        if binding.get("model_identifier_status") == "resolved" and binding.get("model"):
            model_override = binding.get("model_ref") or binding.get("model")
            provider_override = binding.get("provider")
    return {
        "model_override": model_override,
        "provider_override": provider_override,
        "engine_selection": engine_selection,
        "limits": limits,
        # Re-recorded UNCHANGED onto the child (same binding_version) so the
        # lineage is immutable; None for a legacy run with no binding.
        "effective_binding": binding,
    }


_RETRY_BUDGET_DIMENSIONS = (
    "max_iterations",
    "max_spawn_depth",
    "max_concurrent_agents",
    "max_total_tokens",
    "max_cost_micros",
    "max_retries",
)


def _assert_retry_grant_budget_not_widened(orig_events, retry_grant_header) -> None:
    """SEC-9 #6: a retry must never widen an execution-tree budget dimension or
    extend the deadline relative to the ORIGINAL run's grant.

    The execution-tree budget is anchored (worker side) to the inherited binding
    root, so a retry shares the ORIGINAL tree and ``open_tree``'s no-reseed keeps
    the ceilings — but reject an over-provisioned retry grant HERE too, before the
    child run is created, so the drift is refused early and explicitly rather than
    only being clamped later. No-op in standalone, or when either grant is absent
    (a legacy original with no persisted grant), so legitimate retries with equal
    or tighter reasoning pass unchanged.
    """
    from youtab_runtime import managed_execution as mx

    if mx.current_trust_mode() is not mx.TrustMode.MANAGED or not retry_grant_header:
        return
    orig_header = None
    for e in orig_events:
        if getattr(e, "kind", None) == _GRANT_EVENT and isinstance(
            getattr(e, "payload", None), dict
        ):
            orig_header = e.payload.get("grant")
            break
    if not orig_header:
        return  # legacy original run with no persisted grant to compare against
    try:
        orig_env = mx.decode_grant_header(orig_header)
        retry_env = mx.decode_grant_header(retry_grant_header)
    except Exception:  # noqa: BLE001 — a malformed grant is already fail-closed at admission
        return
    orig_reasoning = getattr(orig_env, "reasoning", None)
    retry_reasoning = getattr(retry_env, "reasoning", None)
    if orig_reasoning is None or retry_reasoning is None:
        return
    for dim in _RETRY_BUDGET_DIMENSIONS:
        ov = getattr(orig_reasoning, dim, None)
        rv = getattr(retry_reasoning, dim, None)
        if ov is not None and rv is not None and rv > ov:
            raise HTTPException(
                status_code=422,
                detail={"error": "retry_budget_widened", "dimension": dim},
            )
    # Deadline: compare the WINDOW DURATION (deadline_at - issued_at), never the
    # absolute wall-clock deadline — a legitimate retry is minted later and so has
    # a later absolute deadline_at for the SAME window. Widening the window is the
    # drift to refuse.
    od = getattr(orig_reasoning, "deadline_at", None)
    rd = getattr(retry_reasoning, "deadline_at", None)
    oi = getattr(orig_env, "issued_at", None)
    ri = getattr(retry_env, "issued_at", None)
    if od is not None and rd is not None and oi is not None and ri is not None:
        if (rd - ri) > (od - oi):
            raise HTTPException(
                status_code=422,
                detail={"error": "retry_budget_widened", "dimension": "deadline_at"},
            )


def _run_summary(
    task: "kb.Task", *, cancelled: bool = False, execution_mode: str = "model",
    interactive: "Optional[str]" = None,
) -> Dict[str, Any]:
    return {
        "run_id": task.id,
        "agent_id": task.assignee,
        "agent_name": task.assignee,
        "status": _run_status(task, cancelled=cancelled, interactive=interactive),
        "execution_mode": execution_mode,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.completed_at,
        "tenant_id": task.tenant,
        # The caller's own per-request correlation id (authoritative column).
        # Not a provider/model secret; safe to surface to the consumer.
        "correlation_id": task.correlation_id,
    }


def _journal_usage_rollup(task: "kb.Task") -> Optional[Dict[str, Any]]:
    """Aggregate usage/model_call journal events for this run (unknown-safe).

    WAVE-26: authoritative per-run usage summed from the durable run journal,
    preserving ``unknown`` as first-class rather than the best-effort
    3-fields-or-null the closing run's metadata provides. Returns None when the
    journal has no usage events for this run (deterministic/offline runs).
    """
    try:
        from youtab_runtime.run_journal import Principal, list_events
    except Exception:
        return None
    if not (task.tenant and task.created_by):
        return None
    try:
        pr = Principal(task.tenant, task.created_by)
    except Exception:
        return None
    fields = ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_write_tokens", "reasoning_tokens", "total_tokens")
    totals = {f: 0 for f in fields}
    seen_known = False
    any_unknown = False
    cost = 0.0
    cost_known = False
    seq = 0
    while True:
        batch = list_events(task.id, pr, after_seq=seq, category="usage", limit=500)
        if not batch:
            break
        for ev in batch:
            seq = ev.seq
            p = ev.payload
            if p.get("usage_status") == "known":
                seen_known = True
                for f in fields:
                    v = p.get(f)
                    if v is not None:
                        totals[f] += int(v)
                c = (p.get("cost") or {}).get("amount_usd")
                if c is not None:
                    cost += float(c)
                    cost_known = True
            else:
                any_unknown = True
        if len(batch) < 500:
            break
    if not seen_known and not any_unknown:
        return None
    out: Dict[str, Any] = (
        {f: totals[f] for f in fields} if seen_known
        else {f: None for f in fields}
    )
    out["usage_status"] = (
        "known" if seen_known and not any_unknown
        else ("partial" if seen_known else "unknown")
    )
    out["cost"] = {
        "amount_usd": cost if cost_known else None,
        "status": "estimated" if cost_known else "unknown",
    }
    return out


def _run_detail(conn: "Any", task: "kb.Task", *, cancelled: bool) -> Dict[str, Any]:
    runs = kb.list_runs(conn, task.id)
    attachments = kb.list_attachments(conn, task.id)
    events = kb.list_events(conn, task.id)
    mode = _mode_from_events(events)
    # Best-effort provider/model/usage from the closing run's metadata (the real
    # worker records these; the deterministic worker does not).
    meta = {}
    for r in reversed(runs):
        if isinstance(r.metadata, dict) and r.metadata:
            meta = r.metadata
            break
    # The result is the task's recorded result; a real agent that completes via
    # the kanban_complete tool often carries its answer in the closing run's
    # summary instead, so fall back to that so the UI always shows the outcome.
    result = task.result
    if not result:
        for r in reversed(runs):
            if r.summary:
                result = r.summary
                break
    summary = _run_summary(
        task, cancelled=cancelled, execution_mode=mode,
        interactive=_interactive_status(events),
    )
    summary.update({
        "task": task.body,
        "title": task.title,
        "result": result,
        "error": task.last_failure_error,
        "retries": task.consecutive_failures,
        "current_run_id": task.current_run_id,
        "model": meta.get("model") or meta.get("model_id"),
        "provider": meta.get("provider"),
        "usage": _journal_usage_rollup(task) or meta.get("usage") or meta.get("tokens"),
        # The branded engine the caller selected at create, if any (consumer-safe
        # {profile_id, public_label} only; never the provider/model it resolved to).
        "engine_selection": _engine_selection_from_events(events),
        # WAVE-30H: the canonical immutable per-run effective binding (the BOUND
        # identity). ``model``/``provider`` above are the DISPATCHED identity (worker
        # metadata); the harness reconciles bound vs dispatched and fails closed on
        # any drift. Non-secret classification only; None for a legacy run.
        "runtime_effective_binding": eb.effective_binding_from_events(events),
        "runs": [
            {
                "id": r.id,
                "status": r.status,
                "outcome": r.outcome,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "summary": r.summary,
                "error": r.error,
            }
            for r in runs
        ],
        "artifacts": [_artifact_ref(task.id, a) for a in attachments],
    })
    return summary


def _artifact_ref(run_id: str, a: "kb.Attachment") -> Dict[str, Any]:
    return {
        "id": a.id,
        "run_id": run_id,
        "name": a.filename,
        "kind": a.content_type or "application/octet-stream",
        "size": a.size,
        "created_at": a.created_at,
        # The gateway issues a scoped download URL; the engine never hands the
        # browser a direct engine URL.
        "download_ref": f"/api/runtime/v1/runs/{run_id}/artifacts/{a.id}",
    }


# Interactive control events carry user/agent FREE TEXT (a clarification answer,
# a question prompt, an approval action) that reaches the user-visible event
# stream. These are redacted at the projection boundary (A9) so a secret typed
# into an answer never leaks — while the raw value stays in the durable log for
# the worker to consume. NOT applied to grant/manifest events (their high-entropy
# base64 must survive verbatim for worker re-admission).
_CONTROL_REDACT_KINDS = frozenset({_rc.QUESTION, _rc.ANSWER, _rc.APPROVAL_REQUEST})


def _redact_control_payload(kind: str, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    # A run_checkpoint's ``state.messages_json`` is the FULL serialized
    # conversation — tool-call arguments, tool results, and the user's answer
    # text (which A9 scrubs at the run_answer projection). The worker restores
    # from the DURABLE log, never from this consumer projection, so withhold the
    # conversation blob here: otherwise a secret typed into an answer would leak
    # back through GET /runs/{id}/events despite the A9 scrub. Non-sensitive state
    # keys (schema version, iteration) survive for observability.
    if kind == _rc.CHECKPOINT and isinstance(payload.get("state"), dict):
        state = dict(payload["state"])
        blob = state.get("messages_json")
        if isinstance(blob, str):
            state["messages_json"] = (
                f"<redacted: conversation state withheld from event stream "
                f"({len(blob.encode('utf-8', 'surrogatepass'))} bytes)>"
            )
        return {**payload, "state": state}
    if kind not in _CONTROL_REDACT_KINDS:
        return payload
    from youtab_runtime import redaction as _R

    scrubbed = {
        k: (_R.scrub_text(v) if isinstance(v, str) else v) for k, v in payload.items()
    }
    return _R.redact_mapping(scrubbed)


def _event_projection(run_id: str, e: "kb.Event") -> Dict[str, Any]:
    # Surface the correlation id carried on authoritative dispatch/lineage
    # events (contract C5), so a consumer can trace events by correlation.
    correlation_id = None
    if isinstance(e.payload, dict):
        correlation_id = e.payload.get("correlation_id")
    return {
        "id": e.id,          # monotonic cursor
        "run_id": run_id,
        "kind": e.kind,
        "payload": _redact_control_payload(e.kind, e.payload),
        "created_at": e.created_at,
        "correlation_id": correlation_id,
    }


def _is_cancelled(events: "List[kb.Event]") -> bool:
    return any(e.kind == _CANCEL_EVENT_KIND for e in events)


def _summary_signals(conn: "Any", task_ids: "List[str]") -> "Dict[str, Tuple[bool, str]]":
    """Batch-read the cancel + execution-mode signals for many runs in ONE query.

    Returns ``{task_id: (cancelled, execution_mode)}``. This replaces the
    per-task ``list_events`` fan-out in the run-list projection — that N+1 loaded
    the FULL event history of every run only to detect a cancel event and the
    mode. Here a single query selects just the two relevant event kinds for the
    whole page and reproduces the exact semantics of ``_is_cancelled`` (any
    cancel event) and ``_mode_from_events`` (first valid mode event in
    created-order, default ``"model"``). Rows are ordered created-ascending, so
    the FIRST mode row per task wins, matching ``_mode_from_events``.
    """
    if not task_ids:
        return {}
    cancelled: set[str] = set()
    mode_seen: set[str] = set()
    mode: Dict[str, str] = {}
    # Chunk the IN(...) set to stay under SQLite's bound-variable limit (default
    # 999; the two kind params leave headroom). Each task_id lands in exactly one
    # chunk, so per-task created-order — and thus first-valid-mode-wins — is
    # preserved without any cross-chunk merge concern.
    chunk_size = 900
    for start in range(0, len(task_ids), chunk_size):
        chunk = task_ids[start:start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT task_id, kind, payload FROM task_events "
            "WHERE kind IN (?, ?) "
            f"AND task_id IN ({placeholders}) "
            "ORDER BY created_at ASC, id ASC",
            (_CANCEL_EVENT_KIND, _MODE_EVENT, *chunk),
        ).fetchall()
        for r in rows:
            tid = r["task_id"]
            if r["kind"] == _CANCEL_EVENT_KIND:
                cancelled.add(tid)
            elif r["kind"] == _MODE_EVENT and tid not in mode_seen:
                try:
                    payload = json.loads(r["payload"]) if r["payload"] else None
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    m = str(payload.get("mode") or "").strip().lower()
                    if m in ("model", "deterministic"):
                        mode[tid] = m
                        mode_seen.add(tid)
    return {
        tid: (tid in cancelled, mode.get(tid, "model")) for tid in task_ids
    }


def _load_owned_task(conn: "Any", run_id: str, identity: RuntimeIdentity) -> "kb.Task":
    """Load a task and enforce (tenant, user) ownership. 404 on any mismatch."""
    task = kb.get_task(conn, run_id)
    if (
        task is None
        or task.tenant != identity.tenant
        or task.created_by != identity.user
    ):
        # Never leak existence across tenant/user boundaries.
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})
    return task


# ---------------------------------------------------------------------------
# Branded product-engine catalogue — HONEST availability
# ---------------------------------------------------------------------------
#
# ``/engines`` lists the branded roster (Alpha, Amour, Eco, Homa, Pirouz) with a
# REAL availability flag, never a hardcoded one:
#   * a LOCAL engine (ollama/vllm/llamacpp/lmstudio) is ``online`` only when a
#     bounded loopback reachability probe of its model-list endpoint answers;
#   * an EXTERNAL engine (deepseek/moonshot/zai/…) is ``online`` only when a
#     usable credential is installed for its provider (presence boolean ONLY —
#     the secret value is never read into the response).
# The provider/model/endpoint/credential behind an engine never appear in the
# projection — only the branded fields. Probes are cached per short window so
# listing is cheap.

# Provider aliases that resolve to a LOCAL model server (see auth.resolve_provider
# and runtime_provider's local-server handling). Availability for these is a
# reachability probe, not a credential check.
_LOCAL_ENGINE_PROVIDERS = {
    "ollama", "vllm", "llamacpp", "llama.cpp", "llama-cpp", "lmstudio",
}

# Availability cache: profile_id -> (monotonic_ts, "online"|"unavailable").
_ENGINE_AVAIL_TTL = 20.0  # seconds — one bounded probe per window, not per call
_engine_avail_cache: "Dict[str, tuple[float, str]]" = {}
_engine_avail_lock = threading.Lock()

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_loopback_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        return host in _LOOPBACK_HOSTS
    except Exception:  # noqa: BLE001
        return False


def _http_reachable(url: str, *, timeout: float = 1.0) -> bool:
    """Bounded GET; True if the server answers at all (even 4xx), else False.

    Any connection error / timeout / bad URL reads as not reachable. An HTTP
    error response (e.g. 404 from a wrong path) still proves the server is up,
    so it counts as reachable. Never raises.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — loopback only
            resp.read(1)
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 — unreachable/timeout/bad-url => not online
        return False


def _local_engine_probe_urls(provider: str) -> List[str]:
    """Loopback model-list URLs to probe for a local runtime (never remote).

    A local engine that is actually pointed at a non-loopback host is out of
    scope for this reachability probe — the listing endpoint must not fan out
    network calls to arbitrary addresses — so such URLs are dropped and the
    engine reads as unavailable.
    """
    p = (provider or "").strip().lower()
    urls: List[str] = []
    if p == "ollama":
        root = (os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_HOST") or "").strip().rstrip("/")
        if root and not root.startswith("http"):
            root = "http://" + root
        root = root or "http://127.0.0.1:11434"
        urls = [root + "/api/tags", root + "/v1/models"]
    elif p == "lmstudio":
        try:
            from youtab_agent_cli import auth as _auth
            base = _auth._normalize_lmstudio_runtime_base_url("").rstrip("/")
        except Exception:  # noqa: BLE001
            base = "http://127.0.0.1:1234/v1"
        urls = [base + "/models"]
    else:  # vllm / llamacpp / llama.cpp / llama-cpp
        root = (os.environ.get("VLLM_BASE_URL") or "").strip().rstrip("/") or "http://127.0.0.1:8000/v1"
        urls = [root + "/models"] if root.endswith("/v1") else [root + "/v1/models"]
    return [u for u in urls if _is_loopback_url(u)]


def _local_engine_reachable(provider: str, model: str) -> bool:
    """True iff a local model server for this engine answers a bounded probe."""
    for url in _local_engine_probe_urls(provider):
        if _http_reachable(url, timeout=1.0):
            return True
    return False


def _endpoint_probe_urls(provider: str, endpoint: str) -> List[str]:
    """Model-list URLs to probe at a canonically-resolved connection endpoint.

    Unlike :func:`_local_engine_probe_urls`, this trusts the endpoint because it
    came from :func:`engine_connection.resolve_connection` (a protected
    server-side setting), so a remote-but-authorised model server — e.g. an
    on-prem Ollama reached over a private tunnel — is probeable instead of being
    dropped as non-loopback. It is still a single, bounded GET to one known
    host, not a fan-out to arbitrary addresses.
    """
    root = (endpoint or "").strip().rstrip("/")
    if not root:
        return []
    p = (provider or "").strip().lower()
    if p == "ollama":
        return [root + "/api/tags", root + "/v1/models"]
    if p == "lmstudio":
        return [root + "/models"]
    return [root + "/models"] if root.endswith("/v1") else [root + "/v1/models"]


def _connection_reachable(conn: "engine_connection.ResolvedConnection") -> bool:
    """True iff the canonical connection's endpoint answers a bounded probe."""
    for url in _endpoint_probe_urls(conn.provider, conn.endpoint):
        if _http_reachable(url, timeout=1.0):
            return True
    return False


def _configured_inference_base_url() -> str:
    """The ``model.base_url`` a run would dial, from server config (best effort).

    Used only to detect a split-brain (a base_url that names a different server
    than the availability endpoint). Any read failure returns "" — which the
    consistency check treats as "not independently configured", so a missing
    config never false-fails a run.
    """
    try:
        from youtab_agent_cli.config import load_config_readonly

        model_cfg = load_config_readonly().get("model")
        if isinstance(model_cfg, dict):
            return str(model_cfg.get("base_url") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _profile_default_identity(agent: str) -> "tuple[Optional[str], Optional[str], Optional[str]]":
    """Pure (no-network) resolution of the (provider, model, base_url) the dispatched
    worker for ``agent`` would actually use.

    Mirrors the worker's own precedence (``model.default`` then ``model.model``) and
    reads the AGENT PROFILE's config home (the home the dispatcher injects), not the
    server's — closing the two create-vs-dispatch divergences that left a managed
    run's binding unresolved while the worker ran a real model. Falls back to the
    server config if profile-home scoping is unavailable. Never probes the network
    (the worker's local auto-detect branch is deliberately NOT mirrored here).
    """
    m: Any = {}
    try:
        from youtab_agent_cli import config as _cfg
        from youtab_agent_cli import profiles as _profiles

        home = _profiles.resolve_profile_env(agent)
        token = _cfg.set_youtab_home_override(home)
        try:
            m = _cfg.load_config_readonly().get("model") or {}
        finally:
            _cfg.reset_youtab_home_override(token)
    except Exception:  # noqa: BLE001 — fall back to server-scoped config
        try:
            from youtab_agent_cli.config import load_config_readonly as _lcr

            m = _lcr().get("model") or {}
        except Exception:  # noqa: BLE001
            m = {}
    if not isinstance(m, dict):
        m = {}
    model = (str(m.get("default") or m.get("model") or "").strip()) or None
    provider = (str(m.get("provider") or "").strip()) or None
    base_url = (str(m.get("base_url") or "").strip()) or None
    return provider, model, base_url


def _external_credential_present(provider: str) -> bool:
    """True iff a usable credential is installed for an external provider.

    Presence boolean ONLY — the secret's value is never read into any response.
    Checks the provider's configured API-key env vars and its credential pool.
    Never raises; an unknown/unconfigured provider reads as absent.
    """
    try:
        from youtab_agent_cli import auth as _auth
        try:
            pid = _auth.resolve_provider(provider)
        except Exception:  # noqa: BLE001 — unknown provider => treat as its own id
            pid = (provider or "").strip().lower()
        pconfig = _auth.PROVIDER_REGISTRY.get(pid) or _auth.PROVIDER_REGISTRY.get(
            (provider or "").strip().lower()
        )
        if pconfig is not None:
            try:
                from agent.secret_scope import get_secret as _get_secret
            except Exception:  # noqa: BLE001
                _get_secret = lambda name, default="": os.environ.get(name, default)  # noqa: E731
            for var in getattr(pconfig, "api_key_env_vars", ()) or ():
                try:
                    if _auth.has_usable_secret(_get_secret(var, "")):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        try:
            from agent.credential_pool import load_pool
            pool = load_pool(provider)
            if pool is not None and pool.has_credentials():
                return True
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — any failure reads as no usable credential
        return False
    return False


def _engine_availability(profile_id: str) -> str:
    """Compute HONEST availability for one branded engine ("online"/"unavailable").

    Availability and execution now share ONE endpoint source
    (:func:`engine_connection.resolve_connection`), so this probe targets the
    exact endpoint a run would dial — no longer the loopback-only default that
    made a remote-but-authorised engine read as permanently unavailable.
    """
    conn = engine_connection.resolve_connection(profile_id)
    if conn is None:
        return "unavailable"
    try:
        if conn.is_local_server():
            return "online" if _connection_reachable(conn) else "unavailable"
        return "online" if _external_credential_present(conn.provider) else "unavailable"
    except Exception:  # noqa: BLE001 — never let a probe fail the listing
        return "unavailable"


def _engine_availability_cached(profile_id: str) -> str:
    """Availability with a short TTL so listing is a bounded probe per window."""
    now = time.monotonic()
    with _engine_avail_lock:
        hit = _engine_avail_cache.get(profile_id)
        if hit is not None and (now - hit[0]) < _ENGINE_AVAIL_TTL:
            return hit[1]
    # Probe outside the lock (may do a bounded network call); a brief race just
    # recomputes and is harmless.
    val = _engine_availability(profile_id)
    with _engine_avail_lock:
        _engine_avail_cache[profile_id] = (now, val)
    return val


def _engine_modalities(role: str) -> List[str]:
    """Product-safe modality words derived from the engine's role."""
    return ["text", "image"] if (role or "").strip().lower() == "vision" else ["text"]


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@router.get("/api/runtime/v1/health")
async def runtime_health(identity: RuntimeIdentity = Depends(require_service_identity)):
    from youtab_agent_cli import __version__ as engine_version
    return {
        "ok": True,
        "engine_version": engine_version,
        "contract_version": CONTRACT_VERSION,
        "auth_required": True,
    }


def _redaction_enabled() -> bool:
    """Whether secret redaction is ON (default True; only an explicit
    security.redact_secrets=false disables it)."""
    try:
        from youtab_agent_cli.config import load_config_readonly

        sec = load_config_readonly().get("security")
        if isinstance(sec, dict) and sec.get("redact_secrets") is False:
            return False
    except Exception:  # noqa: BLE001
        return True
    return True


def _configured_model_provider_names() -> Dict[str, Optional[str]]:
    """Safe NAMES only (never a credential) of the configured default model/provider."""
    out: Dict[str, Optional[str]] = {"model": None, "provider": None}
    try:
        from youtab_agent_cli.config import load_config_readonly

        model_cfg = load_config_readonly().get("model")
        if isinstance(model_cfg, dict):
            out["model"] = (str(model_cfg.get("model")) if model_cfg.get("model") else None)
            out["provider"] = (
                str(model_cfg.get("provider")) if model_cfg.get("provider") else None
            )
    except Exception:  # noqa: BLE001
        pass
    return out


def _provider_credential_source(provider: Optional[str]) -> str:
    """"file" | "env" | "absent" — how the configured provider's key is delivered.
    Presence/kind only; the value is never read into the response."""
    if not provider:
        return "absent"
    try:
        from youtab_agent_cli import auth as _auth

        try:
            pid = _auth.resolve_provider(provider)
        except Exception:  # noqa: BLE001
            pid = (provider or "").strip().lower()
        pconfig = _auth.PROVIDER_REGISTRY.get(pid) or _auth.PROVIDER_REGISTRY.get(
            (provider or "").strip().lower()
        )
        for var in getattr(pconfig, "api_key_env_vars", ()) or ():
            if os.getenv(f"{var}_FILE", "").strip():
                return "file"
        if _external_credential_present(provider):
            return "env"
    except Exception:  # noqa: BLE001
        return "absent"
    return "absent"


_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
# 6to4 (2002::/16) and Teredo (2001::/32) IPv6 literals embed a PUBLIC IPv4
# destination yet report ``is_private`` in the stdlib — treat them as public so
# they can never be classified as a verified-local target.
_V6_PUBLIC_TUNNELS = (
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2001::/32"),
)


def _endpoint_class(url: Optional[str]) -> str:
    """Classify an endpoint host for attestation (no raw endpoint is exposed).

    Returns one of loopback|private|link_local|cgnat|public|hostname|unavailable|
    invalid. A bare hostname is reported as ``hostname`` (never trusted as local).
    """
    raw = (url or "").strip()
    if not raw:
        return "unavailable"
    # Case-insensitive scheme detection (mirror of the canonical ``classify_endpoint``
    # and ``normalize_endpoint``): only prepend when there is no scheme at all, so an
    # uppercase ``HTTPS://…`` is not misclassified by a lower-case-only prefix test.
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001
        return "invalid"
    if not host:
        return "invalid"
    if host in _LOOPBACK_NAMES:
        return "loopback"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "hostname"
    if ip.is_loopback:
        return "loopback"
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in n for n in _V6_PUBLIC_TUNNELS):
        return "public"  # 6to4/Teredo embed a public IPv4 dest
    if ip.is_link_local:
        return "link_local"
    if ip in _CGNAT_NET:
        return "cgnat"
    if ip.is_private:
        return "private"
    return "public"


def _ollama_model_digest(endpoint: Optional[str], model: str) -> Tuple[Optional[str], str]:
    """Fail-closed probe of a local Ollama server for ``model``'s manifest digest.

    Delegates the raw outbound call to the audited ``agent.model_metadata`` probe
    (its sanctioned egress site) so no raw HTTP client is constructed in the web
    router. Returns the 64-hex sha256 digest with ``verified_present``; any
    failure/unknown tag yields ``(None, "probe_failed")``. Only ever called for a
    verified-local endpoint. Module-level so tests can monkeypatch it.
    """
    if not (endpoint or "").strip() or not (model or "").strip():
        return (None, "probe_failed")
    try:
        from agent.model_metadata import query_ollama_model_digest

        digest = query_ollama_model_digest(model, endpoint)
    except Exception:  # noqa: BLE001 — a probe failure must never break preflight
        return (None, "probe_failed")
    if digest:
        return (digest, "verified_present")
    return (None, "probe_failed")


def _engine_attestation(engine: str) -> Optional[Dict[str, Any]]:
    """Resolve the effective engine binding for an authenticated pre-run check.

    Returns the attestation the harness verifies BEFORE the first task: the
    effective engine/provider/model, an endpoint CLASSIFICATION (never the raw
    endpoint), local-vs-cloud, the resolved provider-cost policy, and — for a
    verified-local Ollama engine — the model manifest digest. ``None`` for an
    unknown/unbound engine (a fail-closed state the caller surfaces as such).

    Note: ``model``/``model_ref`` carry the Owner-supplied concrete tag. This is
    only returned on this authenticated SERVICE surface (``require_service_identity``)
    to the operator who set it — the gateway must NOT forward these fields to an
    end-user surface (see ``agent_identity``'s tag-confidentiality principle).
    """
    conn = engine_connection.resolve_connection(engine)
    if conn is None:
        return None
    from agent import usage_pricing as _up

    provider = conn.provider
    endpoint = conn.endpoint  # internal — classified, never returned raw
    is_local = conn.is_local_server()
    authorized = engine_connection.endpoint_is_authorized(endpoint) if is_local else True
    local_zero = _up.classify_local_zero(provider, endpoint)

    model: Optional[str] = conn.model
    model_status = "resolved"
    model_ref: Optional[str] = conn.model_ref
    # ECO must be pinned to the Owner-supplied concrete tag; when it is not
    # configured the resolved value is only the committed placeholder — report it
    # as required-but-absent and do not present the placeholder as the effective
    # model (the harness then refuses to dispatch).
    if engine == agent_identity.ECO_PROFILE_ID and not agent_identity.eco_model_configured():
        model = None
        model_ref = None
        model_status = "OWNER_MODEL_IDENTIFIER_REQUIRED"

    # Cost policy is decided from provider+endpoint (the same inputs the pricing
    # layer's ``classify_local_zero`` uses). The €0 branch in ``estimate_usage_cost``
    # only fires when the model has NO pricing entry; for a local Ollama tag that is
    # always the case (Ollama's /v1/models advertises no prices), so the two agree.
    # If a local model were ever given an explicit override/custom-contract entry,
    # that entry would price the call and this policy label would be optimistic —
    # not a concern for ECO, which carries no such entry.
    if local_zero:
        cost_policy = "local_zero_verified"
    elif is_local:
        cost_policy = "unpriced"  # local provider but endpoint not verified-local
    else:
        cost_policy = "campaign_budget_eur"

    att: Dict[str, Any] = {
        "engine_profile": engine,
        "engine_bound": True,
        "provider": provider,
        "model": model,
        "model_ref": model_ref,
        "model_identifier_status": model_status,
        "execution": "local" if is_local else "cloud",
        "endpoint_class": _endpoint_class(endpoint) if is_local else "cloud",
        # WAVE-30H #1: expose the non-secret normalized-authority fingerprint so the
        # preflight binding carries the SAME endpoint_fingerprint create derives from
        # this engine's resolved connection endpoint. Without it the engine-bound
        # preflight binding had endpoint_fingerprint=None while create supplied a real
        # hash -> binding_digest mismatch -> the run self-rejected (HTTP 412). This is
        # a derived hash of the already-resolved endpoint; no raw endpoint/credential
        # is exposed (mirrors effective_binding.compute_endpoint_fingerprint).
        "endpoint_fingerprint": eb.compute_endpoint_fingerprint(endpoint),
        "endpoint_authorized": bool(authorized),
        "provider_cost_policy": cost_policy,
    }
    if provider == "ollama" and local_zero and model:
        digest, dstatus = _ollama_model_digest(endpoint, model)
        att["ollama_model_digest"] = digest
        att["ollama_digest_status"] = dstatus
    else:
        att["ollama_model_digest"] = None
        att["ollama_digest_status"] = "not_applicable"
    return att


def _no_production_dataset() -> bool:
    """The benchmark plane never selects a production/customer dataset.

    This is an invariant of the plane (only synthetic scenarios are dispatched),
    but it is reported through a real gate rather than a bare literal: an explicit
    opt-in env would have to be set to ever admit production data, and setting it
    flips this to False so the harness's live-safety gate refuses the run.
    """
    return not _is_truthy_env("YOUTAB_AGENT_ALLOW_PRODUCTION_DATASET")


def _is_truthy_env(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


@router.get("/api/runtime/v1/preflight")
async def runtime_preflight(
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Authenticated live-benchmark preflight (WAVE-30B §12).

    Surfaces exactly what the harness needs to verify BEFORE a live run — the
    running build SHA, version, provider/model NAMES, and safety posture — and
    NOTHING sensitive: no secret value, authorization header, or raw config. The
    harness compares ``build_sha`` against its authorized SHA and refuses to run
    against a mismatched or production runtime.
    """
    from youtab_agent_cli import __version__ as engine_version
    from youtab_runtime.run_limits import CAMPAIGN_CEILING_EUR, RUN_CEILINGS

    try:
        from youtab_agent_cli.build_info import get_build_sha

        build_sha = get_build_sha(short=0)
    except Exception:  # noqa: BLE001
        build_sha = None

    try:
        from youtab_agent_cli import secret_file as _sf

        live_benchmark = _sf.live_benchmark_file_secrets_required()
    except Exception:  # noqa: BLE001
        live_benchmark = False

    names = _configured_model_provider_names()

    # Optional active campaign (set via env by the benchmark launcher).
    campaign_id = os.getenv("YOUTAB_AGENT_BENCHMARK_CAMPAIGN_ID", "").strip() or None
    remaining_eur: Optional[str] = None
    campaign_ceiling_eur: Optional[str] = None
    if campaign_id:
        try:
            from youtab_runtime import campaign_budget as _cb

            st = _cb.status(campaign_id)
            remaining_eur = str(st.remaining_eur)
            campaign_ceiling_eur = str(st.ceiling_eur)
        except Exception:  # noqa: BLE001
            remaining_eur = None

    # Audit/journal availability (best-effort import probe).
    try:
        import youtab_runtime.run_journal  # noqa: F401

        audit_available = True
    except Exception:  # noqa: BLE001
        audit_available = False

    # Optional per-engine attestation (WAVE-30D §B3). When the harness asks for a
    # specific engine (``?engine=eco.v01``) the runtime resolves the EFFECTIVE
    # binding it would execute and reports it so the harness can verify the exact
    # engine/provider/model/endpoint-class/cost-policy BEFORE the first task, and
    # refuse on any mismatch. Absent the param, behaviour is unchanged.
    engine_param = (request.query_params.get("engine") or "").strip()
    attestation: Optional[Dict[str, Any]] = None
    engine_error: Optional[str] = None
    if engine_param:
        attestation = _engine_attestation(engine_param)
        if attestation is None:
            engine_error = "unknown_or_unbound_engine"
    cost_policy = (attestation or {}).get("provider_cost_policy")
    model_status = (attestation or {}).get("model_identifier_status")

    # Budget enforcement is honestly ARMED only when it can actually enforce:
    #  * campaign arm — a campaign id is set AND that campaign is genuinely OPEN
    #    (``remaining_eur`` resolved). A stale/never-opened id must not read armed
    #    (M1): the worker would fail closed on reserve, and a false-green preflight
    #    would erode the "budget armed before the first call" guarantee.
    #  * local-zero arm — the engine resolves to a verified local-zero cost policy
    #    AND its model is actually RESOLVED (an unset YOUTAB_ECO_MODEL cannot run,
    #    so it must not report armed). Needs no campaign and no FX snapshot — a €0
    #    conversion requires neither, so none is fabricated (WAVE-30D §B5).
    campaign_armed = bool(campaign_id) and remaining_eur is not None
    local_zero_armed = cost_policy == "local_zero_verified" and model_status == "resolved"
    budget_enforced = campaign_armed or local_zero_armed

    # WAVE-30H: the canonical effective binding the runtime WOULD bind for this
    # dispatch — ONE contract. When an engine is named it is the per-engine
    # attestation (which MAY have probed the digest) projected into the binding
    # shape; otherwise it is the config-default identity built purely. The harness
    # verifies its Owner --expected-* AGAINST this (never a parallel identity).
    if attestation is not None:
        _att_digest = attestation.get("ollama_model_digest")
        if _att_digest:
            _digest_status = "attested"
        elif attestation.get("ollama_digest_status") == "not_applicable":
            _digest_status = "not_applicable"
        else:
            _digest_status = "not_probed"
        effective_binding = {
            "binding_version": 1,
            "provider": attestation.get("provider"),
            "model": attestation.get("model"),
            "model_ref": attestation.get("model_ref"),
            "model_identifier_status": attestation.get("model_identifier_status"),
            "execution": attestation.get("execution"),
            "endpoint_class": attestation.get("endpoint_class"),
            # WAVE-30H #1: carry the endpoint_fingerprint so this binding's
            # binding_digest (which includes it) equals the digest create computes
            # for the SAME engine — the atomic preflight->create match.
            "endpoint_fingerprint": attestation.get("endpoint_fingerprint"),
            "provider_cost_policy": attestation.get("provider_cost_policy"),
            "digest_status": _digest_status,
            "model_digest": _att_digest,
            "bound_at": eb.utc_iso_now(),
        }
    else:
        effective_binding = eb.build_effective_binding(
            provider=names.get("provider"),
            model=names.get("model"),
            endpoint=_configured_inference_base_url(),
        )

    return {
        "ok": True,
        "service_ready": True,
        "build_sha": build_sha,
        "engine_version": engine_version,
        "contract_version": CONTRACT_VERSION,
        "model": names["model"],
        "provider": names["provider"],
        "provider_credential_source": _provider_credential_source(names["provider"]),
        "redaction_enabled": _redaction_enabled(),
        "budget_enforcement_enabled": budget_enforced,
        "budget_enforcement_source": (
            "campaign_ledger" if campaign_armed
            else ("local_zero_verified" if local_zero_armed else "none")
        ),
        "live_benchmark_mode": live_benchmark,
        "campaign_id": campaign_id,
        "campaign_ceiling_eur": campaign_ceiling_eur,
        "remaining_eur": remaining_eur,
        "hard_campaign_ceiling_eur": str(CAMPAIGN_CEILING_EUR),
        "run_limit_ceilings": dict(RUN_CEILINGS),
        "audit_available": audit_available,
        "no_production_dataset": _no_production_dataset(),
        "auth_required": True,
        # Per-engine effective-binding attestation (None unless ?engine= given).
        "engine_attestation": attestation,
        "engine_attestation_error": engine_error,
        # WAVE-30H canonical effective binding the harness verifies --expected-*
        # against (one contract; present for both tracks).
        "effective_binding": effective_binding,
    }


@router.get("/api/runtime/v1/capabilities")
async def runtime_capabilities(
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    from youtab_agent_cli import __version__ as engine_version
    return {
        "contract_version": CONTRACT_VERSION,
        "engine_version": engine_version,
        "supported": [
            "agents", "runs.create", "runs.list", "runs.detail",
            "runs.events", "runs.cancel", "runs.retry",
            "runs.logs", "tools", "skills", "sandboxes", "artifacts",
        ],
        # Honest capability flags — the connector/UI must not offer what the
        # engine does not genuinely do. The kanban engine has no resume-from-
        # checkpoint primitive; recovery is retry (a fresh run), so resume is
        # reported unsupported rather than faked.
        "resume_supported": False,
        "logs_supported": True,
        "sandbox_backends": _sandbox_backends().get("backends", []),
        "reasoning_strategies": ["single_shot", "goal_loop"],
    }


@router.get("/api/runtime/v1/agents")
async def runtime_agents(identity: RuntimeIdentity = Depends(require_service_identity)):
    from youtab_agent_cli import profiles
    try:
        agents = [_agent_projection(p) for p in profiles.list_profiles()]
    except Exception as exc:  # noqa: BLE001
        _log.warning("runtime agents projection failed: %s", exc)
        raise HTTPException(status_code=503, detail={"error": "runtime_unavailable"})
    return {"agents": agents, "runtime_available": True}


@router.get("/api/runtime/v1/engines")
async def runtime_engines(
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Branded product-engine catalogue with HONEST availability.

    Rows are the branded roster (``agent_identity.public_agents()``).
    ``availability`` is a REAL check — a bounded loopback reachability probe for
    a local engine, or an installed-credential presence check for an external
    one — cached briefly. The projection carries ONLY branded fields; the
    provider/model/endpoint/credential behind an engine never appear.
    """
    engines: List[Dict[str, Any]] = []
    for ident in agent_identity.public_agents():
        engines.append({
            "profile_id": ident.profile_id,
            "public_label": ident.public_label,
            "display_name": ident.display_name,
            "display_version": ident.display_version,
            "role": ident.role,
            "icon": ident.icon,
            "availability": _engine_availability_cached(ident.profile_id),
            "supported_modalities": _engine_modalities(ident.role),
        })
    return {"engines": engines}


@router.get("/api/runtime/v1/agents/{agent_id}")
async def runtime_agent_detail(
    agent_id: str, identity: RuntimeIdentity = Depends(require_service_identity)
):
    from youtab_agent_cli import profiles
    for p in profiles.list_profiles():
        if getattr(p, "name", None) == agent_id:
            return _agent_projection(p)
    raise HTTPException(status_code=404, detail={"error": "agent_not_found"})


@router.get("/api/runtime/v1/runs")
async def runtime_list_runs(
    identity: RuntimeIdentity = Depends(require_service_identity),
    limit: int = 25,
    offset: int = 0,
    status: Optional[str] = None,
):
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    def _collect() -> "List[Dict[str, Any]]":
        # Runs on a worker thread (see run_in_threadpool below): the kanban
        # store is synchronous SQLite, and doing it inline on the event loop
        # blocks every other request that uvicorn worker is serving. The
        # connection is opened and closed entirely within this thread.
        with kb.connect_closing(board=RUNTIME_BOARD) as conn:
            # Tenant is the DB-level filter; user ownership is enforced per row so
            # a tenant admin still can't read another user's runs here.
            tasks = kb.list_tasks(
                conn, tenant=identity.tenant, include_archived=True,
                order_by="created-desc",
            )
            owned = [t for t in tasks if t.created_by == identity.user]
            # One batched query for cancel/mode signals instead of a per-task
            # full-history list_events fan-out (the N+1).
            signals = _summary_signals(conn, [t.id for t in owned])
            out: List[Dict[str, Any]] = []
            for t in owned:
                cancelled, mode = signals.get(t.id, (False, "model"))
                out.append(_run_summary(t, cancelled=cancelled, execution_mode=mode))
            return out

    summaries = await run_in_threadpool(_collect)
    if status is not None:
        summaries = [s for s in summaries if s["status"] == status]
    total = len(summaries)
    page = summaries[offset:offset + limit]
    return {
        "runs": page, "total": total, "limit": limit, "offset": offset,
        "runtime_available": True,
    }


@router.get("/api/runtime/v1/runs/{run_id}")
async def runtime_run_detail(
    run_id: str, identity: RuntimeIdentity = Depends(require_service_identity)
):
    def _detail() -> "Dict[str, Any]":
        with kb.connect_closing(board=RUNTIME_BOARD) as conn:
            task = _load_owned_task(conn, run_id, identity)
            events = kb.list_events(conn, task.id)
            return _run_detail(conn, task, cancelled=_is_cancelled(events))

    return await run_in_threadpool(_detail)


@router.get("/api/runtime/v1/runs/{run_id}/events")
async def runtime_run_events(
    run_id: str,
    identity: RuntimeIdentity = Depends(require_service_identity),
    after: int = 0,
    limit: int = 500,
):
    """Resumable, ordered event cursor. Returns events with ``id > after``.

    Ordering is by monotonic ``id`` (== ``created_at`` order); the connector
    reconnects from the last acknowledged cursor. ``terminal`` flips true once
    the run reaches a terminal product state, so the connector/UI can stop
    polling with correct final state.
    """
    after = max(0, int(after))
    limit = max(1, min(int(limit), 2000))

    def _collect() -> "Dict[str, Any]":
        with kb.connect_closing(board=RUNTIME_BOARD) as conn:
            task = _load_owned_task(conn, run_id, identity)
            all_events = kb.list_events(conn, task.id)
            cancelled = _is_cancelled(all_events)
            fresh = [e for e in all_events if e.id > after][:limit]
            cursor = fresh[-1].id if fresh else after
            status = _run_status(
                task, cancelled=cancelled,
                interactive=_interactive_status(all_events),
            )
        return {
            "events": [_event_projection(run_id, e) for e in fresh],
            "cursor": cursor,
            "status": status,
            "terminal": status in _TERMINAL_PRODUCT_STATUSES,
        }

    return await run_in_threadpool(_collect)


@router.get("/api/runtime/v1/runs/{run_id}/artifacts")
async def runtime_run_artifacts(
    run_id: str, identity: RuntimeIdentity = Depends(require_service_identity)
):
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = _load_owned_task(conn, run_id, identity)
        attachments = kb.list_attachments(conn, task.id)
        return {"artifacts": [_artifact_ref(run_id, a) for a in attachments]}


@router.get("/api/runtime/v1/runs/{run_id}/artifacts/{artifact_id}")
async def runtime_run_artifact_content(
    run_id: str,
    artifact_id: int,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Stream one owned run's artifact bytes. (tenant,user)-scoped (404 otherwise)."""
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = _load_owned_task(conn, run_id, identity)
        att = kb.get_attachment(conn, int(artifact_id))
        if att is None or att.task_id != task.id:
            raise HTTPException(status_code=404, detail={"error": "artifact_not_found"})
    try:
        with open(att.stored_path, "rb") as f:
            data = f.read()
    except OSError:
        raise HTTPException(status_code=404, detail={"error": "artifact_unavailable"})
    return Response(
        content=data,
        media_type=att.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{att.filename}"'},
    )


@router.get("/api/runtime/v1/runs/{run_id}/logs")
async def runtime_run_logs(
    run_id: str,
    identity: RuntimeIdentity = Depends(require_service_identity),
    tail_bytes: int = 65536,
):
    """Return the owned run's worker log (bounded tail). Honest empty when none yet."""
    tail_bytes = max(1024, min(int(tail_bytes), 1_048_576))
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = _load_owned_task(conn, run_id, identity)
    text = kb.read_worker_log(task.id, tail_bytes=tail_bytes, board=RUNTIME_BOARD)
    return {
        "run_id": run_id,
        "present": text is not None,
        "logs": text or "",
        "truncated": bool(text is not None and len(text.encode("utf-8", "replace")) >= tail_bytes),
    }


@router.get("/api/runtime/v1/tools")
async def runtime_tools(identity: RuntimeIdentity = Depends(require_service_identity)):
    return _tools_catalog()


@router.get("/api/runtime/v1/skills")
async def runtime_skills(identity: RuntimeIdentity = Depends(require_service_identity)):
    return _skills_catalog()


@router.get("/api/runtime/v1/sandboxes")
async def runtime_sandboxes(
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    return _sandbox_backends()


# ---------------------------------------------------------------------------
# Mutating endpoints (signed commands)
# ---------------------------------------------------------------------------


@router.post("/api/runtime/v1/runs")
async def runtime_create_run(
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Create and dispatch a real run. Signed + idempotent.

    Body: ``{ "agent": "<profile>", "task": "<prompt>", "goal_mode"?: bool,
    "title"?: str, "skills"?: [str], "max_runtime_seconds"?: int,
    "engine"?: "<profile_id>" }``.

    ``agent`` (the worker profile) is required. ``engine`` is optional and
    orthogonal: it selects the branded model substrate (e.g. ``eco.v01``) and,
    when that engine is bound to a provider/model, pins the run to it via
    ``model_override``/``provider_override`` without touching the profile.
    """
    # WAVE-30H R8: ingress marks. REQUEST_RECEIVE = synchronous ingress latency
    # (monotonic, recorded at ack with the now-known run_id). enqueue_epoch_ns is a
    # wall-clock mark carried into the create event so the dispatcher can compute
    # QUEUE_WAIT as a cross-process (clock="epoch") gap — the coarse seconds-grained
    # task timestamps cannot resolve a sub-second local queue wait.
    _req_recv_t0 = time.monotonic_ns()
    try:
        from youtab_runtime import stage_trace as _st_ing
    except Exception:  # pragma: no cover - observability never blocks ingress
        _st_ing = None
    await _verify_signed_command(request, identity)
    # Execution-authority gate (WAVE-30H R3): AFTER transport auth, BEFORE any
    # run is created. Managed mode requires a valid Simorgh grant; standalone
    # refuses one. Fail-closed inside (raises HTTPException on any bad grant).
    grant_header, grant_manifest = await _admit_execution_grant(request, identity)
    payload = await _json_body(request)
    agent = str(payload.get("agent") or "").strip()
    task_text = str(payload.get("task") or "").strip()
    if not agent or not task_text:
        raise HTTPException(status_code=422, detail={"error": "agent_and_task_required"})

    from youtab_agent_cli import profiles
    known = {getattr(p, "name", None) for p in profiles.list_profiles()}
    if agent not in known:
        raise HTTPException(status_code=404, detail={"error": "agent_not_found"})

    # Optional branded-engine selection (a profile_id). Additive and orthogonal
    # to ``agent``: ``agent`` picks the worker profile, ``engine`` picks the model
    # substrate. A known-but-unbound engine is honoured (recorded) but runs on the
    # profile default; an unknown engine is a client error.
    engine = str(payload.get("engine") or "").strip()
    engine_identity = None
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    if engine:
        engine_identity = agent_identity.identity_for_profile(engine)
        if engine_identity is None:
            raise HTTPException(status_code=422, detail={"error": "unknown_engine"})
        bound = agent_identity.engine_binding_for_profile(engine)
        if bound:
            provider_override, model_override = bound

        # WAVE-30D B1 (fail CLOSED): the ECO local engine (Track A) must be
        # pinned to the Owner-supplied concrete model tag. Its committed binding
        # is only a provider-neutral placeholder, so without YOUTAB_ECO_MODEL a
        # run would silently execute the placeholder. Refuse before dispatch —
        # never a silent downgrade to the placeholder or the worker default. The
        # tag itself is never echoed (only its presence is checked).
        if engine == agent_identity.ECO_PROFILE_ID and not agent_identity.eco_model_configured():
            raise HTTPException(
                status_code=422, detail={"error": "eco_model_unconfigured"}
            )
        # A local-server engine must resolve to a concrete provider+model pin;
        # it must never dispatch on the worker's profile-default model (that
        # would falsely attribute the run to the branded engine).
        _pin = engine_connection.resolve_connection(engine)
        if _pin is not None and _pin.is_local_server() and not (bound and model_override):
            raise HTTPException(
                status_code=422, detail={"error": "engine_unbound"}
            )
        # Managed direct-API fail-closed (WAVE-30H): a MANAGED run that selected a
        # branded engine MUST resolve to a concrete (provider, model) pin — never
        # silently dispatch the worker default on an unbound (e.g. cloud) engine,
        # which would falsely attribute the run and bypass the binding contract.
        # Standalone is exempt (an unbound engine is honoured + recorded below).
        from youtab_runtime import managed_execution as _mx_gate
        if _mx_gate.current_trust_mode() is _mx_gate.TrustMode.MANAGED and (
            _pin is None or not model_override
        ):
            raise HTTPException(
                status_code=422, detail={"error": "engine_unbound"}
            )

        # Split-brain guard (fail CLOSED): a local-server engine (e.g. ECO on an
        # on-prem Ollama) must execute against the SAME server its availability
        # probe used. If the configured inference base_url names a different
        # host:port than the canonical connection, refuse — never run somewhere
        # health never validated, and never report a false "online". The tenant
        # sees only a neutral error; the endpoints are not written to the log.
        _conn = engine_connection.resolve_connection(engine)
        if _conn is not None and _conn.is_local_server():
            try:
                engine_connection.assert_consistent(_conn, _configured_inference_base_url())
            except engine_connection.ConnectionMismatchError:
                _log.warning(
                    "engine %s failed the availability/execution endpoint "
                    "consistency check; failing closed",
                    engine,
                )
                raise HTTPException(status_code=503, detail={"error": "engine_unavailable"})

    title = str(payload.get("title") or f"[{identity.tenant}] {task_text[:80]}").strip()
    goal_mode = bool(payload.get("goal_mode", False))
    skills = payload.get("skills") if isinstance(payload.get("skills"), list) else None
    try:
        max_runtime = payload.get("max_runtime_seconds")
        max_runtime = int(max_runtime) if max_runtime is not None else None
    except (TypeError, ValueError):
        max_runtime = None

    # Authoritative, server-clamped per-run limits (WAVE-30B §8). Accepts either a
    # nested ``limits`` object or the flat top-level fields; validates + clamps to
    # the server ceilings and refuses invalid values. Runtime enforcement — not
    # the harness — is the authority; the clamped set is persisted below.
    from youtab_runtime.run_limits import RunLimitError, RunLimits

    _raw_limits = payload.get("limits")
    if not isinstance(_raw_limits, dict):
        _raw_limits = {
            k: payload.get(k)
            for k in (
                "max_input_tokens", "max_output_tokens", "max_total_tokens",
                "max_iterations", "max_requests", "max_retries",
                "max_concurrency", "max_cost_eur", "failure_threshold",
            )
            if payload.get(k) is not None
        }
    try:
        run_limits = RunLimits.validate_and_clamp(_raw_limits)
    except RunLimitError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_limits", "reason": str(exc)}
        ) from exc
    # max_runtime_seconds is clamped through the same ceiling for one authority.
    if max_runtime is not None and run_limits.max_runtime_seconds is None:
        run_limits = RunLimits.validate_and_clamp(
            {**run_limits.to_dict(), "max_runtime_seconds": max_runtime}
        )
    if run_limits.max_runtime_seconds is not None:
        max_runtime = run_limits.max_runtime_seconds

    # Authoritative worker-attempt bound (WAVE-30D dispatcher-attempt fix).
    #
    # The dispatcher's per-task circuit breaker owns worker (re)spawn and already
    # enforces this run's ``max_runtime_seconds``. The run's authoritative retry
    # budget must bound the number of worker ATTEMPTS the same way — otherwise a
    # worker that times out or crashes is silently respawned by the dispatcher's
    # default failure limit, and the fresh worker issues ANOTHER model request even
    # though the run declared ``max_requests=1`` / no retries (the WAVE-30D canary
    # ``t_f6ef707c`` defect: claimed→spawned→timed_out→claimed→spawned→timed_out→
    # gave_up = two attempts, two potential model requests).
    #
    # ``tasks.max_retries`` is the consecutive-failure count at which the breaker
    # trips, i.e. the TOTAL attempts allowed (``1`` trips on the first failure =
    # one attempt / zero retries). The run-level ``RunLimits.max_retries`` counts
    # RETRIES (``0`` = no retry — see run_limits.py), so worker attempts =
    # ``1 + max_retries``:
    #   * canary  max_retries=0 → 1 attempt  (no respawn — the fix)
    #   * pilot   max_retries=1 → 2 attempts (unchanged: was DEFAULT_FAILURE_LIMIT=2)
    #   * full    max_retries=1 → 2 attempts (unchanged: was DEFAULT_FAILURE_LIMIT=2)
    # Left ``None`` for ordinary runs that send no retry budget, preserving the
    # dispatcher default (``DEFAULT_FAILURE_LIMIT``; an ordinary run's breaker is
    # never touched). When a retry budget IS declared it becomes authoritative:
    # the attempt count is exactly ``1 + max_retries``, which for a run that asks
    # for >=2 retries is DELIBERATELY higher than the dispatcher default of 2 —
    # the run owns its own attempt budget. It is never unbounded: ``max_retries``
    # is clamped to ``RUN_CEILINGS["max_retries"]`` (3), so attempts <= 4, and
    # cost stays bounded by the durable campaign ledger. Only zero-retry runs (the
    # canary) tighten below the default; the stage profiles use only 0/1.
    _task_max_retries = (
        int(run_limits.max_retries) + 1
        if run_limits.max_retries is not None
        else None
    )

    # Execution mode: real provider-backed model by default. A caller may request
    # the non-production deterministic integration worker with ``deterministic``;
    # it is honoured ONLY when the deterministic worker is enabled (non-prod), so
    # a production run can never be silently downgraded to a deterministic stub.
    want_det = bool(payload.get("deterministic", False))
    mode = "deterministic" if (want_det and _deterministic_worker_enabled()) else "model"

    # WAVE-30H canonical binding: resolve the EFFECTIVE execution identity PURELY
    # (config/classification only — NO network probe on this hot path). A bound
    # engine uses its resolved connection; otherwise we resolve the worker's PROFILE
    # default (the identity the dispatched worker truly uses). The binding itself is
    # built + persisted AFTER the run id is known (inside the create txn below).
    _conn_for_binding = engine_connection.resolve_connection(engine) if engine else None
    if _conn_for_binding is not None:
        _bind_provider = _conn_for_binding.provider
        _bind_model = _conn_for_binding.model
        _bind_endpoint = _conn_for_binding.endpoint
    else:
        _bind_provider, _bind_model, _bind_endpoint = _profile_default_identity(agent)

    from youtab_runtime import managed_execution as _mx_bind
    _is_managed = _mx_bind.current_trust_mode() is _mx_bind.TrustMode.MANAGED

    # Atomic preflight->create: when the caller pins an expected binding digest (the
    # benchmark does, derived from its preflight attestation), the run is created
    # ONLY if the substrate digest matches — closing the TOCTOU between what was
    # attested and what is enqueued. Opt-in (absent = unvalidated); checked before
    # any enqueue so a mismatch creates no task, no binding, no dispatch.
    _substrate_binding = eb.build_effective_binding(
        provider=_bind_provider, model=_bind_model, endpoint=_bind_endpoint,
    )
    _expected_digest = str(payload.get("expected_binding_digest") or "").strip().lower()
    if _expected_digest:
        if len(_expected_digest) != 64 or any(
            c not in "0123456789abcdef" for c in _expected_digest
        ):
            raise HTTPException(status_code=422, detail={"error": "malformed_binding_digest"})
        if _expected_digest != eb.binding_digest(_substrate_binding):
            raise HTTPException(status_code=412, detail={"error": "binding_digest_mismatch"})

    # WAVE-30H #7: an optional attested model-manifest digest pins the concrete model
    # artifact into the persisted binding (digest_status="attested"); the worker
    # re-probes + fails closed on a tag->manifest re-point. It is only meaningful for
    # a probe-eligible ollama-local substrate (the same eligibility the builder uses
    # for "not_probed") — reject it fail-closed otherwise so a caller cannot pin a
    # digest that will never be verified.
    _expected_model_digest = str(payload.get("expected_model_digest") or "").strip().lower()
    if _expected_model_digest:
        if len(_expected_model_digest) != 64 or any(
            c not in "0123456789abcdef" for c in _expected_model_digest
        ):
            raise HTTPException(status_code=422, detail={"error": "malformed_model_digest"})
        if _substrate_binding.get("digest_status") != "not_probed":
            raise HTTPException(status_code=422, detail={"error": "model_digest_not_applicable"})

    # A managed, real-model run MUST carry a fully-resolved identity before it is
    # executable (never enqueue an unresolved managed model-run — the worker would
    # otherwise resolve the substrate from mutable config at dispatch). WAVE-30H
    # hardening: require present tenant/workspace and a RESOLVED endpoint too, so an
    # unresolvable local endpoint fails fast at create (422) rather than as a deferred
    # worker endpoint-drift refusal. The deterministic integration worker (mode !=
    # "model") and standalone are exempt.
    if _is_managed and mode == "model":
        if not (_bind_model and str(_bind_model).strip()):
            raise HTTPException(status_code=422, detail={"error": "managed_model_unresolved"})
        if not (identity.tenant and identity.tenant.strip()):
            raise HTTPException(status_code=422, detail={"error": "managed_tenant_required"})
        if not (identity.workspace and identity.workspace.strip()):
            raise HTTPException(status_code=422, detail={"error": "managed_workspace_required"})
        if (
            _substrate_binding["execution"] == "local"
            and _substrate_binding["endpoint_class"] in ("unavailable", "invalid")
        ):
            raise HTTPException(status_code=422, detail={"error": "managed_endpoint_unresolved"})
        # Pin the row from the resolved identity so the dispatcher + any respawn
        # dispatch FROM the binding and never re-resolve mutable config (an explicit
        # engine binding already set model_override above).
        if not model_override:
            model_override = _bind_model
        if not provider_override and _bind_provider:
            provider_override = _bind_provider

    _enqueue_epoch_ns = _st_ing.mark_epoch() if _st_ing is not None else None

    def _persist_run_metadata(conn, rid):
        """Create-once metadata persisted INSIDE create_task_ex's own write_txn.

        WAVE-30H (race): the resolved mode + the canonical immutable binding + engine
        selection + clamped limits + the admitted Simorgh grant all commit ATOMICALLY
        with the task row insert, so the run is never visible to the dispatcher before
        its binding/grant exist (closing the two-transaction ready-task race). Runs
        exactly once per created run (create_task_ex does not call this on an
        idempotent hit). Uses _append_event directly (no nested write_txn); a raise
        here rolls back the whole insert (no orphan task) — fail closed.
        """
        kb._append_event(conn, rid, _MODE_EVENT, {
            "mode": mode,
            "correlation_id": identity.correlation_id,
            # R8: ns wall-clock enqueue mark for cross-process QUEUE_WAIT.
            "enqueue_epoch_ns": _enqueue_epoch_ns,
        })
        # The canonical immutable binding — scoped to this run (run_id/tenant/
        # workspace), SELF-HASHED, and (WAVE-30H #7) pinning the attested model digest
        # when supplied so the worker fails closed on a tag->manifest re-point.
        effective_binding_payload = eb.build_effective_binding(
            provider=_bind_provider, model=_bind_model, endpoint=_bind_endpoint,
            run_id=rid, root_run_id=rid,
            tenant=identity.tenant, workspace=identity.workspace,
            model_digest=(_expected_model_digest or None),
        )
        kb._append_event(conn, rid, eb.BINDING_EVENT, effective_binding_payload)
        if engine_identity is not None:
            kb._append_event(conn, rid, _ENGINE_EVENT, {
                "profile_id": engine,
                "public_label": engine_identity.public_label,
            })
        _limits_dict = run_limits.to_dict()
        if _task_max_retries is not None:
            _limits_dict["worker_attempt_limit"] = _task_max_retries
        if _limits_dict:
            kb._append_event(conn, rid, _LIMITS_EVENT, _limits_dict)
        if grant_header:
            kb._append_event(conn, rid, _GRANT_EVENT, {"grant": grant_header})
            if grant_manifest is not None:
                kb._append_event(conn, rid, _GRANT_MANIFEST_EVENT, grant_manifest)

    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        run_id, created = kb.create_task_ex(
            conn,
            title=title,
            body=task_text,
            assignee=agent,
            created_by=identity.user,
            tenant=identity.tenant,
            idempotency_key=identity.idempotency_key,
            skills=skills,
            goal_mode=goal_mode,
            max_runtime_seconds=max_runtime,
            max_retries=_task_max_retries,
            model_override=model_override,
            provider_override=provider_override,
            board=RUNTIME_BOARD,
            # Authoritative dedicated correlation column (canonical v1). The
            # session_id write below stays UNCHANGED as the legacy overload.
            correlation_id=identity.correlation_id,
            session_id=identity.correlation_id,
            # Persist binding/grant/mode atomically with the row (see hook).
            on_created=_persist_run_metadata,
        )
        # WAVE-30H #8 + Batch2 #F4: on an idempotent hit the run already exists with
        # its persisted binding; a caller pinning an expected digest must have it match
        # the EXISTING run's binding — else a replayed Idempotency-Key could attach the
        # attestation to a run bound to a DIFFERENT substrate OR a DIFFERENT pinned
        # model artifact. The substrate digest (binding_digest) EXCLUDES model_digest,
        # so the model-artifact pin is compared SEPARATELY (constant-time). Any
        # mismatch fails closed (412); no new task/event/grant/binding is created, and
        # a run pinned to a different model artifact is never returned as if it matched.
        if not created and (_expected_digest or _expected_model_digest):
            _existing = eb.effective_binding_from_events(kb.list_events(conn, run_id))
            if (
                not isinstance(_existing, dict)
                or _existing.get("__corrupt__")
                or not eb.verify_binding(_existing)
            ):
                raise HTTPException(
                    status_code=412,
                    detail={"error": "binding_unverifiable_for_idempotent_run"},
                )
            if _expected_digest and eb.binding_digest(_existing) != _expected_digest:
                raise HTTPException(status_code=412, detail={"error": "binding_digest_mismatch"})
            if _expected_model_digest:
                import hmac as _hmac
                _existing_md = str(_existing.get("model_digest") or "").strip().lower()
                if not _existing_md or not _hmac.compare_digest(
                    _existing_md, _expected_model_digest
                ):
                    raise HTTPException(
                        status_code=412, detail={"error": "model_digest_mismatch"}
                    )
        task = kb.get_task(conn, run_id)

    # Kick a dispatch tick immediately and keep the ticker running so the run
    # actually executes and finalises.
    ensure_dispatcher_running()
    _dispatch_tick()
    # R8: total synchronous ingress latency, now fully correlated (run_id known).
    if _st_ing is not None:
        try:
            with _st_ing.trace_context_scope(
                run_id=run_id,
                tenant=identity.tenant,
                user=identity.user,
                correlation_id=identity.correlation_id,
            ):
                _st_ing.record(
                    _st_ing.Stage.REQUEST_RECEIVE,
                    duration_ns=time.monotonic_ns() - _req_recv_t0,
                    result=("created" if created else "idempotent_hit"),
                )
        except Exception:  # pragma: no cover - observability never blocks ingress
            pass
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = kb.get_task(conn, run_id)
        return (
            _run_summary(task, execution_mode=mode)
            if task
            else {"run_id": run_id, "status": "queued", "execution_mode": mode}
        )


@router.post("/api/runtime/v1/runs/{run_id}/cancel")
async def runtime_cancel_run(
    run_id: str,
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Cancel a run: record the intent, kill any live worker, block the task.

    A cancel of an ALREADY-TERMINAL run (completed, or previously cancelled) is
    an idempotent NO-OP that preserves the existing terminal status: it records
    no cancel intent and never relabels a finished run. Otherwise it records the
    product-cancel intent, kills any live worker, and blocks the task.
    """
    await _verify_signed_command(request, identity)
    # Managed cancel is a governed mutation: it requires the same execution
    # authority (a Simorgh grant) in managed mode; standalone refuses a grant.
    await _admit_execution_grant(request, identity)
    worker_pid = None
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        # ATOMIC check-then-act. The ownership decision, the authoritative
        # terminal-state read, and the cancel-transition append all happen under
        # ONE ``BEGIN IMMEDIATE`` write lock (``kanban_db.write_txn`` — the run
        # store's canonical single-writer transaction). SQLite admits exactly one
        # writer at a time, so a worker completion cannot commit between the read
        # and the append: it has either already committed (we observe the
        # ``completed`` projection and no-op) or it is forced to wait until this
        # transaction finishes. This closes the check-then-act race where a
        # completion that landed *after* a pre-lock terminality read got
        # relabelled ``cancelled``, overwriting a successful completion.
        with kb.write_txn(conn):
            # Ownership is enforced inside the lock too (404 on any mismatch;
            # rolls back, writes nothing — tenant isolation is preserved).
            task = _load_owned_task(conn, run_id, identity)
            # Authoritative, lock-stable terminality. A completed run projects
            # ``completed``; an already-cancelled run projects ``cancelled`` (its
            # cancel event already exists). Either way the cancel is an idempotent
            # NO-OP that returns the preserved terminal status and appends NO
            # second event — a late cancel must never overwrite a finished run's
            # outcome, and a repeated cancel must never emit a duplicate
            # transition.
            current_status = _run_status(
                task, cancelled=_is_cancelled(kb.list_events(conn, task.id))
            )
            if current_status in _TERMINAL_PRODUCT_STATUSES:
                return {"run_id": run_id, "status": current_status}
            # Non-terminal under the lock: record EXACTLY ONE product-cancel
            # intent (also how detail/list project ``cancelled`` rather than a
            # plain block/archive).
            kb._append_event(conn, task.id, _CANCEL_EVENT_KIND, {"by": identity.user})
            worker_pid = task.worker_pid

    if worker_pid:
        try:
            from gateway.status import terminate_pid
            terminate_pid(int(worker_pid))
        except Exception as exc:  # noqa: BLE001 — best-effort kill; block still applies
            _log.warning("runtime cancel: could not terminate pid %s: %s", worker_pid, exc)

    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = kb.get_task(conn, run_id)
        if task is not None and task.status not in ("done", "archived", "blocked"):
            try:
                kb.block_task(conn, run_id, reason="cancelled by user")
            except Exception as exc:  # noqa: BLE001 — cancel event is the source of truth
                _log.warning("runtime cancel: block failed for %s: %s", run_id, exc)
        task = kb.get_task(conn, run_id)
        return {"run_id": run_id, "status": _run_status(task, cancelled=True)}


@router.post("/api/runtime/v1/runs/{run_id}/answer")
async def runtime_answer_run(
    run_id: str,
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Answer a clarification question the agent raised (ADR-0004, A4).

    Records the user's answer to an OPEN question so the SAME run resumes. Same
    governed-mutation authority as cancel: signed + (managed) grant-gated +
    (tenant,user)-owned. Idempotent: a duplicate answer for an already-answered
    question is a no-op (exactly-once consumption by the worker). Fail-closed:
    an answer for a question that was never asked / already answered is refused.
    """
    await _verify_signed_command(request, identity)
    await _admit_execution_grant(request, identity)
    body = await _json_body(request)
    question_id = str(body.get("question_id") or "").strip()
    if not question_id:
        raise HTTPException(status_code=422, detail={"error": "question_id_required"})
    if "answer" not in body:
        raise HTTPException(status_code=422, detail={"error": "answer_required"})
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        with kb.write_txn(conn):
            task = _load_owned_task(conn, run_id, identity)
            events = kb.list_events(conn, task.id)
            # Idempotent exactly-once replay is a NON-mutating read-back, so it is
            # evaluated BEFORE the terminal guard: the worker may consume the
            # answer and drive the run to a terminal state between the original
            # answer and a retried duplicate, but a duplicate for an already-
            # answered question must still succeed (200 already_answered), never
            # 409 run_terminal. The terminal guard below only blocks NEW answers.
            if _rc.answer_for(events, question_id) is not None:
                return {"run_id": run_id, "question_id": question_id, "already_answered": True}
            if _run_status(task, cancelled=_is_cancelled(events)) in _TERMINAL_PRODUCT_STATUSES:
                raise HTTPException(status_code=409, detail={"error": "run_terminal"})
            open_q = any(
                e.kind == _rc.QUESTION and (e.payload or {}).get("question_id") == question_id
                for e in events
            )
            if not open_q:
                raise HTTPException(status_code=409, detail={"error": "no_such_open_question"})
            kb._append_event(conn, task.id, _rc.ANSWER,
                             {"question_id": question_id, "answer": body["answer"],
                              "by": identity.user})
    return {"run_id": run_id, "question_id": question_id, "accepted": True}


@router.post("/api/runtime/v1/runs/{run_id}/approve")
async def runtime_approve_run(
    run_id: str,
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Approve or deny an approval the agent requested (ADR-0004, A5).

    ``{"approval_id": "...", "decision": "approve"|"deny"}``. Approve continues
    the effect; deny fails that effect CLOSED (the worker never performs it).
    Idempotent by approval_id; fail-closed on an unknown/closed approval.
    """
    await _verify_signed_command(request, identity)
    await _admit_execution_grant(request, identity)
    body = await _json_body(request)
    approval_id = str(body.get("approval_id") or "").strip()
    decision = str(body.get("decision") or "").strip().lower()
    if not approval_id:
        raise HTTPException(status_code=422, detail={"error": "approval_id_required"})
    if decision not in (_rc.APPROVE, _rc.DENY):
        raise HTTPException(status_code=422, detail={"error": "decision_must_be_approve_or_deny"})
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        with kb.write_txn(conn):
            task = _load_owned_task(conn, run_id, identity)
            events = kb.list_events(conn, task.id)
            # Idempotent replay BEFORE the terminal guard (same rationale as
            # /answer): the worker may consume the decision and drive the run
            # terminal between the original decision and a retried duplicate; a
            # duplicate for an already-decided approval must still return 200
            # (already_decided, ORIGINAL decision stands), never 409 run_terminal.
            prior = _rc.decision_for(events, approval_id)
            if prior is not None:
                return {"run_id": run_id, "approval_id": approval_id,
                        "already_decided": True, "decision": prior}
            if _run_status(task, cancelled=_is_cancelled(events)) in _TERMINAL_PRODUCT_STATUSES:
                raise HTTPException(status_code=409, detail={"error": "run_terminal"})
            open_a = any(
                e.kind == _rc.APPROVAL_REQUEST and (e.payload or {}).get("approval_id") == approval_id
                for e in events
            )
            if not open_a:
                raise HTTPException(status_code=409, detail={"error": "no_such_open_approval"})
            kb._append_event(conn, task.id, _rc.APPROVAL_DECISION,
                             {"approval_id": approval_id, "decision": decision,
                              "by": identity.user})
    return {"run_id": run_id, "approval_id": approval_id, "decision": decision}


@router.post("/api/runtime/v1/runs/{run_id}/pause")
async def runtime_pause_run(
    run_id: str,
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Request a pause (ADR-0004, A6). Records a ``run_pause`` and reports
    ``pause_requested``. The production ``youtab chat`` worker acknowledges it at
    its next turn boundary and either persists a valid ``run_checkpoint`` (→
    ``paused``, resumable, NOT a cancel/restart, no step re-executed) or, if it
    cannot persist a valid checkpoint, records ``run_pause_failed`` and stops
    FAIL-CLOSED (→ ``pause_failed``, non-completed, non-cancelled, NOT resumable
    until an explicit recovery action). Idempotent over an active pause epoch; a
    pause of a terminal run is a no-op that preserves the terminal status.
    """
    await _verify_signed_command(request, identity)
    await _admit_execution_grant(request, identity)
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        with kb.write_txn(conn):
            task = _load_owned_task(conn, run_id, identity)
            events = kb.list_events(conn, task.id)
            cancelled = _is_cancelled(events)
            status = _run_status(task, cancelled=cancelled,
                                 interactive=_interactive_status(events))
            if status in _TERMINAL_PRODUCT_STATUSES:
                return {"run_id": run_id, "status": status}
            if _rc.is_paused(events):
                # A pause epoch is already active — idempotent; report its sub-state
                # (pause_requested / paused / pause_failed) rather than stacking.
                return {"run_id": run_id, "status": _rc.interactive_status(events)}
            kb._append_event(conn, task.id, _rc.PAUSE, {"by": identity.user})
    return {"run_id": run_id, "status": _rc.PAUSE_REQUESTED}


@router.post("/api/runtime/v1/runs/{run_id}/resume")
async def runtime_resume_run(
    run_id: str,
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Authorize a resume from a VALID persisted checkpoint (ADR-0004, A6).

    Fail-closed: a terminal (completed/cancelled) run cannot be resumed (409); a
    run that is not ``paused`` with a valid checkpoint cannot be resumed (409). A
    ``pause_failed`` run (accepted pause, no restorable checkpoint) is refused
    with a distinct error and requires an explicit recovery action — it is never
    silently resumed or restarted. On success the run is re-queued so the
    dispatcher spawns a fresh worker that loads the checkpoint and continues from
    the SAME state — no step is re-executed.
    """
    await _verify_signed_command(request, identity)
    await _admit_execution_grant(request, identity)
    # SEC-9 #9: the resume event AND the blocked->ready requeue are ONE atomic
    # write. Previously the RESUME event committed in one txn and ``unblock_task``
    # ran in a SEPARATE txn whose failure was swallowed — leaving the run with
    # ``run_resume`` recorded but still blocked while the API falsely returned
    # ``status=running``. Now both happen inside a single ``write_txn``: a requeue
    # failure rolls the RESUME event back too, and a DB failure returns an explicit
    # retryable error rather than a false ``running``.
    try:
        with kb.connect_closing(board=RUNTIME_BOARD) as conn:
            with kb.write_txn(conn):
                task = _load_owned_task(conn, run_id, identity)
                events = kb.list_events(conn, task.id)
                cancelled = _is_cancelled(events)
                if _run_status(task, cancelled=cancelled) in _TERMINAL_PRODUCT_STATUSES:
                    raise HTTPException(status_code=409, detail={"error": "run_terminal"})
                if _interactive_status(events) == _rc.PAUSE_FAILED:
                    raise HTTPException(status_code=409,
                                        detail={"error": "pause_failed_requires_recovery"})
                if not _rc.has_valid_checkpoint_for_resume(events):
                    raise HTTPException(status_code=409, detail={"error": "run_not_paused"})
                kb._append_event(conn, task.id, _rc.RESUME, {"by": identity.user})
                # The worker paused itself by BLOCKING the task; flipping
                # blocked->ready makes it claimable by a fresh worker. Do it in
                # THIS txn via the locked core. If it cannot flip (already
                # requeued by a concurrent resume, or not in blocked/scheduled),
                # roll back the RESUME event too and report a conflict — never a
                # false ``running`` and never a stranded run_resume.
                if not kb._unblock_task_locked(conn, run_id):
                    raise HTTPException(status_code=409,
                                        detail={"error": "resume_requeue_conflict"})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — DB failure: explicit + retryable
        _log.warning("runtime resume: atomic resume failed for %s: %s", run_id, exc)
        raise HTTPException(
            status_code=503, detail={"error": "resume_unavailable_retryable"}
        ) from exc
    # Committed: the RESUME event and the blocked->ready requeue are both durable.
    # Only now tick the dispatcher so a fresh worker re-claims and resumes from the
    # checkpoint (no step is re-executed).
    ensure_dispatcher_running()
    return {"run_id": run_id, "status": "running"}


@router.post("/api/runtime/v1/runs/{run_id}/retry")
async def runtime_retry_run(
    run_id: str,
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Retry a finished/blocked run by creating a fresh run from the same spec."""
    await _verify_signed_command(request, identity)
    # Managed retry is a governed mutation: it requires execution authority (a
    # Simorgh grant) in managed mode; standalone refuses a grant. Capture the
    # freshly-admitted grant + frozen capability manifest so they can be PERSISTED
    # onto the child run below (WAVE-30H): a managed retry re-mints its own grant
    # here, and the dispatched child worker re-admits it from its OWN persisted
    # events (worker_admission.establish_managed_admission). Without persisting it,
    # a managed retry child has no grant to re-admit and fails closed at worker
    # admission — created but never able to execute. Mirrors create_run.
    _retry_grant_header, _retry_grant_manifest = await _admit_execution_grant(
        request, identity
    )
    # Authorize ownership FIRST — a caller that does not own run_id gets 404 and
    # no effect-ledger row is ever created in their namespace (reviewer A INFO-2).
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        _owned = _load_owned_task(conn, run_id, identity)
        # SEC-9 #6: refuse a retry grant that widens the budget / extends the
        # deadline vs the original run — BEFORE claiming the effect or creating any
        # child (so no partial state on a rejected retry).
        _assert_retry_grant_budget_not_widened(
            kb.list_events(conn, _owned.id), _retry_grant_header
        )
    # WAVE-26 effect-level idempotency: a retry spawns a fresh run (an observable
    # effect). When the caller supplies an Idempotency-Key, atomically CLAIM the
    # effect so a double-submitted or concurrently-retried request (even across
    # uvicorn workers) yields exactly one child. Un-keyed retries keep the prior
    # behaviour (each a distinct, legitimate re-attempt).
    _retry_effect_id = None
    _retry_principal = None
    if identity.idempotency_key:
        from youtab_runtime import effect_ledger as _el
        from youtab_runtime.run_journal import Principal as _Principal

        _retry_principal = _Principal(identity.tenant, identity.user)
        _eff = _el.begin_effect(
            run_id, _retry_principal, "runtime.retry", identity.idempotency_key,
            correlation_id=identity.correlation_id,
        )
        won, _eff = _el.try_claim(_eff.effect_id, _retry_principal,
                                  correlation_id=identity.correlation_id)
        if not won:
            prior = _eff.detail.get("child_run_id")
            if str(_eff.state) == "committed" and prior:
                with kb.connect_closing(board=RUNTIME_BOARD) as conn:
                    child = kb.get_task(conn, prior)
                    if child:
                        child_mode = (
                            "deterministic"
                            if _mode_from_events(kb.list_events(conn, child.id))
                            == "deterministic"
                            else "model"
                        )
                        return _run_summary(child, execution_mode=child_mode)
            # in_progress (a concurrent claimer) / unknown: outcome unproven —
            # do not blindly re-run; the winner will record the child.
            raise HTTPException(
                status_code=409,
                detail={"error": "retry_in_progress_or_unknown",
                        "run_id": run_id},
            )
        _retry_effect_id = _eff.effect_id
    # A winning claim MUST reach a terminal effect state. If creation/dispatch
    # raises after the claim, mark the effect unknown so a same-key retry is
    # reconciled rather than permanently 409'd (the API process stays alive, so
    # recover_interrupted — which only reclaims provably-dead owners — would not
    # otherwise free the stranded in_progress claim). Mirrors atomic_write_text.
    try:
        with kb.connect_closing(board=RUNTIME_BOARD) as conn:
            task = _load_owned_task(conn, run_id, identity)
            _orig_events = kb.list_events(conn, task.id)
            # Carry the original run's execution mode forward so a retry of a
            # deterministic integration run stays deterministic and a retry of a
            # real run stays real.
            prior_mode = _mode_from_events(_orig_events)
            retry_mode = "deterministic" if (prior_mode == "deterministic" and _deterministic_worker_enabled()) else "model"
            # WAVE-30H: inherit the ORIGINAL run's COMPLETE authoritative execution
            # binding — resolved provider/model pin (row) + branded engine selection
            # + clamped cost policy — so the retry re-executes on the SAME substrate
            # and can NEVER silently downgrade to the worker-default model. Fails
            # closed (HTTP 422) if that recorded binding is invalid or conflicting.
            _binding = _retry_execution_binding(task, _orig_events)
            # Preserve correlation lineage engine-side: the retry inherits the
            # ORIGINAL task's correlation id (independent of the inbound header)
            # so the whole retry chain is queryable by one correlation (C6).
            # Fall back to the inbound signed correlation only if the original
            # row predates the dedicated column (legacy).
            lineage_correlation = task.correlation_id or identity.correlation_id
            def _persist_child(conn, rid):
                # WAVE-30H (race): persist the child's mode / lineage / engine /
                # limits / RE-SCOPED binding / re-minted grant INSIDE create_task_ex's
                # own write_txn — atomic with the child row insert — so the child is
                # never visible to the dispatcher before its binding/grant exist
                # (closing the same two-transaction race as create_run). A raise here
                # rolls the child insert back entirely (no orphan, no archive needed)
                # and is remapped to a fail-closed 500 below.
                kb._append_event(
                    conn, rid, _MODE_EVENT,
                    {"mode": retry_mode, "correlation_id": lineage_correlation},
                )
                # Authoritative lineage marker: this run is a retry of ``run_id``.
                kb._append_event(
                    conn, rid, _RETRIED_FROM_EVENT,
                    {"original_run_id": run_id, "correlation_id": lineage_correlation},
                )
                # Re-record the branded engine selection (same profile as the original).
                if _binding["engine_selection"] is not None:
                    kb._append_event(conn, rid, _ENGINE_EVENT, {
                        "profile_id": _binding["engine_selection"]["profile_id"],
                        "public_label": _binding["engine_selection"].get("public_label"),
                    })
                # Re-record the authoritative clamped cost policy / limits.
                if _binding["limits"]:
                    kb._append_event(conn, rid, _LIMITS_EVENT, _binding["limits"])
                # Re-issue the parent's effective binding on the CHILD, RE-SCOPED to
                # the child's own run (same provider/model substrate — binding_digest
                # invariant, proving no drift — but run_id=rid, root=parent lineage).
                # The parent binding was already verify_binding'd in
                # _retry_execution_binding (#4), so rescope never launders a tampered
                # parent into a fresh valid child hash.
                if _binding.get("effective_binding") is not None:
                    _child_binding = eb.rescope_binding(
                        _binding["effective_binding"],
                        run_id=rid, tenant=identity.tenant, workspace=identity.workspace,
                    )
                    kb._append_event(conn, rid, eb.BINDING_EVENT, _child_binding)
                # Re-minted per-retry Simorgh grant + frozen manifest (managed only;
                # this retry's own authority, never copied from the original run).
                if _retry_grant_header:
                    kb._append_event(conn, rid, _GRANT_EVENT, {"grant": _retry_grant_header})
                    if _retry_grant_manifest is not None:
                        kb._append_event(
                            conn, rid, _GRANT_MANIFEST_EVENT, _retry_grant_manifest
                        )

            try:
                new_id = kb.create_task(
                    conn,
                    title=f"{task.title} (retry)",
                    body=task.body,
                    assignee=task.assignee,
                    created_by=identity.user,
                    tenant=identity.tenant,
                    skills=task.skills,
                    goal_mode=task.goal_mode,
                    max_runtime_seconds=task.max_runtime_seconds,
                    # Row-level provider/model pin — inherited ATOMICALLY with the
                    # child's binding/grant in create_task_ex's own transaction.
                    model_override=_binding["model_override"],
                    provider_override=_binding["provider_override"],
                    board=RUNTIME_BOARD,
                    correlation_id=lineage_correlation,
                    session_id=identity.correlation_id,
                    on_created=_persist_child,
                )
            except HTTPException:
                raise
            except BaseException:
                # The child insert was rolled back atomically (no partial child to
                # archive). Fail closed: never dispatch on an incomplete binding.
                raise HTTPException(
                    status_code=500,
                    detail={"error": "retry_binding_not_persisted",
                            "run_id": run_id},
                )
        ensure_dispatcher_running()
        _dispatch_tick()
    except BaseException:
        if _retry_effect_id is not None:
            from youtab_runtime import effect_ledger as _el

            # Guard the ledger write so a secondary ledger/DB failure cannot mask
            # the original create/dispatch error (which the caller must see). If
            # marking unknown fails, the effect stays in_progress; a later process
            # restart's recover_interrupted reclaims it (owner then provably dead),
            # so the idempotency key is reconciled rather than lost.
            try:
                _el.mark_unknown(_retry_effect_id, _retry_principal)
            except BaseException:
                import logging

                logging.getLogger(__name__).warning(
                    "runtime_retry_run: could not mark retry effect %s unknown "
                    "after a dispatch failure; leaving it for restart recovery",
                    _retry_effect_id, exc_info=True,
                )
        raise
    if _retry_effect_id is not None:
        from youtab_runtime import effect_ledger as _el

        _el.mark_committed(
            _retry_effect_id, _retry_principal,
            detail={"child_run_id": new_id},
        )
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        new_task = kb.get_task(conn, new_id)
        return (
            _run_summary(new_task, execution_mode=retry_mode)
            if new_task
            else {"run_id": new_id, "status": "queued", "execution_mode": retry_mode}
        )


# ---------------------------------------------------------------------------
# Catalog helpers (best-effort real projections; degrade honestly)
# ---------------------------------------------------------------------------


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        data = None
    return data if isinstance(data, dict) else {}


def _sanitize_label(value: Any) -> str:
    """Clean a toolset label/description for the service plane.

    ``CONFIGURABLE_TOOLSETS`` labels carry leading emoji that can arrive as lone
    surrogates in some encodings; strip those and any non-printable control chars
    so the product never renders mojibake. Normal text and valid emoji are kept.
    """
    return "".join(
        c for c in str(value)
        if not (0xD800 <= ord(c) <= 0xDFFF) and (c.isprintable() or c == " ")
    ).strip()


def _tools_catalog() -> Dict[str, Any]:
    """Real configurable-toolset catalog as structured DTOs.

    ``_get_effective_configurable_toolsets()`` yields ``(key, label, description)``
    tuples (built-in + plugin toolsets). Previously each tuple was stringified into
    the id/name, so the product rendered raw ``"('browser', '...', '...')"`` text.
    Return proper ``{id, name, description}`` instead.
    """
    try:
        from youtab_agent_cli import tools_config
        getter = getattr(tools_config, "_get_effective_configurable_toolsets", None)
        tools: List[Dict[str, str]] = []
        if callable(getter):
            for entry in getter() or []:
                if isinstance(entry, (list, tuple)) and entry:
                    tid = str(entry[0])
                    label = _sanitize_label(entry[1]) if len(entry) > 1 else tid
                    desc = _sanitize_label(entry[2]) if len(entry) > 2 else ""
                elif isinstance(entry, str):
                    tid, label, desc = entry, entry, ""
                else:
                    continue
                if tid:
                    tools.append({"id": tid, "name": label or tid, "description": desc})
        tools.sort(key=lambda t: t["id"])
        return {"tools": tools, "available": True}
    except Exception as exc:  # noqa: BLE001
        _log.debug("runtime tools catalog unavailable: %s", exc)
        return {"tools": [], "available": False, "reason": "tools_catalog_unavailable"}


def _skills_catalog() -> Dict[str, Any]:
    """Real installed-skills catalog for the service plane.

    Uses the same authoritative finder the dashboard ``/api/skills`` route and
    the ``youtab skills`` CLI use (``skills_tool._find_all_skills``), which scans
    the profile skills dir + external dirs for ``SKILL.md`` frontmatter and
    honours the disabled-set. Returns enabled skills only (``skip_disabled`` is
    left False), so a skill disabled in config never appears as invocable. The
    previous implementation imported a non-existent ``skills_registry`` module
    and always returned empty; this returns the genuine registry.
    """
    try:
        from tools.skills_tool import _find_all_skills, _sort_skills
        found = _sort_skills(_find_all_skills())
        items = [
            {
                "id": s.get("name"),
                "name": s.get("name"),
                "description": s.get("description", ""),
                "category": s.get("category", ""),
            }
            for s in found
            if s.get("name")
        ]
        return {"skills": items, "available": True}
    except Exception as exc:  # noqa: BLE001
        _log.debug("runtime skills catalog unavailable: %s", exc)
    return {"skills": [], "available": False, "reason": "skills_catalog_unavailable"}


def _sandbox_backends() -> Dict[str, Any]:
    try:
        from youtab_agent_cli import web_server
        backends = getattr(web_server, "_TERMINAL_BACKEND_NAMES", None)
        if backends:
            names = list(backends() if callable(backends) else backends)
            return {"backends": [{"id": n, "name": n} for n in names],
                    "default": names[0] if names else None, "available": True}
    except Exception as exc:  # noqa: BLE001
        _log.debug("runtime sandbox backends unavailable: %s", exc)
    return {"backends": [], "default": None, "available": False,
            "reason": "sandbox_backends_unavailable"}
