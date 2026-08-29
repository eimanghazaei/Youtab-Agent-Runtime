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

from youtab_agent_cli import agent_identity
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


# Event recorded at create-run naming the execution mode of a run:
#   "model"         — the real, provider-backed agent (production default);
#   "deterministic" — the NON-PRODUCTION deterministic integration worker.
_MODE_EVENT = "runtime_execution_mode"

# Event recorded at create-run naming the branded product-engine the caller
# selected (a profile_id + its public label). Consumer-safe; never carries the
# resolved provider/model/override behind that selection.
_ENGINE_EVENT = "runtime_engine_selection"

# Event recorded on a retry run naming the original run it was retried from and
# the preserved correlation id — makes retry lineage explicit and queryable.
_RETRIED_FROM_EVENT = "runtime_retried_from"


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


def _run_summary(
    task: "kb.Task", *, cancelled: bool = False, execution_mode: str = "model"
) -> Dict[str, Any]:
    return {
        "run_id": task.id,
        "agent_id": task.assignee,
        "agent_name": task.assignee,
        "status": _run_status(task, cancelled=cancelled),
        "execution_mode": execution_mode,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.completed_at,
        "tenant_id": task.tenant,
        # The caller's own per-request correlation id (authoritative column).
        # Not a provider/model secret; safe to surface to the consumer.
        "correlation_id": task.correlation_id,
    }


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
    summary = _run_summary(task, cancelled=cancelled, execution_mode=mode)
    summary.update({
        "task": task.body,
        "title": task.title,
        "result": result,
        "error": task.last_failure_error,
        "retries": task.consecutive_failures,
        "current_run_id": task.current_run_id,
        "model": meta.get("model") or meta.get("model_id"),
        "provider": meta.get("provider"),
        "usage": meta.get("usage") or meta.get("tokens"),
        # The branded engine the caller selected at create, if any (consumer-safe
        # {profile_id, public_label} only; never the provider/model it resolved to).
        "engine_selection": _engine_selection_from_events(events),
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
    # Surface the correlation id carried on authoritative dispatch/lineage
    # events (contract C5), so a consumer can trace events by correlation.
    correlation_id = None
    if isinstance(e.payload, dict):
        correlation_id = e.payload.get("correlation_id")
    return {
        "id": e.id,          # monotonic cursor
        "run_id": run_id,
        "kind": e.kind,
        "payload": e.payload,
        "created_at": e.created_at,
        "correlation_id": correlation_id,
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
            summaries.append(_run_summary(
                t, cancelled=_is_cancelled(events), execution_mode=_mode_from_events(events)
            ))
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
    "title"?: str, "skills"?: [str], "max_runtime_seconds"?: int,
    "engine"?: "<profile_id>" }``.

    ``agent`` (the worker profile) is required. ``engine`` is optional and
    orthogonal: it selects the branded model substrate (e.g. ``eco.v01``) and,
    when that engine is bound to a provider/model, pins the run to it via
    ``model_override``/``provider_override`` without touching the profile.
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

    # Execution mode: real provider-backed model by default. A caller may request
    # the non-production deterministic integration worker with ``deterministic``;
    # it is honoured ONLY when the deterministic worker is enabled (non-prod), so
    # a production run can never be silently downgraded to a deterministic stub.
    want_det = bool(payload.get("deterministic", False))
    mode = "deterministic" if (want_det and _deterministic_worker_enabled()) else "model"

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
            model_override=model_override,
            provider_override=provider_override,
            board=RUNTIME_BOARD,
            # Authoritative dedicated correlation column (canonical v1). The
            # session_id write below stays UNCHANGED as the legacy overload.
            correlation_id=identity.correlation_id,
            session_id=identity.correlation_id,
        )
        # Record the resolved mode as a create-time event (survives restart,
        # visible in the run's own event stream, read by the dispatcher spawn).
        # The correlation id is stamped on this authoritative dispatch event
        # (fail-closed inside the run txn) so the run's own stream is queryable
        # by correlation independent of the tasks column (contract C4).
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                run_id,
                _MODE_EVENT,
                {"mode": mode, "correlation_id": identity.correlation_id},
            )
            # Record the branded engine selection (consumer-safe: profile_id +
            # public label only; never the provider/model it resolved to).
            if engine_identity is not None:
                kb._append_event(conn, run_id, _ENGINE_EVENT, {
                    "profile_id": engine,
                    "public_label": engine_identity.public_label,
                })
        task = kb.get_task(conn, run_id)

    # Kick a dispatch tick immediately and keep the ticker running so the run
    # actually executes and finalises.
    ensure_dispatcher_running()
    _dispatch_tick()
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
        # Carry the original run's execution mode forward so a retry of a
        # deterministic integration run stays deterministic and a retry of a
        # real run stays real.
        prior_mode = _mode_from_events(kb.list_events(conn, task.id))
        retry_mode = "deterministic" if (prior_mode == "deterministic" and _deterministic_worker_enabled()) else "model"
        # Preserve correlation lineage engine-side: the retry inherits the
        # ORIGINAL task's correlation id (independent of the inbound header) so
        # the whole retry chain is queryable by one correlation (contract C6).
        # Fall back to the inbound signed correlation only if the original row
        # predates the dedicated column (legacy).
        lineage_correlation = task.correlation_id or identity.correlation_id
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
            correlation_id=lineage_correlation,
            session_id=identity.correlation_id,
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                new_id,
                _MODE_EVENT,
                {"mode": retry_mode, "correlation_id": lineage_correlation},
            )
            # Authoritative lineage marker: this run is a retry of ``run_id``,
            # carrying the preserved correlation (fail-closed run-txn write).
            kb._append_event(
                conn,
                new_id,
                _RETRIED_FROM_EVENT,
                {
                    "original_run_id": run_id,
                    "correlation_id": lineage_correlation,
                },
            )
    ensure_dispatcher_running()
    _dispatch_tick()
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
