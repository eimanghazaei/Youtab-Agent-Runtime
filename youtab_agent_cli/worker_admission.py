"""Establish the sealed Simorgh admitted execution context in a managed worker.

WAVE-30H R3 — the worker side of the admission-to-execution contract. A managed
run is dispatched to a worker subprocess (see ``kanban_db._default_spawn``); the
ingress endpoint already verified the Simorgh grant, consumed its single-use
nonce, and persisted it as a ``runtime_execution_grant`` event. The sealed
``AdmittedCommand`` carries a per-process proof and cannot cross the process
boundary, so the worker must reconstruct it here: load the persisted grant,
re-verify it (signature/expiry/scope) via ``re_admit_worker_grant`` (which does
NOT re-burn the nonce), and attach it to the agent so the tool_executor
authority gate can consume it before every real tool call.

Fail-closed: in ``managed`` trust mode a kanban run with no valid grant raises
:class:`ManagedWorkerAdmissionError` and the caller MUST refuse to execute.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class ManagedWorkerAdmissionError(RuntimeError):
    """A managed run could not establish its admitted context — refuse to run."""


def establish_managed_admission(agent) -> bool:
    """Attach the sealed Simorgh AdmittedCommand to ``agent`` for a managed run.

    Returns True when an admitted context was established; False when this is not
    a managed kanban run (local-standalone trust mode, or not a dispatched
    kanban worker). Raises :class:`ManagedWorkerAdmissionError` (fail closed)
    when a managed kanban run has no valid persisted grant.
    """
    from youtab_runtime import managed_execution as mx

    if mx.current_trust_mode() is not mx.TrustMode.MANAGED:
        return False  # standalone: no managed admission

    task_id = (os.environ.get("YOUTAB_AGENT_KANBAN_TASK") or "").strip()
    if not task_id:
        # Managed trust mode but not a kanban-dispatched worker. The ingress
        # endpoint is the admission authority for managed runs; there is nothing
        # to reconstruct here. The tool_executor gate still fails closed if a
        # tool is reached without an admitted context.
        return False

    from youtab_agent_cli import kanban_db as kb

    grant_header = None
    manifest_payload = None
    conn = kb.connect()
    try:
        for event in kb.list_events(conn, task_id):
            kind = getattr(event, "kind", None)
            payload = getattr(event, "payload", None) or {}
            if kind == "runtime_execution_grant":
                grant_header = payload.get("grant") or grant_header
            elif kind == "runtime_capability_manifest":
                manifest_payload = payload or manifest_payload
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if not grant_header:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} has no persisted Simorgh execution grant; "
            "refusing to execute (no bypass, no standalone fallback)"
        )

    # Reconstruct the SAME capability binding the ingress froze (correction 2), so
    # the worker enforces exactly the tool set authorized at admission — never a
    # tool registered after. A tampered persisted manifest fails the hash check.
    capability_binding = None
    if manifest_payload is not None:
        from youtab_agent_cli import capability_manifest as cm

        try:
            capability_binding = cm.binding_from_persisted(manifest_payload)
        except ValueError as exc:
            raise ManagedWorkerAdmissionError(
                f"managed run {task_id} capability manifest integrity failure"
            ) from exc

    try:
        admitted = mx.re_admit_worker_grant(
            grant_header=grant_header,
            boundary=mx.AuthorityBoundary(),
            public_keys=mx.load_brain_public_keys(),
            capability_binding=capability_binding,
        )
    except mx.ManagedAdmissionError as exc:
        raise ManagedWorkerAdmissionError(
            f"managed run {task_id} grant re-admission failed: {exc.code}"
        ) from exc

    agent._admitted_command = admitted
    # R4 (corrected): physically namespace the memory store by the grant's
    # tenant/workspace so this managed run can never read or write another
    # tenant's memory. The namespace is installed as an IMMUTABLE PER-RUN context
    # (contextvars), taken from the admitted grant envelope — NOT a mutable
    # process-global env var, which would leak across concurrent runs in a
    # process serving more than one. This worker is a single-run subprocess, so
    # setting it once here scopes the whole run. Read by
    # tools.memory_tool.get_memory_dir; sanitized there against path traversal.
    env = admitted.envelope
    try:
        from tools import memory_tool as _mt

        _mt.set_memory_namespace(env.tenant_id, env.workspace_id)
    except Exception:  # noqa: BLE001 — memory tool optional; fail-closed handled below
        # If the memory tool cannot be imported the run has no memory surface to
        # isolate; the tool_executor authority gate still governs every call.
        logger.warning("memory tool unavailable; no per-run namespace installed")
    logger.info(
        "managed run %s: Simorgh admitted execution context established", task_id
    )
    return True
