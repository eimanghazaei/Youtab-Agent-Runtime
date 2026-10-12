"""Durable `/v1/runs` coordinator + HTTP handlers backed by the canonical RunStore.

This wires the EXISTING `/v1/runs` contract (create/get/events/result/stop) to the
single canonical run authority (``youtab_runtime.durable_run_store.RunStore``).
There is no second run API, task-id space, ledger or execution authority: the same
``run_id``/``task_id`` flows through create, reads, the event stream, result and
cancellation.

Invariants enforced here:
- the run is PERSISTED (create_run) BEFORE any work is dispatched;
- every read / event stream / result / cancel is bound to the requester's
  tenant + workspace + principal; a mismatch returns not-found (no existence leak);
- events are replayable by ``from_seq`` / ``Last-Event-ID`` and are multi-consumer;
- a caller wait timeout returns ``RUNNING + task_id`` and NEVER cancels the run;
  only an explicit authorized ``stop`` (or the run's own execution deadline /
  safety / terminal failure) stops execution.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from youtab_runtime.durable_run_store import (
    RunIdentity,
    RunState,
    RunStore,
    TERMINAL_STATES,
)

# A worker drives the store for one run: (store, run_id, lease_epoch) -> None.
# It must set a terminal state (SUCCEEDED/FAILED) and record progress; it should
# cooperatively observe cancellation via store.get_run(...)["state"].
Worker = Callable[[RunStore, str, int], None]


@dataclass(frozen=True)
class RequestPrincipal:
    """Requester identity — in production supplied by the Gateway's SIGNED scope,
    never from unsigned client fields."""
    tenant_id: str
    workspace_id: str
    principal_id: str


class ScopeDenied(PermissionError):
    """Requester scope does not match the run — surfaced as not-found (no leak)."""


class DurableRunsCoordinator:
    def __init__(self, store: RunStore):
        self._store = store
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def _current_state(self, run_id: str) -> RunState:
        row = self._store.get_run(run_id)
        return RunState(row["state"]) if row is not None else RunState.UNKNOWN

    # -- authorization ------------------------------------------------------ #
    @staticmethod
    def _authorize(row: Dict[str, Any], who: RequestPrincipal) -> None:
        if (row["tenant_id"] != who.tenant_id
                or row["workspace_id"] != who.workspace_id
                or row["principal_id"] != who.principal_id):
            raise ScopeDenied("run not visible to this principal/tenant/workspace")

    def _get_scoped(self, run_id: str, who: RequestPrincipal) -> Optional[Dict[str, Any]]:
        row = self._store.get_run(run_id)
        if row is None:
            return None
        self._authorize(row, who)
        return row

    # -- lifecycle ---------------------------------------------------------- #
    def submit(self, identity: RunIdentity, worker: Worker) -> Dict[str, Any]:
        """Persist the run BEFORE dispatch, then run the worker under its own
        lease in a background thread. Returns the persisted (accepted) run row."""
        row = self._store.create_run(identity)
        # Idempotent replay: if this run already reached a terminal or is already
        # running, do not dispatch a duplicate worker.
        if RunState(row["state"]) != RunState.QUEUED:
            return row

        def _run() -> None:
            epoch = self._store.claim(identity.run_id, owner=f"worker-{uuid.uuid4().hex[:8]}")
            if epoch is None:
                return  # someone else claimed it — no duplicate execution
            self._store.transition(identity.run_id, RunState.RUNNING)
            try:
                worker(self._store, identity.run_id, epoch)
                cur = self._current_state(identity.run_id)
                if cur not in TERMINAL_STATES and cur != RunState.CANCELLING:
                    self._store.transition(identity.run_id, RunState.SUCCEEDED)
            except Exception as exc:  # noqa: BLE001
                if self._current_state(identity.run_id) not in TERMINAL_STATES:
                    self._store.transition(identity.run_id, RunState.FAILED,
                                           error_ref=f"error://{type(exc).__name__}")

        t = threading.Thread(target=_run, name=f"durable-run-{identity.run_id}", daemon=True)
        with self._lock:
            self._threads[identity.run_id] = t
        t.start()
        return row

    def wait(self, run_id: str, who: RequestPrincipal, *, wait_timeout: float):
        row = self._get_scoped(run_id, who)
        if row is None:
            raise KeyError(run_id)
        return self._store.wait_for_terminal(run_id, wait_timeout=wait_timeout)

    def get(self, run_id: str, who: RequestPrincipal) -> Optional[Dict[str, Any]]:
        return self._get_scoped(run_id, who)

    def events(self, run_id: str, who: RequestPrincipal, *, from_seq: int = 0):
        if self._get_scoped(run_id, who) is None:
            raise KeyError(run_id)
        return self._store.get_events(run_id, from_seq=from_seq)

    def result(self, run_id: str, who: RequestPrincipal) -> Optional[Dict[str, Any]]:
        row = self._get_scoped(run_id, who)
        if row is None:
            return None
        return {
            "run_id": row["run_id"], "task_id": row["task_id"], "state": row["state"],
            "terminal": RunState(row["state"]) in TERMINAL_STATES,
            "result_ref": row["result_ref"], "error_ref": row["error_ref"],
            "checkpoint_ref": row["checkpoint_ref"],
        }

    def cancel(self, run_id: str, who: RequestPrincipal, *, reason: str) -> Dict[str, Any]:
        if self._get_scoped(run_id, who) is None:
            raise KeyError(run_id)
        return self._store.request_cancel(run_id, reason=reason, by=f"principal:{who.principal_id}")


# --------------------------------------------------------------------------- #
# aiohttp handlers (thin transport over the coordinator) — same /v1/runs paths
# --------------------------------------------------------------------------- #
def make_app(coordinator: "DurableRunsCoordinator", worker_factory: Callable[[dict], Worker]):
    """Build an aiohttp app exposing the canonical /v1/runs contract on RunStore.

    ``worker_factory(payload) -> Worker`` builds the actual execution for a create
    request (the agent run in production; a stub in tests). aiohttp is imported
    lazily so importing this module never hard-requires the server extra."""
    from aiohttp import web

    def _principal(request) -> RequestPrincipal:
        # Production: derived from the Gateway's SIGNED identity. Here: validated
        # headers stand in for that signed scope.
        return RequestPrincipal(
            tenant_id=request.headers.get("X-Tenant", ""),
            workspace_id=request.headers.get("X-Workspace", ""),
            principal_id=request.headers.get("X-Principal", ""),
        )

    async def handle_create(request):
        who = _principal(request)
        body = await request.json() if request.can_read_body else {}
        run_id = body.get("run_id") or f"run_{uuid.uuid4().hex[:12]}"
        task_id = body.get("task_id") or run_id
        identity = RunIdentity(
            task_id=task_id, run_id=run_id, tenant_id=who.tenant_id,
            organization_id=body.get("organization_id", who.tenant_id),
            workspace_id=who.workspace_id, principal_id=who.principal_id,
            agent_id=body.get("agent_id", "agent"), operation=body.get("operation", "run"),
            delegation_id=body.get("delegation_id"),
            idempotency_key=body.get("idempotency_key"),
            request_digest=body.get("request_digest"),
            execution_deadline=body.get("execution_deadline"),
        )
        coordinator.submit(identity, worker_factory(body))
        wait_timeout = float(request.query.get("wait_timeout", body.get("wait_timeout", 2.0)))
        wr = coordinator.wait(run_id, who, wait_timeout=wait_timeout)
        return web.json_response({
            "run_id": wr.run_id, "task_id": wr.task_id, "state": wr.state,
            "terminal": wr.terminal, "last_progress_seq": wr.last_progress_seq,
            "checkpoint_ref": wr.checkpoint_ref, "result_ref": wr.result_ref,
            "reconnect": wr.reconnect,
        }, status=200 if wr.terminal else 202)

    async def handle_get(request):
        who = _principal(request)
        try:
            row = coordinator.get(request.match_info["run_id"], who)
        except ScopeDenied:
            row = None  # denied is surfaced as not-found (no existence leak)
        if row is None:
            return web.json_response({"error": "run_not_found"}, status=404)
        return web.json_response({k: row[k] for k in (
            "run_id", "task_id", "state", "progress_seq", "checkpoint_ref",
            "result_ref", "error_ref", "created_at", "updated_at")})

    async def handle_result(request):
        who = _principal(request)
        try:
            res = coordinator.result(request.match_info["run_id"], who)
        except ScopeDenied:
            res = None
        if res is None:
            return web.json_response({"error": "run_not_found"}, status=404)
        return web.json_response(res)

    async def handle_events(request):
        who = _principal(request)
        run_id = request.match_info["run_id"]
        # Reconnect cursor: Last-Event-ID header wins, else ?from_seq=.
        from_seq = int(request.headers.get("Last-Event-ID", request.query.get("from_seq", 0)) or 0)
        try:
            events = coordinator.events(run_id, who, from_seq=from_seq)
        except (KeyError, ScopeDenied):
            return web.json_response({"error": "run_not_found"}, status=404)
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        for e in events:
            await resp.write(f"id: {e['seq']}\ndata: {e['kind']}:{e['seq']}\n\n".encode())
        await resp.write(b": end\n\n")
        return resp

    async def handle_stop(request):
        who = _principal(request)
        try:
            row = coordinator.cancel(request.match_info["run_id"], who, reason="client stop")
        except (KeyError, ScopeDenied):
            return web.json_response({"error": "run_not_found"}, status=404)
        return web.json_response({"run_id": row["run_id"], "state": row["state"]})

    app = web.Application()
    app.router.add_post("/v1/runs", handle_create)
    app.router.add_get("/v1/runs/{run_id}", handle_get)
    app.router.add_get("/v1/runs/{run_id}/result", handle_result)
    app.router.add_get("/v1/runs/{run_id}/events", handle_events)
    app.router.add_post("/v1/runs/{run_id}/stop", handle_stop)
    return app
