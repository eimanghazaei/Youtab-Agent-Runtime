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

import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

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


def _effective_spawn():
    """Resolve the spawn function: explicit override > deterministic (non-prod) > real."""
    if _spawn_override is not None:
        return _spawn_override
    if _deterministic_worker_enabled():
        return _deterministic_spawn
    return None  # real _default_spawn (model-backed worker; needs credentials)


def _dispatch_tick() -> None:
    """Run one dispatcher tick on the runtime board (best-effort, never raises)."""
    try:
        with kb.connect_closing(board=RUNTIME_BOARD) as conn:
            kb.dispatch_once(conn, spawn_fn=_effective_spawn(), board=RUNTIME_BOARD)
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
    """Stop the background ticker (test teardown / shutdown)."""
    _ticker_stop.set()


# ---------------------------------------------------------------------------
# Auth / identity dependency
# ---------------------------------------------------------------------------


class RuntimeIdentity:
    """Verified caller context: service identity + gateway-forwarded end user."""

    __slots__ = ("tenant", "user", "roles", "correlation_id", "idempotency_key")

    def __init__(
        self,
        *,
        tenant: str,
        user: str,
        roles: List[str],
        correlation_id: str,
        idempotency_key: Optional[str],
    ) -> None:
        self.tenant = tenant
        self.user = user
        self.roles = roles
        self.correlation_id = correlation_id
        self.idempotency_key = idempotency_key


def _runtime_secret() -> str:
    return (os.environ.get("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET") or "").strip()


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
    return RuntimeIdentity(
        tenant=tenant,
        user=user,
        roles=roles,
        correlation_id=correlation_id,
        idempotency_key=idem,
    )


def _header(request: Request, name: str) -> str:
    return (request.headers.get(name) or "").strip()


# ---- signed-command nonce store (lazy singleton) --------------------------

_nonce_store: "Optional[rca.NonceStore]" = None
_nonce_store_lock = threading.Lock()


def _get_nonce_store() -> "rca.NonceStore":
    global _nonce_store
    with _nonce_store_lock:
        if _nonce_store is None:
            try:
                db_path = str(kb.kanban_db_path(board=RUNTIME_BOARD).parent / "runtime_command_nonces.db")  # noqa: E501
                _nonce_store = rca.SqliteNonceStore(db_path)
            except Exception as exc:  # noqa: BLE001 — fall back to in-memory
                _log.warning("runtime nonce store falling back to memory: %s", exc)
                _nonce_store = rca._MemoryNonceStore()
        return _nonce_store


async def _verify_signed_command(request: Request, identity: RuntimeIdentity) -> None:
    """Verify the signed-command envelope on a mutating request. Raise on failure."""
    body = await request.body()
    try:
        rca.verify_command(
            method=request.method,
            path=request.url.path,
            tenant=identity.tenant,
            user=identity.user,
            body=body,
            headers=request.headers,
            secret=_runtime_secret(),
            store=_get_nonce_store(),
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


def _run_status(task: "kb.Task", *, cancelled: bool) -> str:
    if cancelled and task.status in ("archived", "blocked", "done"):
        return "cancelled"
    return _STATUS_MAP.get(task.status, task.status)


def _run_summary(task: "kb.Task", *, cancelled: bool = False) -> Dict[str, Any]:
    return {
        "run_id": task.id,
        "agent_id": task.assignee,
        "agent_name": task.assignee,
        "status": _run_status(task, cancelled=cancelled),
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.completed_at,
        "tenant_id": task.tenant,
    }


def _run_detail(conn: "Any", task: "kb.Task", *, cancelled: bool) -> Dict[str, Any]:
    runs = kb.list_runs(conn, task.id)
    attachments = kb.list_attachments(conn, task.id)
    summary = _run_summary(task, cancelled=cancelled)
    summary.update({
        "task": task.body,
        "title": task.title,
        "result": task.result,
        "error": task.last_failure_error,
        "retries": task.consecutive_failures,
        "current_run_id": task.current_run_id,
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


def _event_projection(run_id: str, e: "kb.Event") -> Dict[str, Any]:
    return {
        "id": e.id,          # monotonic cursor
        "run_id": run_id,
        "kind": e.kind,
        "payload": e.payload,
        "created_at": e.created_at,
    }


def _is_cancelled(events: "List[kb.Event]") -> bool:
    return any(e.kind == _CANCEL_EVENT_KIND for e in events)


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
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        # Tenant is the DB-level filter; user ownership is enforced per row so a
        # tenant admin still can't read another user's runs through this surface.
        tasks = kb.list_tasks(
            conn, tenant=identity.tenant, include_archived=True,
            order_by="created-desc",
        )
        owned = [t for t in tasks if t.created_by == identity.user]
        # Project status (with cancel detection) before optional status filter.
        summaries = []
        for t in owned:
            events = kb.list_events(conn, t.id)
            summaries.append(_run_summary(t, cancelled=_is_cancelled(events)))
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
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = _load_owned_task(conn, run_id, identity)
        events = kb.list_events(conn, task.id)
        return _run_detail(conn, task, cancelled=_is_cancelled(events))


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
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = _load_owned_task(conn, run_id, identity)
        all_events = kb.list_events(conn, task.id)
        cancelled = _is_cancelled(all_events)
        fresh = [e for e in all_events if e.id > after][:limit]
        cursor = fresh[-1].id if fresh else after
        status = _run_status(task, cancelled=cancelled)
    return {
        "events": [_event_projection(run_id, e) for e in fresh],
        "cursor": cursor,
        "status": status,
        "terminal": status in _TERMINAL_PRODUCT_STATUSES,
    }


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
    "title"?: str, "skills"?: [str], "max_runtime_seconds"?: int }``.
    """
    await _verify_signed_command(request, identity)
    payload = await _json_body(request)
    agent = str(payload.get("agent") or "").strip()
    task_text = str(payload.get("task") or "").strip()
    if not agent or not task_text:
        raise HTTPException(status_code=422, detail={"error": "agent_and_task_required"})

    from youtab_agent_cli import profiles
    known = {getattr(p, "name", None) for p in profiles.list_profiles()}
    if agent not in known:
        raise HTTPException(status_code=404, detail={"error": "agent_not_found"})

    title = str(payload.get("title") or f"[{identity.tenant}] {task_text[:80]}").strip()
    goal_mode = bool(payload.get("goal_mode", False))
    skills = payload.get("skills") if isinstance(payload.get("skills"), list) else None
    try:
        max_runtime = payload.get("max_runtime_seconds")
        max_runtime = int(max_runtime) if max_runtime is not None else None
    except (TypeError, ValueError):
        max_runtime = None

    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        run_id = kb.create_task(
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
            board=RUNTIME_BOARD,
            session_id=identity.correlation_id,
        )
        task = kb.get_task(conn, run_id)

    # Kick a dispatch tick immediately and keep the ticker running so the run
    # actually executes and finalises.
    ensure_dispatcher_running()
    _dispatch_tick()
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = kb.get_task(conn, run_id)
        return _run_summary(task) if task else {"run_id": run_id, "status": "queued"}


@router.post("/api/runtime/v1/runs/{run_id}/cancel")
async def runtime_cancel_run(
    run_id: str,
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Cancel a run: record the intent, kill any live worker, block the task."""
    await _verify_signed_command(request, identity)
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = _load_owned_task(conn, run_id, identity)
        # Record the product-cancel intent (also how detail/list project
        # ``cancelled`` rather than a plain block/archive). Raw event writes are
        # not auto-committed by connect_closing, so wrap in a write txn.
        with kb.write_txn(conn):
            kb._append_event(conn, task.id, _CANCEL_EVENT_KIND, {"by": identity.user})
        worker_pid = task.worker_pid
        already_terminal = _run_status(task, cancelled=True) in _TERMINAL_PRODUCT_STATUSES

    if worker_pid and not already_terminal:
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


@router.post("/api/runtime/v1/runs/{run_id}/retry")
async def runtime_retry_run(
    run_id: str,
    request: Request,
    identity: RuntimeIdentity = Depends(require_service_identity),
):
    """Retry a finished/blocked run by creating a fresh run from the same spec."""
    await _verify_signed_command(request, identity)
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        task = _load_owned_task(conn, run_id, identity)
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
            board=RUNTIME_BOARD,
            session_id=identity.correlation_id,
        )
    ensure_dispatcher_running()
    _dispatch_tick()
    with kb.connect_closing(board=RUNTIME_BOARD) as conn:
        new_task = kb.get_task(conn, new_id)
        return _run_summary(new_task) if new_task else {"run_id": new_id, "status": "queued"}


# ---------------------------------------------------------------------------
# Catalog helpers (best-effort real projections; degrade honestly)
# ---------------------------------------------------------------------------


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        data = None
    return data if isinstance(data, dict) else {}


def _tools_catalog() -> Dict[str, Any]:
    try:
        from youtab_agent_cli import tools_config
        getter = getattr(tools_config, "_get_effective_configurable_toolsets", None)
        names: List[str] = []
        if callable(getter):
            result = getter()
            if isinstance(result, dict):
                names = sorted(result.keys())
            elif isinstance(result, (list, tuple, set)):
                names = sorted(str(x) for x in result)
        return {"tools": [{"id": n, "name": n} for n in names], "available": True}
    except Exception as exc:  # noqa: BLE001
        _log.debug("runtime tools catalog unavailable: %s", exc)
        return {"tools": [], "available": False, "reason": "tools_catalog_unavailable"}


def _skills_catalog() -> Dict[str, Any]:
    try:
        from youtab_agent_cli import skills_registry  # type: ignore
        lister = getattr(skills_registry, "list_skills", None)
        if callable(lister):
            skills = lister()
            items = [
                {"id": getattr(s, "name", s if isinstance(s, str) else str(s)),
                 "name": getattr(s, "name", s if isinstance(s, str) else str(s))}
                for s in skills
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
